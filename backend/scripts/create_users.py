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


def prepare_user(email_str: str):
    # Set the parameters
    user_pool_id = COGNITO_USERPOOL
    temporary_password = generate_password(15)

    # Add the user to the Cognito user pool
    r = add_user_to_cognito(user_pool_id, email_str, temporary_password, email_str)
    if r is None:
        sys.exit

    cognito_username = r['User']['Username']
    print(f"user: {email_str} created with cognito_id: {cognito_username}, and password: {temporary_password}")

    # create new User in the DB with this cognito_username and email
    with app.app_context():
        new_user = User(email=email_str, contact_info='', cognito_username=cognito_username)
        db.session.add(new_user)
        db.session.commit()


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
            prepare_user(line)
        else:
            print(f"'{line}' is not a valid email address.")

if __name__ == "__main__":
    main()
