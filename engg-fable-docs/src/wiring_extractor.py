"""src/wiring_extractor.py — Extract connectivity from FREE-FORM wiring workbooks.

Production wiring lists are rarely canonical tables: they are multi-tab
workbooks with merged headers, cable specs, section titles and endpoint
tokens scattered across arbitrary columns:

    J6.45   B3OXJ2-17   DB25A-1   K4   TP10   B3JX1-CR-3   U1-IP-3

This module turns such workbooks into:
  1. a DRAFT canonical connectivity DataFrame (the columns the pipeline
     needs: System_Name, Subsystem_Name, Component_ID, Make, Model,
     Source_Pin, Target_ID, Target_Pin, Signal_Name), and
  2. a COMPONENT INVENTORY (partial BOM) the user curates — adding
     Make / Model / Part_Number — before the pipeline runs.

Heuristics, not magic: every row that contains two or more endpoint-like
tokens becomes a connection (consecutive endpoints are chained, matching
daisy-chained harness rows). Anything ambiguous lands in the draft for the
user to fix in the review step — the workflow is extract → review →
curate → generate, exactly because production data is messy.
"""
import io
import re
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd

CANONICAL_COLUMNS = ["System_Name", "Subsystem_Name", "Component_ID", "Make",
                     "Model", "Source_Pin", "Target_ID", "Target_Pin", "Signal_Name"]

# Words that look like designators but are nets, positions or cable specs
_STOPWORDS = {
    "GND", "VCC", "VDD", "VSS", "VREF", "VBUS", "EARTH", "NEUTRAL", "SPARE",
    "NC", "NA", "TBD", "UNUSED", "TOP", "BOTTOM", "LEFT", "RIGHT", "FREE",
    "TX", "RX", "SCL", "SDA", "TDI", "TDO", "TCK", "TMS", "RST", "CLK",
    "PH", "NU", "NEU", "AWG", "PTFE", "JST", "FRC", "BERG", "IC", "PIN",
    "LABEL", "LEGEND", "WIRE", "CABLE", "CONN", "CONNECTOR", "HOUSING",
    "FEMALE", "MALE", "BOOT", "LUG", "END", "WITH", "MM", "PCBA", "INPUT",
    "OUTPUT", "CONTROL", "NET", "NETNAME", "REMARKS", "DESCRIPTION",
}

# Endpoint token inside a cell: designator segments joined by '.' or '-'
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{0,14}(?:[-.][A-Za-z0-9]{1,8}){0,3}\b")

# Bare designator with class letters + index: K4, TP10, R623, J6, U5, MPP1, DB25A
_BARE_RE = re.compile(r"^[A-Za-z]{1,5}\d{1,4}[A-Za-z]?$")

# Measurements / cable specs that sneak past: 24AWG, 2X5, 500MM, 230V, 0.75
_MEASURE_RE = re.compile(r"^\d|^[0-9.]+$|\d[Xx]\d|MM$|AWG$|^L=\d|\d+V$|\d+A$", re.IGNORECASE)

# Net-ish tokens: contain '_' (GND_POWER, U1_S) or short ALL-CAPS mnemonics
_NET_WORDS = {"GND", "VCC", "VDD", "VREF", "VBUS", "EARTH", "SCL", "SDA",
              "TX", "RX", "TDI", "TDO", "TCK", "TMS", "RST", "CLK", "MISO",
              "MOSI", "CS", "NSS", "PWM", "FAULT", "ALERT"}


def _parse_endpoint(token: str) -> Optional[Tuple[str, str]]:
    """'J6.45' -> ('J6','45'); 'B3JX1-CR-3' -> ('B3JX1-CR','3');
    'B3SMPS2-ACP' -> ('B3SMPS2','ACP'); 'K4' -> ('K4',''); None if not one."""
    t = token.strip()
    up = t.upper()
    if up in _STOPWORDS or _MEASURE_RE.search(up):
        return None
    if not any(ch.isdigit() for ch in t):
        return None                       # designators carry an index digit
    if "." in t:
        comp, pin = t.rsplit(".", 1)
        if comp and pin and not _MEASURE_RE.search(comp.upper()):
            return comp, pin
        return None
    if "-" in t:
        comp, last = t.rsplit("-", 1)
        if _MEASURE_RE.search(comp.upper()):
            return None
        # short trailing segment (1, 17, A5, ACP, IP2) is a pin; a long one
        # (JTFPG, 23B03) means the whole token is the component name
        if last and len(last) <= 4 and any(ch.isalnum() for ch in last):
            return comp, last
        return t, ""
    if _BARE_RE.match(t):
        return t, ""
    return None


