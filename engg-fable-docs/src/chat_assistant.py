"""src/chat_assistant.py — Conversational helper for the web UI.

Two capabilities, both offline-first (deterministic answers from the data;
the local LLM only makes replies read more naturally when it is running):

1. answer_data_question(): a real chatbot over the CURRENT dataset —
   "what sub-modules are there?", "what is B3OXJ2 connected to?",
   "how many components?", "which signals are in GPIO?", "what is U33?".
   Falls back to an LLM answer grounded in a compact data digest for
   anything the deterministic patterns don't cover.

2. sme_connection_review() / sme_inventory_review(): SME-style review of
   the INPUTS at each curation phase. Returns numbered suggestions, some
   carrying an actionable payload the UI can apply when the user accepts
   ("apply 2", "apply all").
"""
import re
from typing import Dict, List, Optional

import pandas as pd

from src.llm_client import ask_llm, llm_available


# ── data digest (grounds LLM answers; also the offline fallback) ─────────
def data_digest(df_conn: pd.DataFrame, inventory: pd.DataFrame = None,
                max_len: int = 1800) -> str:
    subs = df_conn.groupby("Subsystem_Name").size().to_dict()
    comps = pd.concat([df_conn["Component_ID"], df_conn["Target_ID"]]).astype(str)
    top = comps.value_counts().head(12).to_dict()
    lines = [
        f"System: {df_conn['System_Name'].iloc[0] if len(df_conn) else '?'}",
        f"Connections: {len(df_conn)} | Components: {comps.nunique()} | "
        f"Sub-modules: {len(subs)}",
        "Sub-modules (connections): " + ", ".join(f"{k}({v})" for k, v in subs.items()),
        "Most-connected components: " + ", ".join(f"{k}({v})" for k, v in top.items()),
    ]
    if inventory is not None and len(inventory):
        n_missing = int((inventory["Status"] != "OK").sum())
        lines.append(f"BOM items: {len(inventory)} ({n_missing} missing part info)")
    return "\n".join(lines)[:max_len]


def _component_lookup(cid: str, df_conn: pd.DataFrame,
                      inventory: pd.DataFrame = None) -> Optional[str]:
    mask = (df_conn["Component_ID"].astype(str).str.lower() == cid.lower()) | \
           (df_conn["Target_ID"].astype(str).str.lower() == cid.lower())
    rows = df_conn[mask]
    if rows.empty:
        return None
    outs, ins = [], []
    for _, r in rows.iterrows():
        if str(r["Component_ID"]).lower() == cid.lower():
            outs.append(f"{r['Signal_Name']} → {r['Target_ID']}.{r['Target_Pin']}")
        else:
            ins.append(f"{r['Signal_Name']} ← {r['Component_ID']}.{r['Source_Pin']}")
    subs = sorted(set(rows["Subsystem_Name"].astype(str)))
    parts = [f"**{cid}** appears in {', '.join(subs)} with {len(rows)} connection(s)."]
    if inventory is not None:
        inv_row = inventory[inventory["Component_ID"].astype(str).str.lower() == cid.lower()]
        if len(inv_row):
            r = inv_row.iloc[0]
            meta = " / ".join(x for x in (str(r.get("Make", "")), str(r.get("Model", "")),
                                          str(r.get("Part_Number", ""))) if x.strip())
            parts.append(f"BOM: {r.get('Item_Type', '?')}"
                         + (f" — {meta}" if meta else " — part info still missing"))
    if outs:
        parts.append("Drives: " + "; ".join(sorted(set(outs))[:8]))
    if ins:
        parts.append("Receives: " + "; ".join(sorted(set(ins))[:8]))
    return "\n\n".join(parts)


