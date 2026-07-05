"""src/system_analysis.py — Pure-pandas cross-subsystem analysis (no LLM).

Computes how subsystems interconnect: which components bridge them and what
signal classes flow across the boundary. Feeds the description pipeline,
the system diagram, and the per-subsystem interconnection tables.
"""
from typing import Dict, List

import pandas as pd

from src.component_registry import SIGNAL_CLASSES, classify_signal


def subsystem_components(df_conn: pd.DataFrame) -> Dict[str, set]:
    out: Dict[str, set] = {}
    for sub, g in df_conn.groupby("Subsystem_Name"):
        out[str(sub)] = set(g["Component_ID"].astype(str)) | set(g["Target_ID"].astype(str))
    return out


def find_bridges(df_conn: pd.DataFrame) -> List[dict]:
    """Components that participate in more than one subsystem."""
    sub_comps = subsystem_components(df_conn)
    membership: Dict[str, list] = {}
    for sub, comps in sub_comps.items():
        for c in comps:
            membership.setdefault(c, []).append(sub)
    bridges = []
    for comp, subs in sorted(membership.items()):
        if len(subs) < 2:
            continue
        mask = (df_conn["Component_ID"].astype(str) == comp) | (df_conn["Target_ID"].astype(str) == comp)
        classes = sorted({classify_signal(s) for s in df_conn[mask]["Signal_Name"]})
        bridges.append({
            "component": comp,
            "subsystems": sorted(subs),
            "signal_classes": [SIGNAL_CLASSES[c]["label"] for c in classes],
        })
    return bridges


def interconnections_for(subsystem: str, df_conn: pd.DataFrame) -> List[dict]:
    """For one subsystem: rows describing its links to each other subsystem."""
    sub_comps = subsystem_components(df_conn)
    mine = sub_comps.get(subsystem, set())
    rows = []
    for other, theirs in sub_comps.items():
        if other == subsystem:
            continue
        shared = sorted(mine & theirs)
        if not shared:
            continue
        mask = (df_conn["Subsystem_Name"].astype(str) == other) & (
            df_conn["Component_ID"].astype(str).isin(shared)
            | df_conn["Target_ID"].astype(str).isin(shared))
        signals = sorted(df_conn[mask]["Signal_Name"].astype(str).unique())
        rows.append({
            "other_subsystem": other,
            "shared_components": ", ".join(shared),
            "signals": ", ".join(signals[:8]) + ("…" if len(signals) > 8 else ""),
        })
    return rows


def signal_table(connections: pd.DataFrame) -> List[dict]:
    """Signal list for one subsystem, classified per IEC 61175-style naming."""
    rows = []
    for sig, g in connections.groupby("Signal_Name"):
        cls = classify_signal(str(sig))
        ends = [f"{r['Component_ID']}.{r['Source_Pin']} → {r['Target_ID']}.{r['Target_Pin']}"
                for _, r in g.iterrows()]
        rows.append({
            "signal": str(sig),
            "class": SIGNAL_CLASSES[cls]["label"],
            "connections": "; ".join(ends),
        })
    order = {label["label"]: i for i, label in enumerate(SIGNAL_CLASSES.values())}
    rows.sort(key=lambda r: (order.get(r["class"], 99), r["signal"]))
    return rows
