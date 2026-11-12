
import pypff
import re
import textract
# import pandas as pd
import os
from dotenv import load_dotenv
import boto3
from openai import OpenAI
import magic
from email import encoders
from typing import Tuple
from pypdf import PdfReader
import csv

MAX_ENTRIES = 1000
MAX_LEN = 16385 * 2  # max len in characters 
FINAL_PRIV_MAP = "priv_map_full.csv"

FTYPES = {'image/png': '.png',
          'application/pdf': '.pdf',
          'text/plain': '.txt',
          'image/jpeg': '.jpg',
          'image/gif': '.gif',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx'}

load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def get_openai_summary(email_content) -> str:
    prompt = "Write an overview of the subject matter for the following email in one short sentence.  Do not use the sender or reciepient's names, simple refer to them as sender/reciever. : \n\n" + email_content
    completion = client.chat.completions.create(
        model='gpt-5.5',
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    msg = completion.choices[0].message
    return msg.content

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


def get_text_from_pdf(fn: str) -> str:
    reader = PdfReader(fn)
    docket_text = ""

    # loop over every Page in the PDF and get the text out.
    for i in range(len(reader.pages)):
        page = reader.pages[i]
        page_text = page.extract_text()
        docket_text += page_text
        
    return docket_text


def get_text_from_file(filepath: str) -> str:
    """Extracts and cleans up text from an uploaded legal complaint."""
    filename = filepath.split('/')[-1]
    f_type = filename.rsplit('.', 1)[1].lower()

    if f_type == 'txt':
        return open(filepath).read()
    elif f_type == 'pdf':
        return get_text_from_pdf(filepath)
    elif f_type == 'doc' or f_type == 'docx':
        text = textract.process(filepath)
        return text.decode('utf-8')
    else:
        raise Exception("Unsupported file type")


READABLE_TYPES = {'application/pdf', 'text/plain', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}

def attachment_summary(message) -> Tuple[str, str]:
    """Creates a summary of any attachments"""
    # attach attachments if any
    num_attach = get_number_of_attachemnts(message=message)

    ret_str = f"There are {num_attach} attachments.  "
    type_counts = {'image/png': 0, 'application/pdf': 0, 'text/plain': 0, 'application/zip': 0}

    readable_content = []
    for i in range(num_attach):
        attachment_i = message.attachments[i]
        binary_content = get_binary_conent(attachment_i)
        if binary_content is not None:
            mime = magic.Magic(mime=True)
            file_type = mime.from_buffer(binary_content)
            print(f"this file is type: {file_type}")
            type_counts[file_type] = type_counts.get(file_type, 0) + 1

            if file_type in READABLE_TYPES:
                ftype = FTYPES[file_type]
                fname = f'temp{ftype}'
                with open(fname, 'wb') as file:
                    file.write(binary_content)
                readable_content.append(get_text_from_file(fname))

    # get a summary of any documents of type: pdf, txt, word
    doc_summary = ""
    if len(readable_content) > 0:
        all_readable_content =" ".join(readable_content)
        doc_summary = get_openai_summary(cap_text_len(all_readable_content))

    if num_attach > 0:
        for ft, cnts in type_counts.items():
            if cnts > 0:
                ret_str += f"{cnts} file(s) of type {ft}, "

    return ret_str, doc_summary


def cap_text_len(instr: str) -> str:
    """Caps the maximum size of a utf-8 encoded string."""
    if len(instr) <= MAX_LEN:
        return instr
    else:
        return instr[:MAX_LEN]


def extract_folder_items(folder, name, out_messages, i_off, priv_m):
    # Recursive function to print folder contents
    for folder in folder.sub_folders:
        sm = folder.number_of_sub_messages
        # print(folder.name + f" sm: {sm}, ne: {ne}")
        extract_folder_items(folder, name=name, out_messages=out_messages, i_off=i_off, priv_m=priv_m)
        # print(dir(folder))

        for i in range(sm):

            current_i = len(out_messages) + i_off
            if current_i in priv_m:
                print("hell yeah, found a privlidged doc!  ")
                priv_map_details = priv_m[current_i]
                print(priv_map_details)
                message = folder.get_sub_message(i)
                assert priv_map_details[0] == name
                assert int(priv_map_details[1]) == message.identifier

                # write these to a dict for export
                ret_d = {}
                # print(f"source filename: {name}")
                ret_d["source_filename"] = name
                # print(f"Identifier: {message.identifier}")
                ret_d["identifier"] = message.identifier
                # print(f"Delivery time: {message.get_delivery_time()}")
                ret_d['delivery_time'] = message.get_delivery_time().strftime('%Y-%m-%d %H:%M:%S')
                # print(f"Subject: {message.subject}")
                ret_d['subject'] = message.subject or "No Subject"
                # print(f"Body: {message.plain_text_body}")  
                body_text = str(message.plain_text_body)
                ret_d['summary'] = get_openai_summary(cap_text_len(body_text))
                print(ret_d['summary'])
                # print('------------   start transport headers  -------------')
                # print(message.transport_headers)
                from_s, to_s, cc_s = parse_transport_header(message.transport_headers)
                if to_s is None:
                    print('None to_s value')
                ret_d['from'] = from_s.strip().replace('\r\n', '').replace('\n', '')
                ret_d['to'] = to_s.strip().replace('\r\n', '').replace('\n', '')
                ret_d['cc'] = cc_s.strip().replace('\r\n', '').replace('\n', '')
                ret_d['id'] = str(current_i)
                ret_d['notes'] = priv_map_details[2]
                summary, details = attachment_summary(message)
                ret_d['attachment_summary'] = summary
                ret_d['attachment_details'] = details

                out_messages.append(ret_d)
            else:
                out_messages.append({})

 
def split_array(large_array, max_length=MAX_ENTRIES):
    """Split a large array into smaller arrays of maximum length max_length."""
    # Initialize an empty list to hold the smaller arrays
    smaller_arrays = []

    # Iterate through the large_array in steps of max_length
    for i in range(0, len(large_array), max_length):
        # Extract a slice of max_length and append to smaller_arrays
        smaller_arrays.append(large_array[i:i + max_length])

    return smaller_arrays


def process_pst_file(file_path: str, out_file_dir: str, index_offset:int, priv_m: dict):
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



def download_file(bucket_name, object_name, local_file_path):
    s3 = boto3.client('s3')
    s3.download_file(bucket_name, object_name, local_file_path)
    print(f"Downloaded {object_name} to {local_file_path}")

def load_priv_map():
    fr = open("priv_map_full.csv")
    fr.readline()

    ret_map = {}
    for line in fr.readlines():
        la = line.strip().split(',')
        ret_map[int(la[3])] = (la[1], la[2], la[4])

    return ret_map


def write_csv(file_path: str, data: list) -> None:
    # Writing data to the CSV file
    with open(file_path, mode='w', newline='') as file:
        # Get the field names from the keys of the first dictionary
        fieldnames = data[0].keys()
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

    print(f"CSV file created at {file_path}")


if __name__ == "__main__":
    priv_map = load_priv_map()

    s3_bucket_name = 'lexintel-file-transfer'
    base_folder = "/home/ubuntu/"
    base_folder = "/Users/pbharrin/Downloads/"
    output_folder = base_folder + "temp_pst"

    # priv_rows = process_pst_file("/Users/pbharrin/Downloads/FINRARequestJun12021Aug17202312_384903_Email_001.pst", 
    #                  output_folder, 0, priv_map)
    
    # df_out = pd.DataFrame(list(filter(lambda x: bool(x), priv_rows)))
    # df_out.to_csv(output_folder + f"/{0}.csv", index=False)

    # get files to download
    files_in_bucket = list_files_in_bucket(s3_bucket_name)
    for i, fn in enumerate(files_in_bucket):
        if i < 39:
            continue

        print(f"Downloading: {fn}, file number: {i}")

        # download file, then process it locally
        download_file(s3_bucket_name, fn, base_folder + fn)
        priv_rows = process_pst_file(base_folder + fn, output_folder, i*100000, priv_map)
        
        # delete single file
        os.remove(base_folder + fn)
        
        # create a CSV from all the rows, and write to disk
        non_empty = list(filter(lambda x: bool(x), priv_rows))

        if len(non_empty) > 0:
            write_csv(output_folder + f"/{i}.csv", non_empty)
