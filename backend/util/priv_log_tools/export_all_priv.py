import mailbox
from email.message import EmailMessage, MIMEPart
from email import message_from_string, encoders
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.application import MIMEApplication

import pypff
import re
import json
import os
import magic
import boto3

MAX_ENTRIES = 1000
MAX_BYTES = 1048576
MAX_BYTES08 = int(0.8*MAX_BYTES)

HOLDER_EMAIL = """From: sender@example.com
To: recipient@example.com
Subject: Test Email
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Hello, this is a test email."""

FTYPES = {'image/png': '.png',
          'application/pdf': '.pdf',
          'text/plain': '.txt',
          'image/jpeg': '.jpg',
          'image/gif': '.gif',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx'}

def open_pst(file_path):
    # Open the PST file
    pst = pypff.open(file_path)
    root = pst.get_root_folder()
    if root is None:
        return "No root folder found."
    return root

def parse_transport_header(tp_text: str):
    # Regular expression to find the From field
    from_field = re.search(r"From: (.+)", tp_text)
    if from_field:
        from_value = from_field.group(1)
        # print("From:", from_value)

        # Regular expression to find the To field
    # Improved pattern to stop at the next header by detecting the start of a new header line
    TO_REGEX = r"\nTo: ((?:.+\n\s*(?:,\s*)?)+)"
    CC_REGEX = r"\nCc: ((?:.+\n\s*(?:,\s*)?)+)"
    def find_field(pat: str) -> str:
        to_value = 'None'
        to_field = re.search(pat, tp_text)
        if to_field:
            # Clean and split the results to list each recipient separately
            recipients = re.split(r",\s*\n\s*", to_field.group(1).strip())
            # print("To:")
            recipients_a = []
            for i, recipient in enumerate(recipients):
                if i == len(recipients) - 1:
                    recipient = recipient.split('\n')[0].strip()
                recipients_a.append(recipient)
                # print("--->", recipient, "<---")
            to_value = ",".join(recipients_a)

        return to_value
    
    to_value_o = find_field(TO_REGEX)
    cc_value = find_field(CC_REGEX)

    return from_value, to_value_o, cc_value


def clean_banned_unicode(instr: str) -> str:
    """removes AWS banned unicode chars."""
    pattern = r'[^\u0009\u000a\u000d\u0020-\uD7FF\uE000-\uFFFD]'
    
    # Substitute matched characters with an empty string
    cleaned_text = re.sub(pattern, '', instr)
    
    return cleaned_text


def cap_text_len(instr: str, max_bytes=MAX_BYTES) -> str:
    """Caps the maximum size of a utf-8 encoded string."""
    byte_length = len(json.dumps(instr).encode('utf-8'))
    if byte_length <= max_bytes:
        return instr
    else:
        return instr[:10000]

def load_priv_map():
    fr = open("priv_map_full.csv")
    fr.readline()

    ret_map = {}
    for line in fr.readlines():
        la = line.strip().split(',')
        ret_map[int(la[3])] = (la[1], la[2])

    return ret_map

# def get_encoding(ctype_str: str) -> str:
#     encoding_start = ctype_str.find('charset=')
#     if encoding_start == -1:
#         return None
#     return ctype_str[encoding_start+8:].lower().replace('"', '')

def copy_headers(orig, target) -> None:
    """In place copy of heders from orig to target."""
    for key, value in orig.items():
        try:
            target[key] = value
        except ValueError as ve:
            ve_str = str(ve)
            if ve_str.startswith("There may be at most"):
                print('duplicate headers, skipping update')
                continue
            target[key] = re.sub(r'\s*\n\s*', " ", value)


def get_number_of_attachemnts(message) -> int:
    """The default method raises OSError.  """
    try:
        n = message.get_number_of_attachments()
    except:
        n = 0
    return n


def get_binary_conent(attachment):
    """Default method can raise OSError."""
    try:
        bin_content = attachment.read_buffer(attachment.size)
    except:
        bin_content = None
    return bin_content


def copy_attachments(message, new_ret_msg) -> None:
    """Copy in place any attachments"""
    # attach attachments if any
    num_attach = get_number_of_attachemnts(message=message)

    for i in range(num_attach):
        attachment_i = message.attachments[i]
        binary_content = get_binary_conent(attachment_i)
        if binary_content is not None:
            mime = magic.Magic(mime=True)
            file_type = mime.from_buffer(binary_content)
            print(f"this file is type: {file_type}")
            # Create a MIMEApplication object
            # mime_app = MIMEApplication(binary_content, Name=f'cat.txt')
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(binary_content)
            encoders.encode_base64(part)
            suffix = FTYPES.get(file_type, '')
            filename = f"{attachment_i.identifier}{suffix}"
            print(f"filename: {filename}")
            part['Content-Disposition'] = f'attachment; filename="{filename}"'
            new_ret_msg.attach(part)


def extract_folder_items(folder, name, out_messages, i_off, priv_m):
    # Recursive function to print folder contents
    for folder in folder.sub_folders:
        sm = folder.number_of_sub_messages
        extract_folder_items(folder, name=name, out_messages=out_messages, i_off=i_off, priv_m=priv_m)

        for i in range(sm):
            
            message = folder.get_sub_message(i)
            # if i > 10025: 
            #     break

            current_i = len(out_messages) + i_off
            if current_i in priv_m:
                print("holy shit found a matching doc")
                print(priv_m[current_i])
                header_only_msg = message_from_string(message.transport_headers)
                print(f"email number: {i}", header_only_msg['Content-Type'])
                assert int(priv_m[current_i][1]) == message.identifier
                assert priv_m[current_i][0] == name

                # set content
                encoding_type = header_only_msg.get_content_charset()
                if encoding_type is None:
                    encoding_type = 'utf-8'
                content_type = header_only_msg.get_content_type()
                # print(f'encoding type: {encoding_type}')
                if content_type == 'multipart/alternative':
                    ret_msg = EmailMessage()
                    copy_headers(header_only_msg, ret_msg)
                    if message.plain_text_body is not None:
                        ret_msg.add_alternative(message.plain_text_body.decode(encoding_type, 'ignore'), subtype='plain')
                    if message.html_body is not None:
                        ret_msg.add_alternative(message.html_body.decode(encoding_type, 'ignore'), subtype='html',)
                elif content_type == 'multipart/mixed':
                    ret_msg = MIMEMultipart('mixed')
                    copy_headers(header_only_msg, ret_msg)
                    if message.plain_text_body is not None:
                        text_part = MIMEText(message.plain_text_body.decode(encoding_type, 'ignore'), 'plain')
                        ret_msg.attach(text_part)

                    if message.html_body is not None:
                        html_part = MIMEText(message.html_body.decode(encoding_type, 'ignore'), 'html')
                        ret_msg.attach(html_part)

                    copy_attachments(message=message, new_ret_msg=ret_msg)

                elif content_type == 'multipart/related':
                    ret_msg = MIMEMultipart('related')
                    copy_headers(header_only_msg, ret_msg)
                    # assume it has an HTML body
                    html_part = MIMEText(message.html_body.decode(encoding_type, 'ignore'), 'html')
                    ret_msg.attach(html_part)

                    copy_attachments(message=message, new_ret_msg=ret_msg)

                elif content_type == 'multipart/report':
                    ret_msg = MIMEMultipart('report')
                    copy_headers(header_only_msg, ret_msg)
                    # assume it is plain text only
                    text_part = MIMEText(message.plain_text_body.decode(encoding_type, 'ignore'), 'plain')
                    ret_msg.attach(text_part)

                    ret_msg
                    
                elif content_type == 'text/html':
                    ret_msg = EmailMessage()
                    copy_headers(header_only_msg, ret_msg)
                    ret_msg.set_content(message.html_body.decode(encoding_type, 'ignore'), subtype='html')
                elif content_type == 'text/plain':
                    ret_msg = EmailMessage()
                    copy_headers(header_only_msg, ret_msg)
                    if message.plain_text_body is not None:
                        ret_msg.set_content(message.plain_text_body.decode(encoding_type, 'ignore'), subtype='plain')
                elif content_type == 'text/signed' or content_type == 'multipart/signed':
                    ret_msg = MIMEMultipart('mixed')
                    copy_headers(header_only_msg, ret_msg)
                    if message.plain_text_body is not None:
                        text_part = MIMEText(message.plain_text_body.decode(encoding_type, 'ignore'), 'plain')
                        ret_msg.attach(text_part)

                    if message.html_body is not None:
                        html_part = MIMEText(message.html_body.decode(encoding_type, 'ignore'), 'html')
                        ret_msg.attach(html_part)

                    copy_attachments(message=message, new_ret_msg=ret_msg)
                    
                else:
                    raise Exception(f"Uknown content type: {content_type}")
                # could get the charset from the header: "Content-Type"
                # the first line of Content-Type is: 
                # multipart/alternative, multipart/related, multipart/mixed, text/html, text/plain, 
            else:
                ret_msg = message_from_string(HOLDER_EMAIL)

            out_messages.append(ret_msg)


def process_pst_file(file_path: str, out_file_dir: str, index_offset: int, priv_m: dict):
    name = file_path.split('/')[-1]
    out_name = name.split('.')[0]
    root_folder = open_pst(file_path)

    all_messages = []
    extract_folder_items(root_folder, name, all_messages, index_offset, priv_m)

    print(f"we have {len(all_messages)} records")

    return all_messages

#  ***************    AWS Code

def list_files_in_bucket(bucket_name):
    s3 = boto3.client('s3')
    ret_l = []

    continuation_token = None
    while True:
        # Get a batch of objects from the bucket
        if continuation_token:
            response = s3.list_objects_v2(Bucket=bucket_name, ContinuationToken=continuation_token)
        else:
            response = s3.list_objects_v2(Bucket=bucket_name)
        
        # Check if the bucket has any objects
        if 'Contents' in response:
            for item in response['Contents']:
                # print(item['Key'])
                ret_l.append(item['Key'])
        
        # Check if more objects are available
        if response.get('IsTruncated'):  # True if more results are available
            continuation_token = response['NextContinuationToken']
        else:
            break

    return ret_l


def cleanup_temp_dir(folder_path: str):
    try:
        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
                print(f"Deleted {file_path}")
            else:
                print(f"Skipped {file_path}")
    except OSError as e:
        print(f"Error: {e.strerror}")


def download_file(bucket_name, object_name, local_file_path):
    s3 = boto3.client('s3')
    s3.download_file(bucket_name, object_name, local_file_path)
    print(f"Downloaded {object_name} to {local_file_path}")


def upload_file_to_s3(local_file, bucket_name, s3_file):
    """
    Upload a file to an AWS S3 bucket.

    Args:
    local_file (str): Path to the local file to upload.
    bucket_name (str): The S3 bucket to upload to.
    s3_file (str): The object name under which the file is stored in S3.

    Returns:
    bool: True if file was uploaded, else False
    """
    # Create an S3 client
    s3_client = boto3.client('s3')

    try:
        # Upload the file to S3
        s3_client.upload_file(local_file, bucket_name, s3_file)
        print(f"File {s3_file} uploaded successfully")
        return True
    except Exception as e:
        print(f"Failed to upload file: {e}")
        return False
    

if __name__ == "__main__":
    priv_map = load_priv_map()

    # get files to download
    s3_bucket_name = 'lexintel-file-transfer'
    base_folder = "/home/ubuntu/"
    base_folder = "/Users/pbharrin/Downloads/"
    output_folder = base_folder + "temp_pst"

    all_records = []
    files_in_bucket = list_files_in_bucket(s3_bucket_name)
    for i, fn in enumerate(files_in_bucket):
        print(f"Downloading: {fn}, file number: {i}")
        if i < 4:
            continue

        # download file, then process it locally
        download_file(s3_bucket_name, fn, base_folder + fn)
        email_records = process_pst_file(base_folder + fn, output_folder, i*100000, priv_map)
        for record in email_records:
            if record['From'] == "sender@example.com":
                continue
            all_records.append(record)

        email_records.clear()  # save memory 
        # delete single file
        os.remove(base_folder + fn)
        
    # create MBOX file here
    output_fn = output_folder + f'/all_priv.mbox'
    mbox = mailbox.mbox(output_fn)
    mbox.lock()

    for i, record in enumerate(all_records):  # remove dummy emails
        mbox.add(record)
        if i % 1000 == 0:
            print("added 1000") 

    mbox.flush()
    mbox.unlock()

    # upload MBOX file to S3 bucket lex-intel-priv-mbox
    upload_file_to_s3(output_fn,"lexintel-priv-log-priv", f'all_privileged.mbox')

    # remove
    cleanup_temp_dir(output_folder)

