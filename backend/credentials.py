"""Explicit project-owned credentials; never consult the user's global AWS profiles."""
import configparser
import os
from backend.config import PRIVATE

def textract_client():
    import boto3
    config=configparser.RawConfigParser()
    config.read(PRIVATE/'aws/credentials')
    if not config.has_section('workspace'):
        raise RuntimeError('Copy AWS credentials into this project .private/aws/credentials profile [workspace] before cloud OCR.')
    values=config['workspace']
    if not values.get('aws_access_key_id') or not values.get('aws_secret_access_key'):
        raise RuntimeError('The project AWS credential profile is incomplete.')
    session=boto3.Session(aws_access_key_id=values['aws_access_key_id'],
        aws_secret_access_key=values['aws_secret_access_key'],aws_session_token=values.get('aws_session_token'),
        region_name=os.getenv('ZYX_AWS_REGION','us-east-1'))
    return session.client('textract')
