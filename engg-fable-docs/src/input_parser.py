"""src/input_parser.py — Universal connectivity input parser.

Accepts multiple input formats and normalizes them all to the canonical
connectivity DataFrame (REQUIRED_COLUMNS from excel_parser):

  .xlsx / .xls   multi-tab workbook (tab name = Subsystem_Name if column absent)
  .csv           single flat table
  .docx          Word document — every table with the required header row is read;
                 table caption/preceding heading is used as Subsystem_Name fallback
  .txt / .md     pipe-delimited table (| col | col |), markdown-style

URL and PDF ingestion are on the roadmap (Phase 3): PDFs need per-document
table-extraction tuning, and URL fetch requires the machine to be online,
which conflicts with the offline-first requirement.
"""
import io
import os
from typing import Union

import pandas as pd

from src.excel_parser import REQUIRED_COLUMNS, parse_connectivity_data


def _validate(df: pd.DataFrame, source: str) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{source}: missing columns {missing}")
    df = df.dropna(subset=["Component_ID", "Target_ID", "Signal_Name"])
    for c in REQUIRED_COLUMNS:
        df[c] = df[c].astype(str).str.strip()
    return df


def _parse_csv(fobj) -> pd.DataFrame:
    return _validate(pd.read_csv(fobj), "CSV")


def _parse_docx(path_or_buffer) -> pd.DataFrame:
    from docx import Document
    doc = Document(path_or_buffer)
    frames = []
    for tbl in doc.tables:
        header = [c.text.strip() for c in tbl.rows[0].cells]
        if "Component_ID" not in header:
            continue
        rows = [[c.text.strip() for c in r.cells] for r in tbl.rows[1:]]
        frames.append(pd.DataFrame(rows, columns=header))
    if not frames:
        raise ValueError("DOCX: no tables with a Component_ID header found")
    return _validate(pd.concat(frames, ignore_index=True), "DOCX")


def _parse_pipe_table(text: str) -> pd.DataFrame:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= {"-", ":", " "} for c in cells):  # markdown separator row
            continue
        rows.append(cells)
    if len(rows) < 2:
        raise ValueError("Text input: no pipe-delimited table found")
    return _validate(pd.DataFrame(rows[1:], columns=rows[0]), "Text")


def parse_any(source: Union[str, io.BytesIO], filename: str = "") -> pd.DataFrame:
    """Parse connectivity data from any supported format.

    `source` is a filesystem path or a file-like object (e.g. a Streamlit
    upload); `filename` supplies the extension when source is file-like.
    """
    name = (filename or (source if isinstance(source, str) else "")).lower()
    ext = os.path.splitext(name)[1]

    if ext in (".xlsx", ".xls"):
        return parse_connectivity_data(source)
    if ext == ".csv":
        return _parse_csv(source)
    if ext == ".docx":
        return _parse_docx(source)
    if ext in (".txt", ".md"):
        text = source if isinstance(source, str) and "|" in source else None
        if text is None:
            if isinstance(source, str):
                with open(source, encoding="utf-8") as f:
                    text = f.read()
            else:
                text = source.read().decode("utf-8")
        return _parse_pipe_table(text)
    raise ValueError(
        f"Unsupported input format '{ext}'. Supported: .xlsx, .xls, .csv, .docx, .txt, .md")
