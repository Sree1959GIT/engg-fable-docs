#!/usr/bin/env python3
"""run_pipeline.py — Headless end-to-end run (no Streamlit needed).

Canonical input (has Component_ID/Source_Pin/... columns) — one command:

    python run_pipeline.py input/connectivity.xlsx

Free-form wiring workbooks (production data) — two rounds, mirroring the
web UI's extract → curate → generate workflow:

    # Round 1: extract connections + component inventory for curation
    python run_pipeline.py --system BB3 file1.xlsx file2.xlsx file3.xlsx
      → writes output/draft_connectivity.xlsx  (review/correct rows)
      → writes output/bom_worksheet.xlsx       (fill Make/Model/Part_Number)

    # Round 2: generate the manual from the curated files
    python run_pipeline.py --conn output/draft_connectivity.xlsx \
                           --bom output/bom_worksheet.xlsx --system BB3
"""
import argparse
import os
import sys

import pandas as pd

from src.bom_generator import generate_draft_bom
from src.config import OUTPUT_DIR
from src.input_parser import parse_any
from src.wiring_extractor import (
    CANONICAL_COLUMNS,
    build_inventory,
    extract_part_hints,
    extract_workbook,
    inventory_to_bom,
    is_canonical,
    merge_inventory,
)
from supervisor import SupervisorAgent


def _run(df_conn: pd.DataFrame, df_bom: pd.DataFrame):
    sv = SupervisorAgent(df_conn, df_bom)
    state = sv.run_generation_cycle()
    print("\n──── RESULTS ────")
    print(f"Diagrams:  {list(state['diagrams'].values())}")
    print(f"DOCX:      {state['docs']['docx']}")
    print(f"PDF:       {state['docs']['pdf']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="input wiring/connectivity files")
    ap.add_argument("--system", default="System", help="system name for the manual")
    ap.add_argument("--conn", help="curated canonical connectivity xlsx")
    ap.add_argument("--bom", help="curated BOM worksheet xlsx (Component_ID + Make/Model/Part_Number)")
    args = ap.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Round 2: curated inputs provided ──────────────────────────────────
    if args.conn:
        df_conn = pd.read_excel(args.conn, dtype=str).fillna("")
        for c in CANONICAL_COLUMNS:
            if c not in df_conn.columns:
                df_conn[c] = ""
        df_conn["System_Name"] = args.system
        df_conn = df_conn[CANONICAL_COLUMNS]
        print(f"Connections: {len(df_conn)} rows, "
              f"{df_conn['Subsystem_Name'].nunique()} sub-modules")
        if args.bom:
            worksheet = pd.read_excel(args.bom, dtype=str).fillna("")
            if "Reference_IDs" in worksheet.columns:      # already aggregated
                df_bom = worksheet
            else:                                          # per-component worksheet
                df_conn = merge_inventory(df_conn, worksheet)
                df_bom = inventory_to_bom(worksheet)
                # non-physical items (signals/test points/terminations) are
                # kept as internal reference data, not BOM lines
                from src.wiring_extractor import split_reference_items
                _, ref_items = split_reference_items(worksheet)
                if len(ref_items):
                    os.makedirs("input", exist_ok=True)
                    ref_items.to_excel("input/system_reference.xlsx", index=False)
                    print(f"  {len(ref_items)} non-physical items saved to "
                          "input/system_reference.xlsx (not BOM lines)")
        else:
            df_bom = generate_draft_bom(df_conn)
        missing = df_bom[df_bom["Make"].astype(str).str.upper() == "TBD"]
        if len(missing):
            print(f"  note: {len(missing)} BOM lines still TBD — flagged in the manual")
        _run(df_conn, df_bom)
        return

    if not args.files:
        ap.print_help()
        sys.exit(1)

    # ── Parse/extract every input file ─────────────────────────────────────
    frames, hints, any_extracted = [], {}, False
    for path in args.files:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".xlsx", ".xls") and not is_canonical(path):
            df = extract_workbook(path, filename=path, system_name=args.system)
            hints.update(extract_part_hints(path))
            any_extracted = True
            print(f"🔎 {os.path.basename(path)}: free-form — extracted {len(df)} connections")
        else:
            df = parse_any(path)
            for c in CANONICAL_COLUMNS:
                if c not in df.columns:
                    df[c] = ""
            print(f"✅ {os.path.basename(path)}: canonical — {len(df)} rows")
        frames.append(df)

    df_conn = pd.concat(frames, ignore_index=True)
    df_conn["System_Name"] = args.system
    df_conn = df_conn[CANONICAL_COLUMNS].drop_duplicates(
        subset=["Subsystem_Name", "Component_ID", "Source_Pin",
                "Target_ID", "Target_Pin"]).reset_index(drop=True)

    if any_extracted:
        # Round 1 for free-form data: hand the drafts to the user for curation
        conn_path = os.path.join(OUTPUT_DIR, "draft_connectivity.xlsx")
        bom_path = os.path.join(OUTPUT_DIR, "bom_worksheet.xlsx")
        df_conn.to_excel(conn_path, index=False)
        inventory = build_inventory(df_conn, hints)
        inventory.to_excel(bom_path, index=False)
        n_missing = int((inventory["Status"] != "OK").sum())
        print(f"\nExtracted {len(df_conn)} connections | "
              f"{len(inventory)} components ({n_missing} missing part info)")
        print(f"\n  1. Review/correct:   {conn_path}")
        print(f"  2. Fill part data:   {bom_path}  (Make / Model / Part_Number)")
        print("  3. Generate:")
        print(f"       python run_pipeline.py --conn {conn_path} --bom {bom_path} "
              f"--system {args.system}")
        print("\n(Or use the web UI — `python -m streamlit run web_ui.py` — "
              "which walks these steps interactively.)")
        return

    # Canonical single-shot (original behaviour)
    print("Generating draft BOM ...")
    df_bom = generate_draft_bom(df_conn)
    print(df_bom.to_string(index=False))
    _run(df_conn, df_bom)


if __name__ == "__main__":
    main()