def answer_data_question(question: str, df_conn: pd.DataFrame,
                         inventory: pd.DataFrame = None,
                         extra_context: str = "") -> str:
    q = question.lower()

    # deterministic patterns first — exact and instant, no LLM needed
    if re.search(r"how many (connection|wire)", q):
        return f"There are **{len(df_conn)}** connections in the dataset."
    if re.search(r"how many (component|part|item)", q):
        n = pd.concat([df_conn["Component_ID"], df_conn["Target_ID"]]).nunique()
        return f"There are **{n}** distinct components."
    if re.search(r"(what|which|list).*(sub-?modules?|subsystems?)", q):
        subs = df_conn.groupby("Subsystem_Name").size()
        return ("Sub-modules in this system:\n" +
                "\n".join(f"- **{k.replace('_', ' ')}** — {v} connections"
                          for k, v in subs.items()))
    m = re.search(r"signals? (?:in|of|for) ([\w /-]+)", q)
    if m:
        name = m.group(1).strip().replace(" ", "_")
        g = df_conn[df_conn["Subsystem_Name"].astype(str).str.lower() == name.lower()]
        if len(g):
            sigs = sorted(set(g["Signal_Name"].astype(str)))
            return (f"Signals in {name.replace('_', ' ')} ({len(sigs)}): "
                    + ", ".join(sigs[:30]) + ("…" if len(sigs) > 30 else ""))
    # "what is X / what is X connected to / connections of X"
    m = re.search(r"(?:what is|what's|tell me about|connections? (?:of|to|from)|"
                  r"connected to)\s+([A-Za-z0-9_.-]+)", question, re.IGNORECASE)
    if m:
        ans = _component_lookup(m.group(1).strip("?., "), df_conn, inventory)
        if ans:
            return ans
    # any token that IS a component id
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,15}", question):
        ans = _component_lookup(tok, df_conn, inventory)
        if ans:
            return ans

    # LLM answer grounded in the digest
    if llm_available():
        digest = data_digest(df_conn, inventory)
        reply = ask_llm(
            "You are an assistant helping an engineer understand their wiring "
            "dataset before documentation is generated. Answer briefly and "
            "only from the facts below; if the answer isn't in them, say so "
            "and suggest what to ask instead.\n\nDATA:\n" + digest
            + (f"\n\nUSER PREFERENCES SO FAR:\n{extra_context[:600]}" if extra_context else "")
            + f"\n\nQUESTION: {question}",
            max_tokens=300)
        if reply:
            return reply
    return ("I couldn't match that to the data. I can answer things like: "
            "'list the sub-modules', 'what is B3OXJ2 connected to?', "
            "'signals in GPIO', 'how many components?'.\n\n"
            + data_digest(df_conn, inventory))


# ── SME review of the INPUTS (per phase) ──────────────────────────────────
def sme_connection_review(df_conn: pd.DataFrame) -> List[Dict]:
    """Suggestions on the extracted connections. Each item:
    {n, text, action, payload} — action 'drop_rows' carries row indices the
    UI can apply on acceptance; action None is informational."""
    out: List[Dict] = []
    n = 1

    self_loops = df_conn[df_conn["Component_ID"].astype(str)
                         == df_conn["Target_ID"].astype(str)]
    if len(self_loops):
        out.append({"n": n, "action": "drop_rows",
                    "payload": list(self_loops.index),
                    "text": f"{len(self_loops)} connection(s) loop a component "
                            "back to itself (extraction artifact) — recommend deleting."})
        n += 1

    junk_re = re.compile(r"^(pin\d+|with|and|the|for)$", re.IGNORECASE)
    ids = pd.concat([df_conn["Component_ID"], df_conn["Target_ID"]]).astype(str)
    junk = sorted({i for i in ids if junk_re.match(i)})
    if junk:
        rows = df_conn[df_conn["Component_ID"].astype(str).isin(junk)
                       | df_conn["Target_ID"].astype(str).isin(junk)]
        out.append({"n": n, "action": "drop_rows", "payload": list(rows.index),
                    "text": f"Suspicious component tokens {junk} look like "
                            f"words/pins misread as components — recommend "
                            f"deleting their {len(rows)} connection(s)."})
        n += 1

    unnamed = df_conn[df_conn["Signal_Name"].astype(str).str.endswith("_NET")]
    if len(unnamed):
        out.append({"n": n, "action": None, "payload": None,
                    "text": f"{len(unnamed)} connection(s) have auto-generated "
                            "signal names (*_NET) because the source sheet had "
                            "no net name — fine to keep, or rename in the table."})
        n += 1

    tiny = [str(s) for s, g in df_conn.groupby("Subsystem_Name") if len(g) < 3]
    if tiny:
        out.append({"n": n, "action": None, "payload": None,
                    "text": f"Sub-module(s) {tiny} have fewer than 3 connections "
                            "— consider merging them into a neighbour by "
                            "renaming Subsystem_Name."})
        n += 1

    if not out:
        out.append({"n": 1, "action": None, "payload": None,
                    "text": "No structural issues found in the connections. ✔"})
    return out


