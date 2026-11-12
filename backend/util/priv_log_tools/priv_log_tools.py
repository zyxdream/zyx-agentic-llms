import pypff
import re
import json
import os

import boto3

MAX_ENTRIES = 1000
MAX_BYTES = 1048576
MAX_BYTES08 = int(0.8*MAX_BYTES)

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


def extract_folder_items(folder, name, out_messages, i_off):
    # Recursive function to print folder contents
    for folder in folder.sub_folders:
        sm = folder.number_of_sub_messages
        # print(folder.name + f" sm: {sm}, ne: {ne}")
        extract_folder_items(folder, name=name, out_messages=out_messages, i_off=i_off)
        # print(dir(folder))

        for i in range(sm):

            message = folder.get_sub_message(i)
            print(dir(message))
            # write these to JSON
            ret_d = {}
            # print(f"source filename: {name}")
            ret_d["source_filename"] = name
            # print(f"Identifier: {message.identifier}")
            ret_d["identifier"] = message.identifier
            # print(f"Delivery time: {message.get_delivery_time()}")
            ret_d['delivery_time'] = message.get_delivery_time().strftime('%Y-%m-%d %H:%M:%S')
            # print(f"Subject: {message.subject}")
            ret_d['subject'] = clean_banned_unicode(message.subject or "No Subject")
            # print(f"Body: {message.plain_text_body}")  
            body_text = clean_banned_unicode(str(message.plain_text_body))
            ret_d['body'] = cap_text_len(body_text, MAX_BYTES)
            # print('------------   start transport headers  -------------')
            # print(message.transport_headers)
            from_s, to_s, cc_s = parse_transport_header(message.transport_headers)
            if to_s is None:
                print('None to_s value')
            ret_d['from'] = from_s
            ret_d['to'] = to_s
            ret_d['cc'] = cc_s

            wrap_d = {"type": "add", "id": str(len(out_messages) + i_off), "fields": ret_d}
            out_messages.append(wrap_d)

 
def split_array(large_array, max_length=MAX_ENTRIES):
    """Split a large array into smaller arrays of maximum length max_length."""
    # Initialize an empty list to hold the smaller arrays
    smaller_arrays = []

    # Iterate through the large_array in steps of max_length
    for i in range(0, len(large_array), max_length):
        # Extract a slice of max_length and append to smaller_arrays
        smaller_arrays.append(large_array[i:i + max_length])

    return smaller_arrays


def process_pst_file(file_path: str, out_file_dir: str, index_offset:int):
    name = file_path.split('/')[-1]
    out_name = name.split('.')[0]
    root_folder = open_pst(file_path)

    all_messages = []
    extract_folder_items(root_folder, name, all_messages, index_offset)

    print(f"we have {len(all_messages)} records")
    # with open(f'{out_name}.json', 'w') as file:
    #     json.dump(all_messages, file)

    if len(all_messages) > MAX_ENTRIES:
        sub_arrays = split_array(all_messages)
    else:
        sub_arrays = [all_messages]

    sub_files_written = []
    for i, sub_array in enumerate(sub_arrays):
        sub_file_name = f'{out_file_dir}/{out_name}_{i}.json'
        with open(sub_file_name, 'w') as file:
            json.dump(sub_array, file)
            sub_files_written.append(sub_file_name)

    return sub_files_written

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


if __name__ == "__main__":
    # output_folder = "/Users/pbharrin/Downloads/temp_pst"
    # process_pst_file("/Users/pbharrin/Downloads/FINRARequestJun12021Aug17202312_384903_Email_025.pst", 
    #                  "/Users/pbharrin/Downloads/temp_pst", 0)
    
    # domain_client = boto3.client(
    #     'cloudsearchdomain',
    #     endpoint_url='https://doc-lexprivilege-qrf3smcihaziwyq2zsl344d6yu.us-east-1.cloudsearch.amazonaws.com'
    # )
    # for fn in os.listdir(output_folder):
    #     print(f'Processing: {fn}')
    #     documents = open(f"{output_folder}/{fn}").read()
    #     response = domain_client.upload_documents(
    #         documents=documents,
    #         contentType='application/json'
    #     )
    #     print(f"response: ", response)

    # get files to download
    s3_bucket_name = 'lexintel-file-transfer'
    base_folder = "/home/ubuntu/"
    base_folder = "/Users/pbharrin/Downloads/"
    output_folder = base_folder + "temp_pst"

    files_in_bucket = list_files_in_bucket(s3_bucket_name)
    for i, fn in enumerate(files_in_bucket):
        print(f"Downloading: {fn}, file number: {i}")

        # download file, then process it locally

        download_file(s3_bucket_name, fn, base_folder + fn)
        process_pst_file(base_folder + fn, output_folder, i*100000)
        
        # delete single file
        os.remove(base_folder + fn)
        
        # index smaller files
        domain_client = boto3.client(
            'cloudsearchdomain',
            endpoint_url='https://doc-lexprivilege2-wsgzalyyr4wbekc7zl55qoxppq.us-east-1.cloudsearch.amazonaws.com'
            #endpoint_url='https://doc-lexprivilege-qrf3smcihaziwyq2zsl344d6yu.us-east-1.cloudsearch.amazonaws.com'
        )

        # for fn in os.listdir(output_folder):
        #     print(f'Processing: {fn}')
        #     documents = open(f"{output_folder}/{fn}").read()
        #     response = domain_client.upload_documents(
        #         documents=documents,
        #         contentType='application/json'
        #     )
        #     print(f"response: ", response)

        # remove
        cleanup_temp_dir(output_folder)