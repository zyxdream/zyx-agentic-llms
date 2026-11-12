
import pandas as pd
import os

if __name__ == "__main__":
    # Directory containing the CSV files
    directory_path = 'temp_pst'

    # List to hold the DataFrames
    dfs = []

    # Loop through the CSV files in the directory
    for filename in os.listdir(directory_path):
        if filename.endswith('.csv'):
            file_path = os.path.join(directory_path, filename)
            # Read each CSV file into a DataFrame and append it to the list
            dfs.append(pd.read_csv(file_path))

    # Concatenate all DataFrames in the list into a single DataFrame
    merged_df = pd.concat(dfs, ignore_index=True)

    # Write the merged DataFrame to a new CSV file
    merged_df.to_csv('merged_output.csv', index=False)

    print("CSV files have been merged successfully into 'merged_output.csv'.")

