import os
import json
import pymysql
from pymysql.cursors import DictCursor

# Reuse connection across invocations (Lambda container reuse)
_conn = None


def get_conn():
    global _conn
    if _conn and _conn.open:
        try:
            _conn.ping(reconnect=True)
            return _conn
        except Exception:
            _conn = None  # force reconnect below

    host = os.environ["MYSQL_HOST"]
    port = int(os.environ.get("MYSQL_PORT", "3306"))
    user = os.environ["MYSQL_USER"]
    password = os.environ["MYSQL_PASSWORD"]
    dbname = os.environ["MYSQL_DB"]

    # If you need TLS:
    # ssl_ca = os.environ.get("MYSQL_SSL_CA")
    # ssl = {"ca": ssl_ca} if ssl_ca else None
    ssl = None

    _conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=dbname,
        autocommit=False,          # we will control commits
        cursorclass=DictCursor,
        ssl=ssl,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
    )
    return _conn


def lambda_handler(event, context):
    print("triggerSource:", event.get("triggerSource"))
    attrs = (event.get("request") or {}).get("userAttributes") or {}
    print("userAttributes keys:", sorted(list(attrs.keys())))
    print("userName:", event.get("userName"))

    # Best trigger for federated "first time user appears" behavior
    if event.get("triggerSource") != "PostAuthentication_Authentication":
        return event

    attrs = (event.get("request") or {}).get("userAttributes") or {}

    email = (attrs.get("email") or "").strip().lower()
    if not email:
        # If you truly can't rely on email, you'd need a unique key in DB for sub/username.
        print("WARN: Missing email in userAttributes; cannot check MySQL user table with current schema.")
        return event

    cognito_username = event.get("userName")  # Cognito username (for federated often Provider_subject-ish)
    # You may also want sub:
    # user_sub = attrs.get("sub")

    # Determine whether this is the first login by checking DB then upserting
    is_first_login = False
    conn = get_conn()

    try:
        with conn.cursor() as cur:
            # 1) Check if user exists (and not disabled) by email
            cur.execute(
                """
                SELECT id, is_disabled, cognito_username
                FROM user
                WHERE email = %s
                LIMIT 1
                """,
                (email,),
            )
            row = cur.fetchone()

            if row is None:
                # 2) First time we've seen this email -> insert
                # Use ON DUPLICATE KEY to be safe under concurrency.
                cur.execute(
                    """
                    INSERT INTO user (email, cognito_username, contact_info, is_disabled)
                    VALUES (%s, %s, %s, 0)
                    ON DUPLICATE KEY UPDATE
                        -- keep existing values, but backfill cognito_username if missing
                        cognito_username = COALESCE(user.cognito_username, VALUES(cognito_username))
                    """,
                    (email, cognito_username, _default_contact_info(attrs)),
                )

                # If we inserted, lastrowid may be 0 when it hit duplicate key.
                # So we determine "first login" by checking again whether it existed before.
                is_first_login = True
                print(f"INFO: First login detected (insert path) for email={email}")

            else:
                # Existing user
                if row.get("is_disabled"):
                    # Optional: you can block/deny by raising an error in some triggers,
                    # but PostAuthentication doesn't support modifying tokens.
                    print(f"WARN: User is disabled in DB: email={email} id={row.get('id')}")

                # Optional: backfill cognito_username if it's missing in DB
                if not row.get("cognito_username") and cognito_username:
                    cur.execute(
                        "UPDATE user SET cognito_username=%s WHERE id=%s",
                        (cognito_username, row["id"]),
                    )

                is_first_login = False
                print(f"INFO: Not first login for email={email} id={row.get('id')}")

        conn.commit()

    except Exception as e:
        conn.rollback()
        # Usually fail-open for login flows unless you must enforce provisioning
        print(f"ERROR: MySQL failure while checking/inserting user: {e}")
        return event

    if is_first_login:
        # ✅ Put your one-time provisioning side effects here
        # If it’s heavy/slow, send a message to SQS/SNS/EventBridge instead.
        on_first_login(email=email, cognito_username=cognito_username, attrs=attrs, event=event)

    return event


def _default_contact_info(attrs: dict) -> str:
    """
    Optional: stash whatever you want in contact_info.
    Keep it small; your column is 2000 chars.
    """

    return f"email: {attrs.get('email')}"


def _get_provider(attrs: dict) -> str:
    identities_raw = attrs.get("identities")
    if not identities_raw:
        return "COGNITO_LOCAL"
    try:
        identities = json.loads(identities_raw)
        if isinstance(identities, list) and identities:
            first = identities[0]
            return first.get("providerName") or first.get("providerType") or "FEDERATED"
        return "FEDERATED"
    except Exception:
        return "FEDERATED"


def on_first_login(*, email: str, cognito_username: str, attrs: dict, event: dict) -> None:
    print("INFO: Running first-login actions")
    print(
        json.dumps(
            {
                "email": email,
                "cognito_username": cognito_username,
                "userPoolId": event.get("userPoolId"),
                "userName": event.get("userName"),
            }
        )
    )
    # TODO: provision app resources, send welcome email, etc.

