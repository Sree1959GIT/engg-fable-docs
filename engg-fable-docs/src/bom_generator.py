"""src/bom_generator.py — Draft BOM from connectivity data.

LLM infers part number/type/description; the offline knowledge base fills in
recognized parts when the LLM is unavailable, so the draft never says 'Unknown'
for components we know about.
"""
import pandas as pd

from src.component_registry import infer_refdes_prefix, lookup_known, IEC_81346_CLASSES
from src.config import MAX_TOKENS_SHORT
from src.llm_client import ask_llm

BOM_COLUMNS = ['Reference_IDs', 'Make', 'Model', 'Part_Number', 'Type', 'Description', 'Qty']


def _infer_part_info(make: str, model: str) -> tuple:
    resp = ask_llm(
        f"Given component Make='{make}' Model='{model}', respond with exactly one line: "
        f"PartNumber|Type|ShortDescription\nExample: BQ76952|IC|Battery Management IC",
        max_tokens=MAX_TOKENS_SHORT,
    )
    if resp and "|" in resp:
        parts = resp.splitlines()[0].split("|")
        if len(parts) >= 2:
            return (parts[0].strip(), parts[1].strip(),
                    "|".join(parts[2:]).strip() if len(parts) > 2 else "")
    # Offline fallback: knowledge base, then IEC class from the model/prefix
    known = lookup_known(model)
    if known:
        first_sentence = known["description"].split(". ")[0] + "."
        return model, known["type"], first_sentence
    prefix = infer_refdes_prefix("", model)
    return model, IEC_81346_CLASSES.get(prefix, "Component"), "Pending review"


def generate_draft_bom(df: pd.DataFrame) -> pd.DataFrame:
    refs = df.groupby(['Make', 'Model'])['Component_ID'].apply(
        lambda x: ', '.join(sorted(x.unique()))).reset_index(name='Reference_IDs')
    qty = df.groupby(['Make', 'Model'])['Component_ID'].nunique().reset_index(name='Qty')
    bom = refs.merge(qty, on=['Make', 'Model'])
    pns, typs, descs = [], [], []
    for _, r in bom.iterrows():
        pn, t, d = _infer_part_info(str(r['Make']), str(r['Model']))
        pns.append(pn)
        typs.append(t)
        descs.append(d)
    bom['Part_Number'] = pns
    bom['Type'] = typs
    bom['Description'] = descs
    return bom[BOM_COLUMNS]
