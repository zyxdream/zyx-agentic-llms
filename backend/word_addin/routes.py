"""Tenant-local, authenticated Word API; enabled explicitly per deployment."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from functools import wraps
from urllib.parse import urlparse

import jwt
from flask import Blueprint, current_app, jsonify, request
from flask_cognito import cognito_auth_required, current_user
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from werkzeug.exceptions import RequestEntityTooLarge

from backend.database import db
from backend.models import ChatThread, Project, TextEntry, UploadedFiles
from backend.word_addin.service import ACTIONS, MAX_TEXT, owned_files, validate_request

bp = Blueprint("word_addin", __name__, url_prefix="/api/word")
TTL = 900
MAX_REQUEST_BYTES = 2_000_000
TERMINAL = {"complete", "failed", "cancelled"}
RELEASE = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
RENEW = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end"
CONSUME = "local v=redis.call('get',KEYS[1]); if v then redis.call('del',KEYS[1]); end; return v"


def redis_client():
    if "word_redis" not in current_app.extensions:
        from backend.constants import REDIS_URL
        current_app.extensions["word_redis"] = Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
    return current_app.extensions["word_redis"]


def bearer():
    value = request.headers.get("Authorization", "")
    return value[7:] if value.startswith("Bearer ") else ""


def live_claims(token):
    # Signature, pool and client are checked by cognito_auth_required first.
    # Enforce expiration here because legacy app config disables that check.
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        if claims.get("token_use") != "access" or float(claims.get("exp", 0)) <= time.time() + 5:
            return None
        return claims
    except (ValueError, TypeError, jwt.PyJWTError):
        return None


def authenticated(fn):
    @wraps(fn)
    @cognito_auth_required
    def wrapper(*args, **kwargs):
        if not live_claims(bearer()):
            return {"error": "Your session expired. Sign in again."}, 401
        if not current_user or current_user.is_disabled:
            return {"error": "This account cannot use LexText."}, 403
        return fn(*args, **kwargs)
    return wrapper


@bp.before_request
def enabled_and_bounded():
    if not current_app.config.get("WORD_ADDIN_ENABLED", False):
        return {"error": "The Word add-in is not enabled for this environment."}, 404
    # ``Request.max_content_length`` is read-only on the Flask version used by
    # the deployed API. Enforce this blueprint's smaller limit without changing
    # the main application's document-upload limit.
    if request.content_length is not None and request.content_length > request_limit():
        return {"error": "The Word request is too large."}, 413
    if request.method == "OPTIONS":
        return "", 204


@bp.after_request
def response_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    origin = request.headers.get("Origin")
    # Deployed LexText APIs already emit CORS at the nginx boundary. Sending
    # the same header here as well produces a comma-joined duplicate that
    # Safari rejects as a network error. Direct/local deployments can opt in.
    if (current_app.config.get("WORD_ADDIN_EMIT_CORS", False)
            and origin in current_app.config.get("WORD_ADDIN_ORIGINS", [])):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.vary.add("Origin")
    return response


@bp.errorhandler(ValueError)
def invalid_input(exc):
    db.session.rollback()
    return jsonify(error=str(exc)), 400


@bp.errorhandler(RedisError)
def unavailable(_exc):
    db.session.rollback()
    return jsonify(error="The Word service is temporarily unavailable. Please retry."), 503


@bp.errorhandler(RequestEntityTooLarge)
def request_too_large(_exc):
    return jsonify(error="The Word request is too large."), 413


def request_limit():
    from backend.word_addin.saved_documents import MAX_SAVE_REQUEST_BYTES
    return MAX_SAVE_REQUEST_BYTES if request.endpoint == "word_addin.save_document" else MAX_REQUEST_BYTES


def body():
    # Content-Length is normally present, but also check the cached bytes so a
    # chunked request cannot bypass the per-add-in limit.
    if len(request.get_data(cache=True)) > request_limit():
        raise RequestEntityTooLarge()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data


def project_for_user(project_id):
    if isinstance(project_id, bool) or not isinstance(project_id, int):
        raise ValueError("Choose a matter.")
    return Project.query.filter_by(id=project_id, userId=current_user.id).first()


def operation_for_user(public_id):
    return ChatThread.query.join(Project, ChatThread.projectId == Project.id).filter(
        ChatThread.public_id == public_id, Project.userId == current_user.id,
        ChatThread.docCategory == 69).first()


def entry(draft_id, key):
    if key == 'state':
        from backend.util.workspace_store import load
        return load(draft_id)
    row = TextEntry.query.filter_by(chat_thread_id=draft_id, textName=key).first()
    return json.loads(row.content) if row else None


def set_entry(draft_id, key, value):
    if key == 'state':
        raise ValueError('Use publish_workspace_result for a Word workspace result.')
    row = TextEntry.query.filter_by(chat_thread_id=draft_id, textName=key).first()
    if row is None:
        row = TextEntry(chat_thread_id=draft_id, textName=key)
        db.session.add(row)
    row.content = json.dumps(value, ensure_ascii=False)


def publish_workspace_result(draft_id, state, *, expected_version=None):
    """Stage this Word operation's own draft and result turn in its caller's transaction."""
    from backend.util import workspace_store
    ChatThread.query.filter_by(id=draft_id).with_for_update().one()
    current = workspace_store.load(draft_id, lock=True)
    workspace_store.update_document(draft_id, current['draft_document_id'], state['draft'],
        expected_version=current['draft_version'] if expected_version is None else expected_version)
    metadata = state.get('chat_history_meta', [])
    for index, turn in enumerate(state.get('chat_history', [])):
        meta = dict(metadata[index]) if index < len(metadata) else {}
        meta.update(chat_thread_id=current['chat_thread_id'], draft_document_id=current['draft_document_id'])
        workspace_store.append_turn(draft_id, turn[0], turn[1], meta=meta,
            turn_id=f'word:{draft_id}:{index}')
    workspace_store.patch_auxiliary(draft_id,
        {key: value for key, value in state.items() if key not in workspace_store.CANONICAL_KEYS})


def transition(draft_id, allowed, state, **values):
    row = db.session.execute(select(TextEntry).where(TextEntry.chat_thread_id == draft_id,
                             TextEntry.textName == "word.status").with_for_update()).scalar_one_or_none()
    if row is None:
        db.session.rollback()
        return False
    status = json.loads(row.content)
    if status["state"] not in allowed:
        db.session.rollback()
        return False
    row.content = json.dumps({**status, **values, "state": state})
    db.session.commit()
    return True


@bp.post("/auth/issue")
@authenticated
def issue_grant():
    data = body()
    claims = live_claims(bearer())
    if not claims:
        return {"error": "Your session expired. Sign in again."}, 401
    challenge = data.get("challenge", "")
    if not isinstance(challenge, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        raise ValueError("Invalid sign-in request.")
    r = redis_client()
    rate_key = f"word:auth-rate:{current_user.id}:{int(time.time() // 60)}"
    if r.incr(rate_key) > 20:
        return {"error": "Too many sign-in attempts. Wait a minute and retry."}, 429
    r.expire(rate_key, 65)
    code = secrets.token_urlsafe(32)
    r.setex("word:grant:" + hashlib.sha256(code.encode()).hexdigest(), 60,
            json.dumps({"challenge": challenge, "token": bearer(), "expires_at": claims["exp"]}))
    return {"code": code}


@bp.post("/auth/exchange")
def exchange_grant():
    data = body()
    code, verifier = data.get("code", ""), data.get("verifier", "")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code) or not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", verifier):
        return {"error": "Invalid or expired sign-in request."}, 401
    key = "word:grant:" + hashlib.sha256(code.encode()).hexdigest()
    raw = redis_client().eval(CONSUME, 1, key)
    if not raw:
        return {"error": "Invalid or expired sign-in request."}, 401
    grant = json.loads(raw)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    if not hmac.compare_digest(challenge, grant["challenge"]) or not live_claims(grant["token"]):
        return {"error": "Invalid or expired sign-in request."}, 401
    return {"access_token": grant["token"], "expires_at": grant["expires_at"]}


@bp.get("/workspace")
@authenticated
def workspace():
    projects = Project.query.filter_by(userId=current_user.id).order_by(Project.name).all()
    return {"user_id": str(current_user.id), "matters": [{"id": p.id, "name": p.name} for p in projects], "max_characters": MAX_TEXT}


@bp.get("/matters/<int:project_id>/sources")
@authenticated
def sources(project_id):
    if not project_for_user(project_id):
        return {"error": "Matter not found."}, 404
    files = UploadedFiles.query.filter_by(project_id=project_id, draft_id=None).order_by(UploadedFiles.original_filename).all()
    return {"sources": [{"id": f.unique_id, "name": f.original_filename} for f in files]}


def enqueue(draft_id):
    from backend.word_addin.tasks import word_operation
    word_operation.delay(draft_id)


@bp.post("/operations")
@authenticated
def submit():
    data = body()
    normalized = validate_request(data)
    project = project_for_user(data.get("project_id"))
    if not project:
        return {"error": "Matter not found."}, 404
    owned_files(project.id, normalized["file_ids"])
    if normalized.get("parent_operation_id"):
        parent = operation_for_user(normalized["parent_operation_id"])
        if not parent or parent.projectId != project.id:
            return {"error": "Previous Word task not found in this matter."}, 404
        if (entry(parent.id, "word.status") or {}).get("state") != "complete":
            raise ValueError("Only a completed task can be refined.")
        previous_request, previous_result = entry(parent.id, "word.request"), entry(parent.id, "word.result")
        if not previous_request or not previous_result:
            raise ValueError("The previous task is unavailable. Start a new task.")
        from backend.word_addin.refinement import bind_context
        normalized["refinement_context"] = bind_context(normalized, previous_request, previous_result)
    try:
        request_id = str(uuid.UUID(str(data.get("request_id", ""))))
    except ValueError:
        raise ValueError("Invalid request ID.") from None
    r = redis_client()
    lock_key = f"word:submit:{current_user.id}"
    lock = secrets.token_hex(16)
    if not r.set(lock_key, lock, nx=True, ex=30):
        return {"error": "Another request is starting. Please retry."}, 409
    active_key = f"word:active:{current_user.id}"
    idempotency_key = f"word:request:{current_user.id}:{request_id}"
    digest = hashlib.sha256(json.dumps([project.id, normalized], sort_keys=True).encode()).hexdigest()
    try:
        previous = r.get(idempotency_key)
        if previous:
            previous = json.loads(previous)
            if previous["digest"] != digest:
                return {"error": "Request ID already used for different content."}, 409
            return {"operation_id": previous["id"]}, 202
        if r.get(active_key):
            return {"error": "You have a Word task running. Finish or cancel it before starting another."}, 409
        draft = ChatThread(display_name=f"Word · {data['action'].replace('_', ' ').title()} · {normalized['document_name']}"[:240],
                       docCategory=69, projectId=project.id)
        db.session.add(draft)
        db.session.flush()
        from backend.util.workspace_store import initialize
        initialize(draft.id)
        set_entry(draft.id, "word.request", normalized)
        set_entry(draft.id, "word.status", {"state": "queued", "created_at": time.time(), "message": "Waiting for LexText…"})
        db.session.commit()
        r.setex(active_key, TTL, draft.public_id)
        r.setex(idempotency_key, 86400, json.dumps({"id": draft.public_id, "digest": digest}))
        try:
            enqueue(draft.id)
        except Exception:
            transition(draft.id, {"queued"}, "failed", message="The task could not start. Please retry.")
            r.eval(RELEASE, 1, active_key, draft.public_id)
            current_app.logger.warning("Word task enqueue failed", extra={"draft_id": draft.id})
        return {"operation_id": draft.public_id}, 202
    finally:
        r.eval(RELEASE, 1, lock_key, lock)


@bp.get("/operations/<public_id>")
@authenticated
def status(public_id):
    draft = operation_for_user(public_id)
    value = entry(draft.id, "word.status") if draft else None
    if not value:
        return {"error": "Word task not found."}, 404
    if value["state"] not in TERMINAL and time.time() - value["created_at"] > TTL:
        transition(draft.id, {"queued", "running"}, "failed", message="The task timed out. Try a smaller selection.")
        value = entry(draft.id, "word.status")
    return {**value, "operation_id": public_id,
            "result": entry(draft.id, "word.result") if value["state"] == "complete" else None}


@bp.post("/operations/<public_id>/cancel")
@authenticated
def cancel(public_id):
    draft = operation_for_user(public_id)
    if not draft or not entry(draft.id, "word.status"):
        return {"error": "Word task not found."}, 404
    transition(draft.id, {"queued", "running"}, "cancelled", message="Cancelled. Nothing was changed in Word.")
    # Keep the active lease until the worker exits; cancellation discards output
    # but does not forcibly kill a shared model request in progress.
    return {"state": entry(draft.id, "word.status")["state"]}


from backend.word_addin.saved_documents import register_saved_documents
register_saved_documents(bp, authenticated, body, entry, set_entry, operation_for_user)


def register_word_addin(app):
    app.config.setdefault("WORD_ADDIN_ENABLED", os.getenv("WORD_ADDIN_ENABLED", "").lower() in {"1", "true"})
    app.config.setdefault("WORD_ADDIN_EMIT_CORS", os.getenv("WORD_ADDIN_EMIT_CORS", "").lower() in {"1", "true"})
    origins = app.config.setdefault("WORD_ADDIN_ORIGINS", [s.strip() for s in os.getenv("WORD_ADDIN_ORIGINS", "").split(",") if s.strip()])
    for origin in origins:
        parsed = urlparse(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("WORD_ADDIN_ORIGINS must contain exact HTTPS origins without paths.")
    app.register_blueprint(bp)
