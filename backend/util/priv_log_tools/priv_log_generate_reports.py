
import boto3
import pandas as pd
import json

# [X] unescape JSON characters
# [X] pagination

# CLOUDSEARCH_BATCH_SIZE = 1000
CLOUDSEARCH_BATCH_SIZE = 1000


def decode_json_fields(data):
    for key in data:
        try:
            data[key] = json.loads(data[key])
        except (TypeError, json.JSONDecodeError, ValueError):
            pass
        if isinstance(data[key], list) and len(data[key]) == 1:
            data[key] = data[key][0]
    return data


def search_cloudsearch(domain, query, region='us-east-1'):
    client = boto3.client('cloudsearchdomain', endpoint_url=f"https://{domain}.cloudsearch.amazonaws.com")
    
    documents = []
    start = 0

    # response = client.search(query=query, queryParser='simple', size=CLOUDSEARCH_BATCH_SIZE) 
    # print(response.keys())
    # hits = response['hits']['hit']  # array of elements with 'id' and 'fields'
    # print(hits[0].keys())
    while True:
        try:
            param_d = {'return': 'identifier,source_filename'}
            response = client.search(query=query, queryParser='simple', start=start, size=CLOUDSEARCH_BATCH_SIZE, **param_d)  # Fetch all fields
            hits = response['hits']['hit']
            documents.extend(hits)

            # Check if there are more documents to fetch
            if len(hits) < CLOUDSEARCH_BATCH_SIZE:
                break
            start += CLOUDSEARCH_BATCH_SIZE

        except Exception as e:
            print(f"An error occurred: {e}")
            break

    return documents

def add_id(result_arr: list) -> list:
    """Yes, I called it an array and added the list type.  """
    for el in result_arr:
        el['fields']['id'] = el['id']


def results_to_csv(results, filename):
    print(f'we have {len(results)} results')
    # for item in results:
    #     print(item['fields'])
    add_id(results)
    data = [decode_json_fields(item['fields']) for item in results]
    df = pd.DataFrame(data)
    df.to_csv(filename, index=False)
    print(f"Data written to {filename}")


def create_file_for_query(output_filename: str, query: str) -> None:
    domain = 'doc-lexprivilege2-wsgzalyyr4wbekc7zl55qoxppq.us-east-1'

    # Perform the search
    results = search_cloudsearch(domain, query)

    # Write results to a file
    results_to_csv(results, output_filename)
    print(f"Results have been written to {output_filename}")


if __name__ == "__main__":
    q = 'goodwinlaw* || holcombward* || lw.com'
    create_file_for_query('goodwinlaw-OR-holcombward-or-lw.csv', q) 
    q = '"attorney-client privilege"'
    create_file_for_query('attorney-client-privilege.csv', q)
    q = '"attorney work product"'
    create_file_for_query('attorney-work-product.csv', q)   
    q = 'kdvlaw* || acceleronlaw* || agg.com || allenmatkins*'
    create_file_for_query('kdvlaw-acceleronlaw-agg-allenmatkins.csv', q)
    q = 'pillsburylaw* || wsgr.com || adlercolvin*'
    create_file_for_query('pillsburylaw-wsgr-adlercolvin.csv', q)
