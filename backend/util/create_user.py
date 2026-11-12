import boto3
from botocore.exceptions import ClientError
import string
import random

from backend.models import User
from backend.constants import COGNITO_USERPOOL


def disable_cognito_user(username: str):
    # Create a Cognito Identity Provider (IdP) client
    client = boto3.client("cognito-idp")
    
    try:
        response = client.admin_disable_user(
            UserPoolId=COGNITO_USERPOOL,
            Username=username
        )
        print(f"Successfully disabled user '{username}'")
        return response
    except ClientError as e:
        print(f"Unable to disable user '{username}': {e}")
        raise


def enable_cognito_user(username: str):
    client = boto3.client("cognito-idp")
    try:
        response = client.admin_enable_user(
            UserPoolId=COGNITO_USERPOOL,
            Username=username
        )
        print(f"Successfully enabled user '{username}'")
        return response
    except ClientError as e:
        print(f"Unable to enable user '{username}': {e}")
        raise


def delete_cognito_user(username: str):
    client = boto3.client("cognito-idp")
    try:
        response = client.admin_delete_user(
            UserPoolId=COGNITO_USERPOOL,
            Username=username
        )
        print(f"Successfully deleted user '{username}'")
        return response
    except ClientError as e:
        print(f"Unable to delete user '{username}': {e}")
        raise


def add_user_to_cognito(user_pool_id, username, temporary_password, email):
    # Create a Boto3 client for Cognito Identity Provider
    client = boto3.client('cognito-idp')

    try:
        # Add the user to the Cognito user pool
        response = client.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            TemporaryPassword=temporary_password,
            UserAttributes=[
                {
                    'Name': 'email',
                    'Value': email
                },
                {
                    'Name': 'email_verified',
                    'Value': 'true'
                }
            ],
            # MessageAction='RESEND'  # 'SUPPRESS' Do not send an invitation email, set this to 'RESEND' to send it
        )
        print(f"User {username} added successfully.")
        return response
    except ClientError as e:
        print(f"Error adding user: {e}")
        return None


def generate_password(length=12):
    # Define character sets
    lowercase = string.ascii_lowercase
    uppercase = string.ascii_uppercase
    digits = string.digits
    special_characters = string.punctuation

    # Ensure the password meets all requirements
    password = [
        random.choice(lowercase),
        random.choice(uppercase),
        random.choice(digits),
        random.choice(special_characters)
    ]

    # Fill the rest of the password length with random characters
    if length > 4:
        password += random.choices(lowercase + uppercase + digits + special_characters, k=length-4)

    # Shuffle the password list to ensure randomness
    random.shuffle(password)

    # Convert the list to a string
    return ''.join(password)


def create_user_in_db(email_str: str, db) -> str:
    # Set the parameters
    user_pool_id = COGNITO_USERPOOL
    temporary_password = generate_password(15)

    # Add the user to the Cognito user pool
    r = add_user_to_cognito(user_pool_id, email_str, temporary_password, email_str)
    if r is None:
        return 'Error adding user to cognito'

    cognito_username = r['User']['Username']
    print(f"user: {email_str} created with cognito_id: {cognito_username}, and password: {temporary_password}")

    # create new User in the DB with this cognito_username and email
    new_user = User(
        email=email_str,
        contact_info='',
        cognito_username=cognito_username,
        is_disabled=False,
    )
    db.session.add(new_user)
    db.session.commit()
    return 'user created in db'
