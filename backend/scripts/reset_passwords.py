import boto3
from botocore.exceptions import ClientError
import string
import random
import sys
import re
from backend.database import db
from backend.app import app
from backend.models import User
from backend.constants import COGNITO_USERPOOL
from backend.util.create_user import generate_password, add_user_to_cognito


def prepare_user(cognito_id: str):
    # Set the parameters
    client = boto3.client("cognito-idp")

    temp_password = generate_password()

    try:
        resp = client.admin_set_user_password(
            UserPoolId=COGNITO_USERPOOL,
            Username=cognito_id,
            Password=temp_password,
            Permanent=False,
        )
        print("Success. Password set (temporary, change required).")
        return resp

    except client.exceptions.UserNotFoundException:
        print(f"Error: user '{cognito_id}' not found in pool {COGNITO_USERPOOL}.")
    except client.exceptions.InvalidParameterException as e:
        print(f"Invalid parameters: {e}")
    except client.exceptions.NotAuthorizedException as e:
        print(f"Not authorized (check IAM permissions): {e}")
    except client.exceptions.TooManyRequestsException:
        print("Throttled by Cognito. Try again with backoff.")
    except ClientError as e:
        # Generic catch-all for any other service/client errors
        print(f"AWS ClientError: {e.response.get('Error', {}).get('Message', e)}")



def referesh_password(cognito_user_id: str):
    pass

def get_cognito_id(email_address: str) -> str:
    """
    Look up a user's Cognito username from the database by email address.

    Args:
        email (str): The email address to look up.

    Returns:
        str | None: The cognito_username if found, otherwise None.
    """
    with app.app_context():
        user = User.query.filter_by(email=email_address).first()
        if user:
            return user.cognito_username
        return None


def is_email(address) -> bool:
    """Simple regex to validate an email address."""
    email_regex = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(email_regex, address)

def main():
    print("Enter an email address or a blank line to quit:")
    
    for line in sys.stdin:
        line = line.strip()
        if not line:  # If the input is a blank line, quit the program
            print("Exiting...")
            break
        
        if is_email(line):
            cog_id = get_cognito_id(line)
            if cog_id is None:
                print(f"could not find the user: {line} in the database.")
            else:
                prepare_user(cog_id)
        else:
            print(f"'{line}' is not a valid email address.")

if __name__ == "__main__":
    main()
