import pandas as pd
REQUIRED_COLUMNS = [
    'System_Name','Subsystem_Name','Component_ID',
    'Make','Model','Source_Pin','Target_ID','Target_Pin','Signal_Name'
]
def parse_connectivity_data(file_path_or_buffer) -> pd.DataFrame:
    all_sheets = pd.read_excel(file_path_or_buffer, sheet_name=None, engine='openpyxl')
    frames = []
    for sheet_name, df in all_sheets.items():
        if 'Subsystem_Name' not in df.columns:
            df['Subsystem_Name'] = sheet_name
        frames.append(df)
    combined = pd.concat(frames, ignore_index=True)
    missing = [c for c in REQUIRED_COLUMNS if c not in combined.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    combined = combined.dropna(subset=['Component_ID','Make','Model'])
    return combined
