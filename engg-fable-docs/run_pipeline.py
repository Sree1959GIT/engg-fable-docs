#!/usr/bin/env python3
"""run_pipeline.py — Headless end-to-end run for testing (no Streamlit needed).

Usage:
    python generate_sample_data.py
    python run_pipeline.py [input/connectivity.xlsx]
"""
import sys

from src.bom_generator import generate_draft_bom
from src.excel_parser import parse_connectivity_data
from supervisor import SupervisorAgent


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "input/connectivity.xlsx"
    print(f"Parsing {path} ...")
    df_conn = parse_connectivity_data(path)
    print(f"  {len(df_conn)} connections, {df_conn['Subsystem_Name'].nunique()} subsystems")

    print("Generating draft BOM ...")
    df_bom = generate_draft_bom(df_conn)
    print(df_bom.to_string(index=False))

    sv = SupervisorAgent(df_conn, df_bom)
    state = sv.run_generation_cycle()

    print("\n──── RESULTS ────")
    print(f"Diagrams:  {list(state['diagrams'].values())}")
    print(f"DOCX:      {state['docs']['docx']}")
    print(f"PDF:       {state['docs']['pdf']}")


if __name__ == "__main__":
    main()
