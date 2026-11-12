
"""
0.  takes in a list of "privelidged emails", puts them in a map (priv_map).
1. Downloads list of mail files from s3 bucket
2. for each file opens and rewrites all records except those in the priv_map.
3. upload new file to a different s3 bucket
"""

import pandas as pd

from priv_log_tools import process_pst_file

IN_BUCKET = 'lexintel-file-transfer'
OUT_BUCKET = 'lexintel-priv-log-filtered'

PRIVLIGED_FILES = ['attorney-client-privilege.csv', 
                'kdvlaw-acceleronlaw-agg-allenmatkins.csv',
                'attorney-work-product.csv',
                'pillsburylaw-wsgr-adlercolvin.csv',
                'goodwinlaw-OR-holcombward-or-lw.csv']

def load_priv_map() -> dict:
    """The priv_map should ids as keys and filename_identifier as the values"""
    data_frames = []
    for fn in PRIVLIGED_FILES:
        data_frames.append(pd.read_csv(fn))

    combined_df = pd.concat(data_frames, ignore_index=True)

    combined_df.to_csv('priv_map.csv')
    


if __name__ == "__main__":
    # fn = "/Users/pbharrin/Downloads/FINRARequestJun12021Aug17202312_384903_Email_025.pst"
    # fn = "/Users/pbharrin/Downloads/FINRARequestJun12021Aug17202322_384906_IM_001.pst"
    # process_pst_file(fn, 'temp', 0)

    load_priv_map()
