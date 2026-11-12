from opensearchpy import OpenSearch
from backend.util.free_law_tools import _split_case_name, tokenize_side


def create_index(client):
    index_name = "citations"

    index_body = {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 1
        },
        "mappings": {
            "properties": {
                "row_id": { "type": "integer" },
                "cluster_id": { "type": "integer" },
                "citation": {"type": "text"},
                "type": { "type": "integer" },
                "case_name": { "type": "text" },
                "year_filed": { "type": "integer" },
                "case_name_a": {"type": "text"},
                "case_name_b": {"type": "text"}
            }
        }
    }

    client.indices.create(index=index_name, body=index_body)
    print(f"Index {index_name} created successfully")


# Connect to OpenSearch
client = OpenSearch(
    hosts=[{'host': '52.70.182.237', 'port': 9200}],
    http_auth=('admin', 'Pass234-FU'),  
    use_ssl=True, verify_certs=False  # Set to False if using self-signed cert
)



def _find_casename_opensearch(instr:str) -> str:
    """
    OpenSearch implementation of find_casename logic:
    - Boolean AND: all tokens in left_q must be in case_name_a
    - Boolean AND: all tokens in right_q must be in case_name_b
    - Sort by type (ascending) to get the best citation type
    """
    substr_a, substr_b = _split_case_name(instr)
    left_q = tokenize_side(substr_a)
    right_q = tokenize_side(substr_b)
    print("left_q: ", left_q)
    print("right_q: ", right_q)

    if not left_q and not right_q:
        return ""
    

    # Build the query
    must_clauses = []
    
    if right_q:
        # Both sides provided: each token must match its respective field
        for token in left_q:
            must_clauses.append({
                "match": {
                    "case_name_a": {
                        "query": token,
                        "operator": "and"
                    }
                }
            })
        for token in right_q:
            must_clauses.append({
                "match": {
                    "case_name_b": {
                        "query": token,
                        "operator": "and"
                    }
                }
            })
    else:
        # Only left side provided: search in both fields combined
        # We need to ensure ALL tokens appear in the combined case_name
        for token in left_q:
            must_clauses.append({
                "multi_match": {
                    "query": token,
                    "fields": ["case_name_a", "case_name_b"],
                    "operator": "or"  # token can be in either field
                }
            })
    print("must_clauses: ", must_clauses)
    query_body = {
        "size": 1,  # Only need the best match
        "query": {
            "bool": {
                "must": must_clauses
            }
        },
        "sort": [
            {"_score": {"order": "desc"}},  # Sort by relevance score FIRST
            {"type": {"order": "asc"}}       # Then by type (ascending) to get best citation
        ],
        "_source": ["citation", "type", "case_name", "case_name_a", "case_name_b", "year_filed"]
    }
    
    try:
        response = client.search(index="citations", body=query_body)
        hits = response.get("hits", {}).get("hits", [])
        
        if not hits:
            return ""
        
        best = hits[0]["_source"]
        print(f"OpenSearch best match: {best}")
        return best["citation"]
        
    except Exception as e:
        print(f"OpenSearch query failed: {e}")
        return ""