def _find_endpoints(cell: str) -> List[Tuple[str, str]]:
    """All endpoint tokens inside a cell ('J5.1 RX' -> [('J5','1')])."""
    s = str(cell).strip()
    # single token with a slash is a pin alternate ('5/A5'), not a component
    if "/" in s and re.match(r"^\S+$", s):
        return []
    out = []
    for m in _TOKEN_RE.finditer(s):
        ep = _parse_endpoint(m.group(0))
        if ep:
            out.append(ep)
    return out


# Classic IEC-style designator prefixes. A bare token whose prefix is NOT in
# this set (A5, AA18, AB10 — BGA ball / grid references) is a PIN of the
# preceding bare designator, not a component of its own.
_CLASS_PREFIXES = {"J", "U", "K", "R", "C", "D", "Q", "F", "M", "B", "L",
                   "S", "T", "X", "W", "E", "TP", "TB", "SW", "BT", "CON"}


def _merge_grid_pins(eps: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Merge [('J6',''),('A22','')] -> [('J6','A22')]: a pin-less designator
    followed by a pin-less grid reference is one endpoint split across
    adjacent columns (common in BGA / connector ball maps)."""
    merged: List[Tuple[str, str]] = []
    i = 0
    while i < len(eps):
        comp, pin = eps[i]
        if pin == "" and i + 1 < len(eps):
            nxt, nxt_pin = eps[i + 1]
            m = re.match(r"^([A-Za-z]{1,2})\d{1,3}$", nxt)
            if nxt_pin == "" and m and m.group(1).upper() not in _CLASS_PREFIXES:
                merged.append((comp, nxt))
                i += 2
                continue
        merged.append((comp, pin))
        i += 1
    return merged


def _find_signal(cells: List[str]) -> str:
    """Best net-name candidate in a row: GND_POWER, SCL, 230V PH, U1_S..."""
    for c in cells:
        s = str(c).strip()
        if "_" in s and 2 < len(s) <= 24 and not s.startswith("="):
            if re.match(r"^[A-Za-z0-9_/-]+$", s):
                return s
    for c in cells:
        s = str(c).strip().upper()
        if s in _NET_WORDS:
            return s
        if re.match(r"^\d{2,3}V\s?(PH|NU|NEU)?$", s):
            return s.replace(" ", "_")
    return ""


def extract_workbook(source: Union[str, io.BytesIO], filename: str = "",
                     system_name: str = "") -> pd.DataFrame:
    """Extract a draft connectivity table from one free-form workbook.
    Sheet name becomes Subsystem_Name. Make/Model stay blank — they are
    filled from the curated inventory later."""
    xl = pd.ExcelFile(source)
    rows = []
    for sheet in xl.sheet_names:
        df = xl.parse(sheet, header=None, dtype=str)
        for _, r in df.iterrows():
            cells = [c for c in r.tolist() if isinstance(c, str) and c.strip()]
            if not cells:
                continue
            # endpoints in reading order across the row
            eps: List[Tuple[str, str]] = []
            for c in cells:
                for ep in _find_endpoints(c):
                    if ep not in eps:
                        eps.append(ep)
            eps = _merge_grid_pins(eps)
            if len(eps) < 2:
                continue
            signal = _find_signal(cells)
            # chain consecutive endpoints: A→B→C gives A→B and B→C,
            # matching daisy-chained harness rows
            for (sc, sp), (tc, tp) in zip(eps, eps[1:]):
                if sc == tc:
                    continue
                rows.append({
                    "System_Name": system_name or "System",
                    "Subsystem_Name": str(sheet).strip().replace(" ", "_"),
                    "Component_ID": sc, "Make": "", "Model": "",
                    "Source_Pin": sp or "-",
                    "Target_ID": tc, "Target_Pin": tp or "-",
                    "Signal_Name": signal or f"{sc}_{sp or 'NET'}",
                })
    out = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
    # collapse duplicates from repeated harness rows
    if not out.empty:
        out = out.drop_duplicates(
            subset=["Subsystem_Name", "Component_ID", "Source_Pin",
                    "Target_ID", "Target_Pin"]).reset_index(drop=True)
    return out


def extract_part_hints(source: Union[str, io.BytesIO]) -> Dict[str, dict]:
    """Scan every sheet for part-info tables (DESCRIPTION / NOMENCLATURE /
    PARTNUMBER / MAKE / MODEL headers) and free-standing part numbers next
    to designators. Returns {component_id: {make, model, part_number}}."""
    hints: Dict[str, dict] = {}
    xl = pd.ExcelFile(source)
    for sheet in xl.sheet_names:
        df = xl.parse(sheet, header=None, dtype=str)
        header_row, col_map = None, {}
        for i, r in df.iterrows():
            up = [str(c).strip().upper() for c in r.tolist()]
            keys = {}
            for j, v in enumerate(up):
                if v in ("NOMENCLATURE", "REFERENCE", "REF", "DESIGNATOR"):
                    keys["id"] = j
                elif v in ("PARTNUMBER", "PART NUMBER", "PART_NO", "PART NO"):
                    keys["part"] = j
                elif v in ("MAKE", "MANUFACTURER", "VENDOR"):
                    keys["make"] = j
                elif v in ("MODEL", "TYPE OF CONNECTOR"):
                    keys["model"] = j
                elif v == "DESCRIPTION":
                    keys["desc"] = j
            if "id" in keys or ("part" in keys and len(keys) >= 2):
                header_row, col_map = i, keys
                break
        if header_row is None:
            continue
        for _, r in df.iloc[header_row + 1:].iterrows():
            def _cell(k):
                j = col_map.get(k)
                v = r.iloc[j] if j is not None and j < len(r) else None
                return str(v).strip() if isinstance(v, str) else ""
            cid = _cell("id")
            if not cid:
                continue
            hints[cid] = {"make": _cell("make"), "model": _cell("model"),
                          "part_number": _cell("part"),
                          "description": _cell("desc")}
    return hints


INVENTORY_COLUMNS = ["Component_ID", "Item_Type", "Connections", "Make",
                     "Model", "Part_Number", "Type", "Description", "Status"]

# Item_Type vocabulary — the user assigns these in the curation step. Only
# PHYSICAL types appear in the final BOM; the rest is saved as internal
# reference data (input/system_reference.xlsx) for the pipeline to use.
ITEM_TYPES = ["Component", "Module", "Sub-system", "Connector", "Cable",
              "Termination", "Test point", "Signal name"]
PHYSICAL_ITEM_TYPES = {"Component", "Module", "Sub-system", "Connector", "Cable"}


def _guess_item_type(cid: str) -> str:
    up = cid.upper()
    if re.match(r"^TP\d", up):
        return "Test point"
    if re.match(r"^(PWR|GND|VCC|VDD|3V3|5V|12V|24V)", up):
        return "Signal name"
    if "SMPS" in up or "BRD" in up or "BOARD" in up or re.match(r"^RB\d", up):
        return "Module"
    if re.match(r"^(J|X|DB|CON|MPP|JP|JF|TB|TAP)[A-Z0-9]", up) or "JX" in up:
        return "Connector"
    return "Component"


def build_inventory(df_conn: pd.DataFrame,
                    part_hints: Dict[str, dict] = None) -> pd.DataFrame:
    """Component inventory (partial BOM) for user curation. Every component
    seen in the connectivity draft gets a row; Make/Model/Part_Number are
    pre-filled from whatever the source files contained and the rest is
    left for the user. Status tracks completeness for the iteration loop."""
    from src.component_registry import IEC_81346_CLASSES, infer_refdes_prefix
    part_hints = part_hints or {}
    counts: Dict[str, int] = {}
    for col in ("Component_ID", "Target_ID"):
        for v in df_conn[col].astype(str):
            counts[v] = counts.get(v, 0) + 1

    make_by_id: Dict[str, Tuple[str, str]] = {}
    for _, r in df_conn.iterrows():
        mk, md = str(r.get("Make", "") or ""), str(r.get("Model", "") or "")
        if mk or md:
            make_by_id.setdefault(str(r["Component_ID"]), (mk, md))

    rows = []
    for cid in sorted(counts):
        hint = part_hints.get(cid, {})
        mk, md = make_by_id.get(cid, ("", ""))
        make = hint.get("make") or mk
        model = hint.get("model") or md
        part = hint.get("part_number") or ""
        prefix = infer_refdes_prefix(cid, model)
        ctype = IEC_81346_CLASSES.get(prefix, "Component")
        complete = bool((make or model or part))
        rows.append({
            "Component_ID": cid, "Item_Type": _guess_item_type(cid),
            "Connections": counts[cid],
            "Make": make, "Model": model, "Part_Number": part,
            "Type": hint.get("description") or ctype,
            "Description": hint.get("description") or "",
            "Status": "OK" if complete else "MISSING — add Make/Model/Part_Number",
        })
    return pd.DataFrame(rows, columns=INVENTORY_COLUMNS)


def merge_inventory(df_conn: pd.DataFrame,
                    inventory: pd.DataFrame) -> pd.DataFrame:
    """Write curated Make/Model back into the connectivity rows (source
    components only — targets resolve through the registry)."""
    inv = {str(r["Component_ID"]): r for _, r in inventory.iterrows()}
    df = df_conn.copy()
    df["Make"] = [str(inv.get(str(c), {}).get("Make", "") or "")
                  for c in df["Component_ID"]]
    df["Model"] = [str(inv.get(str(c), {}).get("Model", "") or "")
                   for c in df["Component_ID"]]
    return df


def split_reference_items(inventory: pd.DataFrame):
    """(physical_items, reference_items): only physical, procurable items
    belong in the BOM; signal names / test points / terminations are kept
    as internal reference data for the pipeline."""
    it = inventory.get("Item_Type", pd.Series(["Component"] * len(inventory)))
    mask = it.astype(str).isin(PHYSICAL_ITEM_TYPES)
    return inventory[mask].reset_index(drop=True), inventory[~mask].reset_index(drop=True)


def inventory_to_bom(inventory: pd.DataFrame) -> pd.DataFrame:
    """Aggregate the curated inventory into the pipeline's BOM format
    (Reference_IDs / Make / Model / Part_Number / Type / Description / Qty).
    Only PHYSICAL items are listed (signals/test points/terminations are
    reference data); items sharing Make+Model collapse into one line with a
    real aggregated quantity; items still missing data become TBD lines the
    manual flags for confirmation."""
    physical, _ = split_reference_items(inventory)
    groups: Dict[tuple, dict] = {}
    for _, r in physical.iterrows():
        make = str(r.get("Make", "") or "").strip()
        model = str(r.get("Model", "") or "").strip()
        part = str(r.get("Part_Number", "") or "").strip()
        key = (make, model, part) if (make or model or part) else ("TBD", str(r["Component_ID"]), "")
        g = groups.setdefault(key, {"ids": [], "type": str(r.get("Type", "") or ""),
                                    "desc": str(r.get("Description", "") or "")})
        g["ids"].append(str(r["Component_ID"]))

    rows = []
    for (make, model, part), g in groups.items():
        tbd = make == "TBD"
        rows.append({
            "Reference_IDs": ", ".join(g["ids"]),
            "Make": "TBD" if tbd else make,
            "Model": "TBD" if tbd else model,
            "Part_Number": "TBD" if tbd else (part or model),
            "Type": g["type"] or "Component",
            "Description": g["desc"] or (
                "Inferred from connectivity context — confirm part selection."
                if tbd else ""),
            "Qty": len(g["ids"]),
        })
    return pd.DataFrame(rows, columns=["Reference_IDs", "Make", "Model",
                                       "Part_Number", "Type", "Description", "Qty"])


def is_canonical(source: Union[str, io.BytesIO], filename: str = "") -> bool:
    """True if the workbook already has the canonical connectivity columns."""
    try:
        xl = pd.ExcelFile(source)
        for sheet in xl.sheet_names:
            head = xl.parse(sheet, nrows=1)
            need = {"Component_ID", "Source_Pin", "Target_ID", "Target_Pin", "Signal_Name"}
            if need.issubset(set(map(str, head.columns))):
                return True
        return False
    finally:
        if hasattr(source, "seek"):
            source.seek(0)
