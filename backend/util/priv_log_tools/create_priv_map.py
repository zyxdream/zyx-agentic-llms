import pandas as pd

FN = '/Users/pbharrin/Downloads/20240726_output.csv'
TARGET_CATEGORY = "Privileged (in full)"
TC2 = "Privileged (in full) - calendar invitation"
TC3 = "Privileged (in full) - common interest"
FINAL_PRIV_MAP = "priv_map_full.csv"

if __name__ == "__main__":


    out_csv = pd.read_csv(FN)

    target_records = []
    for row in out_csv.itertuples(index=True, name='Pandas'):
        if row.Final_Category == TARGET_CATEGORY or row.Final_Category == TC2:
            target_records.append({'source_filename': row.source_filename,
                                   "identifier": row.identifier,
                                   "id": row.id,
                                   "note": ""})
        elif row.Final_Category == TC3:
            target_records.append({'source_filename': row.source_filename,
                        "identifier": row.identifier,
                        "id": row.id,
                        "note": "Common Interest"})
            

    df = pd.DataFrame(target_records)
    df.to_csv(FINAL_PRIV_MAP)