def _find_casenames_opensearch_batch(case_names: list[str]) -> list[str]:
    """
    Search for multiple case names in parallel using OpenSearch msearch API.
    Returns list of citations in the same order as input.
    """
    # Build msearch request body
    request_body = []
    for case_name in case_names:
        substr_a, substr_b = _split_case_name(case_name)
        left_q = tokenize_side(substr_a)
        right_q = tokenize_side(substr_b)
        
        if not left_q and not right_q:
            # Add empty query for this position to maintain order
            request_body.append({})
            request_body.append({"query": {"match_none": {}}})
            continue
        
        # Build must clauses for this case
        must_clauses = []
        if right_q:
            for token in left_q:
                must_clauses.append({
                    "match": {"case_name_a": {"query": token, "operator": "and"}}
                })
            for token in right_q:
                must_clauses.append({
                    "match": {"case_name_b": {"query": token, "operator": "and"}}
                })
        else:
            for token in left_q:
                must_clauses.append({
                    "multi_match": {
                        "query": token,
                        "fields": ["case_name_a", "case_name_b"],
                        "operator": "or"
                    }
                })
        
        # Add index header and query body
        request_body.append({"index": "citations"})
        request_body.append({
            "size": 1,
            "query": {"bool": {"must": must_clauses}},
            "sort": [
                {"_score": {"order": "desc"}},
                {"type": {"order": "asc"}}
            ],
            "_source": ["citation", "type", "case_name_a", "case_name_b"]
        })
    # Execute msearch
    try:
        response = client.msearch(body=request_body)
        results = []
        
        for resp in response['responses']:
            if 'error' in resp or not resp.get('hits', {}).get('hits'):
                results.append("")
            else:
                citation = resp['hits']['hits'][0]['_source']['citation']
                results.append(citation)
        
        return results
        
    except Exception as e:
        print(f"Multi-search failed: {e}")
        return [""] * len(case_names)


if __name__ == "__main__":

    # Check cluster info
    print(client.info())
    # Check if index exists
    if client.indices.exists(index="citations"):
        print("Index 'citations' exists")
    else:
        print("Index 'citations' does not exist")

        
    # # Print total document count
    try:
        count_resp = client.count(index="citations")
        total = count_resp.get('count', 0)
        print(f"Total documents in 'citations': {total}")
    except Exception as e:
        print(f"Failed to get count: {e}")

    # # Fetch and print the first document without filtering (internal order)
    # if total:
    #     try:
    #         resp = client.search(
    #             index="citations",
    #             body={
    #                 "size": 1,
    #                 "query": {"match_all": {}},
    #                 "sort": ["_doc"]  # efficient, returns first doc by internal order
    #             }
    #         )
    #         hits = resp.get("hits", {}).get("hits", [])
    #         if hits:
    #             doc = hits[0]
    #             print("First document id:", doc.get("_id"))
    #             print("First document source:", doc.get("_source"))
    #         else:
    #             print("No hits returned from index 'citations'.")
    #     except Exception as e:
    #         print(f"Failed to fetch first document: {e}")


    # response = client.search(
    #     index="citations",
    #     body={
    #         "query": {
    #             "match": {
    #             "case_name": "Carrillo v. Boise Tire Co. Inc."}
    #             }
    #     }       
    # )
    # print(response["hits"]["hits"])
    # Search
# # # print(find_casename("Osborn v. Salmon River Propane, LLC"))
# # print(find_casename("Carrillo v. Boise Tire Co."))
# best:  row_id                      4590962
# case_name_a                carrillo
# case_name_b    boise tire co., inc.
# citation              152 Idaho 741
# type                              2
# year_filed                     2012
# # print(find_casename("Whitted v. Canyon County"))
# # print(find_casename("Barry v. PacifiCorp"))
# print(find_casename("Perry v. Magic Valley Regional Medical Center"))
    # print(_find_casename_opensearch("Carrillo v. Boise Tire Co. Inc."))  # 152 Idaho 741  Carrillo v. BOISE TIRE CO., INC.
    # print(_find_casename_opensearch("Whitted v. Canyon County"))    #  'Whitted v. Canyon County Board of Commissioners' 2002 Ida. LEXIS 48
    # print(_find_casename_opensearch("Barry v. PacifiCorp"))    # ''
    # print(_find_casename_opensearch("Perry v. Magic Valley Regional Medical Center"))   # 134 Idaho 46  Perry v. Magic Valley Regional Medical Center


    # Usage:
    case_names = [
        "Carrillo v. Boise Tire Co. Inc.",
        "Whitted v. Canyon County",
        "Barry v. PacifiCorp",
        "Perry v. Magic Valley Regional Medical Center"
    ]
    citations = _find_casenames_opensearch_batch(case_names)
    for c in citations:
        print(c)
    # Returns: ['152 Idaho 741', '2002 Ida. LEXIS 48', '', '134 Idaho 46']