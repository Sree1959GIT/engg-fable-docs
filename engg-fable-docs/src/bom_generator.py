import pandas as pd
from src.llm_client import ask_gemma
BOM_COLUMNS = ['Reference_IDs','Make','Model','Part_Number','Type','Description','Qty']
def _infer_part_info(make: str, model: str) -> tuple:
    prompt = f"Given component Make='{make}' Model='{model}', respond with exactly: PartNumber|Type|ShortDescription\nExample: BQ76952|IC|Battery Management IC"
    resp = ask_gemma(prompt)
    try:
        parts = resp.split('|')
        return parts[0].strip(), parts[1].strip(), '|'.join(parts[2:]).strip() if len(parts) > 2 else ""
    except:
        return "Unknown","Unknown","Pending review"
def generate_draft_bom(df: pd.DataFrame) -> pd.DataFrame:
    refs = df.groupby(['Make','Model'])['Component_ID'].apply(lambda x: ', '.join(sorted(x.unique()))).reset_index(name='Reference_IDs')
    qty = df.groupby(['Make','Model'])['Component_ID'].nunique().reset_index(name='Qty')
    bom = refs.merge(qty, on=['Make','Model'])
    pns, typs, descs = [], [], []
    for _, r in bom.iterrows():
        pn, t, d = _infer_part_info(r['Make'], r['Model'])
        pns.append(pn); typs.append(t); descs.append(d)
    bom['Part_Number'] = pns; bom['Type'] = typs; bom['Description'] = descs
    return bom[BOM_COLUMNS]
