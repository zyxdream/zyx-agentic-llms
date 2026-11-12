import pandas as pd
import sqlite3

def export_table_to_csv(db_file, table_name):
    conn = sqlite3.connect(db_file, isolation_level=None, detect_types=sqlite3.PARSE_COLNAMES)
    db_df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
    
    # cursor = conn.cursor()
    # cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    # print(cursor.fetchall())
    db_df.to_csv(f'{table_name}.csv', index=False)

if __name__ == "__main__":
    export_table_to_csv('../../myapp_remote_7_25.db', 'user')