def sme_inventory_review(inventory: pd.DataFrame) -> List[Dict]:
    """Suggestions on the component inventory / BOM worksheet.
    action 'drop_components' carries Component_IDs to remove on acceptance."""
    out: List[Dict] = []
    n = 1

    junk_ids = [str(r["Component_ID"]) for _, r in inventory.iterrows()
                if re.match(r"^(pin\d*|with|and|the|for)$",
                            str(r["Component_ID"]), re.IGNORECASE)
                or (str(r["Component_ID"]).islower()
                    and int(r.get("Connections", 0) or 0) <= 2)]
    if junk_ids:
        out.append({"n": n, "action": "drop_components", "payload": junk_ids,
                    "text": f"These look like junk tokens, not components: "
                            f"{junk_ids} — recommend deleting them (their "
                            "connections are removed too)."})
        n += 1

    missing = inventory[inventory["Status"] != "OK"]
    if len(missing):
        worst = missing.nlargest(min(5, len(missing)), "Connections",
                                 keep="all")["Component_ID"].tolist()[:5]
        out.append({"n": n, "action": None, "payload": None,
                    "text": f"{len(missing)} item(s) still lack Make/Model/"
                            f"Part_Number; the most-connected (highest impact "
                            f"on the manual): {worst}. Fill these first."})
        n += 1

    heavy_conn = inventory[(inventory["Item_Type"] == "Connector")
                           & (pd.to_numeric(inventory["Connections"],
                                            errors="coerce").fillna(0) >= 30)]
    if len(heavy_conn):
        out.append({"n": n, "action": None, "payload": None,
                    "text": f"{heavy_conn['Component_ID'].tolist()} are typed "
                            "'Connector' but have 30+ connections — if any is "
                            "actually a board/module, change its Item_Type to "
                            "'Module' for a better symbol and description."})
        n += 1

    tp_as_comp = inventory[(inventory["Item_Type"] == "Component")
                           & inventory["Component_ID"].astype(str)
                                        .str.match(r"^TP\d", na=False)]
    if len(tp_as_comp):
        out.append({"n": n, "action": None, "payload": None,
                    "text": f"{tp_as_comp['Component_ID'].tolist()} look like "
                            "test points typed as 'Component' — set Item_Type "
                            "to 'Test point' so they move to reference data "
                            "instead of the BOM."})
        n += 1

    if not out:
        out.append({"n": 1, "action": None, "payload": None,
                    "text": "Inventory looks clean and complete. ✔"})
    return out


def parse_apply(text: str, suggestions: List[Dict]) -> List[Dict]:
    """'apply 2', 'apply 1 and 3', 'apply all', 'yes' → actionable items."""
    low = text.strip().lower()
    actionable = [s for s in suggestions if s["action"]]
    if not actionable:
        return []
    if re.search(r"\b(apply|accept|fix)\b.*\ball\b", low) or low in (
            "yes", "y", "ok", "okay", "apply", "accept", "proceed", "do it"):
        return actionable
    nums = [int(x) for x in re.findall(r"\d+", low)]
    if nums and re.search(r"\b(apply|accept|fix)\b", low):
        return [s for s in actionable if s["n"] in nums]
    return []
