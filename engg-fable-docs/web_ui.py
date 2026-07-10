"""web_ui.py — Streamlit wizard for the documentation pipeline (v12).

Workflow:
  1. UPLOAD       multiple files (canonical or free-form) + document title /
                  number + optional reference document + output expectations
                  + optional signal-bus diagram style
  2. CONNECTIONS  review/correct extracted connectivity — delete junk rows
  3. BOM          component inventory with Item_Type; deletable rows (purges
                  the underlying connections too); only physical items enter
                  the BOM; if items remain TBD, a gaps report is shown for a
                  final check before proceeding
  4. AGENTS       the app presents the agent roster (sized to the detected
                  hardware) for acceptance, plus a chat box for any
                  system-wide instructions before generation starts — useful
                  when you don't yet know how many sub-modules exist
  5. GENERATE     pipeline with live agent status (diagram verification +
                  SME review) → draft preview → a SINGLE chat box drives
                  rework for any module (or System Overview, or all of
                  them): the assistant restates what it understood and asks
                  for confirmation before applying anything → Approve →
                  downloads

All inputs and curated data are also saved under input/ for traceability.

Every step (2-5) has a "← Back" button to the previous step.
"""
import io
import os
import re
import threading
import time

import pandas as pd
import streamlit as st

from src import config
from src.hardware import describe_profile, detect_profile
from src.input_parser import parse_any
from src.chat_assistant import (
    answer_data_question,
    parse_apply,
    sme_connection_review,
    sme_inventory_review,
)
from src.llm_client import ask_llm, llm_available
from src.wiring_extractor import (
    CANONICAL_COLUMNS,
    ITEM_TYPES,
    build_inventory,
    drop_components,
    extract_part_hints,
    extract_workbook,
    gaps_report,
    inventory_to_bom,
    is_canonical,
    merge_inventory,
    split_reference_items,
)
from supervisor import SupervisorAgent

st.set_page_config(page_title="Engineering Documentation Generator", layout="wide")
st.title("Engineering Documentation Generator")

if llm_available():
    st.caption(f"🟢 Local LLM online · {describe_profile()}")
else:
    st.caption(f"🟠 Local LLM offline — programmatic mode · {describe_profile()}")

ss = st.session_state
ss.setdefault("step", 1)
ss.setdefault("supervisor", None)
ss.setdefault("show_gaps", False)
ss.setdefault("setup_chat", [])
ss.setdefault("review_chat", [])
ss.setdefault("pending_action", None)
ss.setdefault("sme_chat_conn", [])
ss.setdefault("sme_chat_inv", [])
ss.setdefault("sme_sug_conn", [])
ss.setdefault("sme_sug_inv", [])
ss.setdefault("audit", [])
ss.setdefault("pipe_thread", None)


def _audit(entry: str):
    ss.audit.append(entry)


def _is_question(text: str) -> bool:
    low = text.strip().lower()
    first = low.split()[0] if low.split() else ""
    return "?" in low or first in {
        "what", "which", "how", "why", "where", "who", "list", "show",
        "tell", "explain", "describe", "does", "is", "are", "can"}


def _extract_title(text: str):
    """'name the title as X' / 'title the document \"X\"' → X, else None."""
    if not re.search(r"\b(title|rename|name)\b", text, re.IGNORECASE):
        return None
    m = re.search(r'["\u201c]([^"\u201d]{3,80})["\u201d]', text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:title|document|name)\s+(?:as|to|:)\s+(.{3,80})$",
                  text, re.IGNORECASE)
    if not m:
        m = re.search(r"(?:title\s+(?:as|to|:)?|name\s+the\s+title\s+(?:as|to)?)\s+(.+)$",
                      text, re.IGNORECASE)
    if m:
        t = m.group(1).strip().strip('".')
        return t if 3 <= len(t) <= 80 else None
    return None


def _apply_title(sv, new_title: str, rebuild_docs: bool) -> str:
    sv.state["doc_title"] = new_title
    ss.doc_title = new_title
    _audit(f"Document title set to '{new_title}'")
    if rebuild_docs and sv.state.get("descriptions"):
        from agents import DocumentationAgent
        sv.state["docs"] = DocumentationAgent.run(sv.state)
        sv.save_snapshot()
        return (f"✅ Document title set to **{new_title}** and the "
                "documents were rebuilt — download below to check.")
    return (f"✅ Document title set to **{new_title}** — it will appear on "
            "the cover page and footers when the manual is generated.")


def _sme_chat_panel(phase: str, suggestions_key: str, chat_key: str,
                    run_review, apply_action):
    """Shared SME-review chat: 'Run SME Review' produces numbered
    suggestions; the user can accept actionable ones ('apply 1', 'apply
    all') or ask free questions about the data."""
    with st.expander(f"🧑‍🔬 SME review & chat — {phase}", expanded=bool(ss[chat_key])):
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("Run SME Review", key=f"smebtn_{suggestions_key}"):
                sugs = run_review()
                ss[suggestions_key] = sugs
                body = "\n".join(f"**{x['n']}.** {x['text']}" for x in sugs)
                actionable = [x for x in sugs if x["action"]]
                if actionable:
                    body += ("\n\nSay **apply N** (e.g. 'apply 1'), "
                             "**apply all**, or ask me anything about the data.")
                ss[chat_key].append({"role": "assistant", "content": body})
                st.rerun()
        with c2:
            st.caption("The SME agent checks the current data for issues and "
                       "suggests corrections you can accept in the chat. You "
                       "can also just ask questions ('what is J6 connected "
                       "to?', 'list the sub-modules').")
        _render_chat(ss[chat_key])
        msg = st.chat_input("Ask about the data, or accept suggestions ('apply 1', 'apply all')",
                            key=f"smein_{suggestions_key}")
        if msg:
            ss[chat_key].append({"role": "user", "content": msg})
            hits = parse_apply(msg, ss[suggestions_key])
            if hits:
                results = [apply_action(h) for h in hits]
                ss[chat_key].append({"role": "assistant",
                                     "content": "\n".join(results)})
            elif _is_question(msg):
                ans = answer_data_question(msg, ss.df_conn, ss.get("inventory"))
                ss[chat_key].append({"role": "assistant", "content": ans})
            else:
                ss[chat_key].append({"role": "assistant", "content":
                    "Noted. To act on a suggestion say 'apply N' or 'apply "
                    "all'; you can also edit the table directly, or ask me a "
                    "question about the data."})
            st.rerun()

_EDIT_COLS = ["Subsystem_Name", "Component_ID", "Source_Pin",
              "Target_ID", "Target_Pin", "Signal_Name"]
_CONFIRM_WORDS = {"yes", "y", "confirm", "confirmed", "proceed", "go ahead",
                  "goahead", "ok", "okay", "do it", "apply", "sure", "yep"}
_CANCEL_WORDS = {"no", "n", "cancel", "stop", "nevermind", "never mind", "skip"}
os.makedirs("input", exist_ok=True)


def _xlsx_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _canonize(df: pd.DataFrame, sys_name: str) -> pd.DataFrame:
    df = df.copy()
    for c in CANONICAL_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df["System_Name"] = sys_name
    df = df.fillna("")
    df = df[(df["Component_ID"].astype(str).str.strip() != "")
            & (df["Target_ID"].astype(str).str.strip() != "")]
    return df[CANONICAL_COLUMNS].reset_index(drop=True)


def _read_reference(upload) -> str:
    name = upload.name.lower()
    if name.endswith(".docx"):
        from docx import Document
        return "\n".join(p.text for p in Document(upload).paragraphs if p.text)
    return upload.read().decode("utf-8", errors="replace")


def _revalidate(inv: pd.DataFrame) -> pd.DataFrame:
    inv = inv.copy()
    inv["Status"] = [
        "OK" if (str(r["Make"]).strip() or str(r["Model"]).strip()
                 or str(r["Part_Number"]).strip())
        else "MISSING — add Make/Model/Part_Number"
        for _, r in inv.iterrows()]
    return inv


def _finalize_bom(edited: pd.DataFrame):
    """Purge deleted/junk components from the connections, aggregate the
    BOM, split off internal reference data, persist everything to input/,
    and hand off to the supervisor."""
    keep_ids = edited["Component_ID"].astype(str).tolist()
    df_conn2, n_removed = drop_components(ss.df_conn, keep_ids)
    if n_removed:
        st.info(f"Removed {n_removed} connection(s) referencing deleted/junk "
                f"components along with the row(s) you deleted.")
    ss.df_conn = df_conn2
    ss.inventory = edited
    ss.df_bom = inventory_to_bom(edited)
    _, reference_items = split_reference_items(edited)

    ss.df_conn.to_excel("input/draft_connectivity_final.xlsx", index=False)
    edited.to_excel("input/bom_worksheet_final.xlsx", index=False)
    ss.df_bom.to_excel("input/BOM_curated.xlsx", index=False)
    if len(reference_items):
        reference_items.to_excel("input/system_reference.xlsx", index=False)

    n_tbd = int((edited["Status"] != "OK").sum())
    _audit(f"BOM built: {len(ss.df_bom)} lines ({n_tbd} items TBD), "
           f"{len(reference_items)} non-physical items → system_reference.xlsx"
           + (f", {n_removed} junk connections purged" if n_removed else ""))
    ss.supervisor = SupervisorAgent(
        ss.df_conn, ss.df_bom,
        doc_title=ss.get("doc_title", ""),
        reference_context=ss.get("reference_context", ""),
        expectations=ss.get("expectations", ""),
        bus_mode=ss.get("bus_mode", False))
    ss.show_gaps = False
    ss.step = 4
    st.rerun()


def _back_button(target_step: int, label: str = "← Back"):
    if st.button(label, key=f"back_to_{target_step}"):
        ss.step = target_step
        st.rerun()


def _run_with_progress(fn, *args, spinner_label="Working...", **kwargs):
    """Run a supervisor action with a progress display bound to THIS script
    run, then release the callback immediately afterward. sv (in
    session_state) persists across reruns but Streamlit UI containers do
    not — leaving a stale callback around is what previously caused a
    blank/broken page on the next action."""
    sv = ss.supervisor
    box = st.empty()
    sv.progress_callback = lambda stage, detail, _b=box: _b.write(
        f"{stage}" + (f" — {detail}" if detail else ""))
    with st.spinner(spinner_label):
        result = fn(*args, **kwargs)
    sv.progress_callback = None
    box.empty()
    return result


# ── Chat intent parsing (fully offline; LLM only used to rephrase) ───────
def _module_display_names(sv) -> list:
    return list(sv.state["descriptions"].keys())


def _parse_targets(text: str, module_names: list) -> list:
    low = " " + text.lower() + " "
    targets = []
    if any(p in low for p in (" system overview", " overall system",
                              " whole system", " system diagram",
                              " system description", " system block")):
        targets.append("System Overview")
    if any(p in low for p in (" all modules", " every module",
                              " all sub-modules", " all submodules",
                              " entire manual", " whole manual", " everything",
                              " all drawings", " all diagrams", " all sheets",
                              " all the drawings", " all the diagrams")):
        return list(module_names) + (["System Overview"]
                                     if "System Overview" not in targets else [])
    for m in module_names:
        mn = m.replace("_", " ").lower()
        if re.search(r"\b" + re.escape(mn) + r"\b", low) or \
           re.search(r"\b" + re.escape(m.lower()) + r"\b", low):
            if m not in targets:
                targets.append(m)
    return targets


def _classify_message(text: str, module_names: list) -> dict:
    low = text.strip().lower().rstrip("!.")
    words = re.findall(r"[a-z']+", low)
    first_word = words[0] if words else ""
    # exact short replies, or a leading confirm/cancel word ("yes please",
    # "no thanks") — but only when that's clearly the whole point of the
    # message (short reply), so a longer instruction that happens to start
    # with "no, instead do X on Y" isn't misread as a bare cancellation
    if low in _CONFIRM_WORDS or (first_word in _CONFIRM_WORDS and len(words) <= 3):
        return {"kind": "confirm"}
    if low in _CANCEL_WORDS or (first_word in _CANCEL_WORDS and len(words) <= 3):
        return {"kind": "cancel"}
    targets = _parse_targets(text, module_names)
    return {"kind": "instruction", "targets": targets, "instruction": text.strip()}


def _render_chat(history: list):
    for msg in history:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])


# ── Step 1: Upload & document setup ───────────────────────────────────────
if ss.step == 1:
    # ── Previous session detection: offer resume before anything else ──
    snap_path = "input/session_snapshot.json"
    conn_path = "input/draft_connectivity_final.xlsx"
    bom_path = "input/bom_worksheet_final.xlsx"
    if (ss.supervisor is None and os.path.exists(snap_path)
            and os.path.exists(conn_path) and os.path.exists(bom_path)):
        import json
        try:
            with open(snap_path, encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            snap = {}
        docs = snap.get("docs") or {}
        docs_exist = os.path.exists(docs.get("docx", "")) and             os.path.exists(docs.get("pdf", ""))
        with st.container(border=True):
            st.subheader("⏮ Previous session found")
            st.write(f"Saved: **{snap.get('saved_at', '?')}** · status: "
                     f"**{snap.get('status', '?')}** · cycle "
                     f"#{snap.get('cycle_count', 0)} · "
                     f"{len(snap.get('descriptions') or {})} module "
                     f"descriptions on record"
                     + (" · **manual generated** ✔" if docs_exist else ""))
            if docs_exist:
                d1, d2 = st.columns(2)
                with d1:
                    with open(docs["docx"], "rb") as f:
                        st.download_button("Open previous DOCX", f,
                                           "Technical_User_Manual.docx",
                                           key="prev_docx")
                with d2:
                    with open(docs["pdf"], "rb") as f:
                        st.download_button("Open previous PDF", f,
                                           "Technical_User_Manual.pdf",
                                           key="prev_pdf")
            r1, r2 = st.columns(2)
            with r1:
                if st.button("Resume previous session ▶", type="primary",
                             width="stretch"):
                    df_conn = pd.read_excel(conn_path, dtype=str).fillna("")
                    inv = pd.read_excel(bom_path, dtype=str).fillna("")
                    ss.df_conn, ss.inventory = df_conn, inv
                    ss.df_bom = inventory_to_bom(inv)
                    ss.sys_name = (df_conn["System_Name"].iloc[0]
                                   if len(df_conn) else "System")
                    ss.doc_title = snap.get("doc_title", "")
                    ss.reference_context = snap.get("reference_context", "")
                    ss.expectations = snap.get("expectations", "")
                    ss.bus_mode = bool(snap.get("bus_mode"))
                    sv = SupervisorAgent(
                        ss.df_conn, ss.df_bom, doc_title=ss.doc_title,
                        reference_context=ss.reference_context,
                        expectations=ss.expectations, bus_mode=ss.bus_mode)
                    sv.restore_snapshot(snap_path)
                    # registry/analysis aren't JSON-serializable — rebuild
                    # them from the data (pure pandas, instant) so rework
                    # after resume has full context
                    from src.component_registry import build_component_registry
                    from src.system_analysis import find_bridges, interconnections_for
                    sv.state["registry"] = build_component_registry(df_conn, ss.df_bom)
                    sv.state["bridges"] = find_bridges(df_conn)
                    subs_r = [str(x) for x in df_conn["Subsystem_Name"].unique()]
                    sv.state["interconnections"] = {
                        s2: interconnections_for(s2, df_conn) for s2 in subs_r}
                    # a resumed session is reviewable if docs exist,
                    # otherwise ready to (re)generate
                    sv.state["status"] = ("awaiting_review" if docs_exist
                                          else "idle")
                    ss.supervisor = sv
                    _audit("Resumed previous session "
                           f"(saved {snap.get('saved_at', '?')})")
                    ss.step = 5 if docs_exist else 4
                    st.rerun()
            with r2:
                if st.button("Start fresh (keep files on disk)",
                             width="stretch"):
                    os.remove(snap_path)
                    st.rerun()
        st.divider()

    st.header("Step 1 · Upload Input Data & Document Setup")
    c1, c2, c3 = st.columns(3)
    with c1:
        sys_name = st.text_input("System name", value=ss.get("sys_name", "System"))
    with c2:
        doc_title = st.text_input("Document title (cover page)",
                                  value=ss.get("doc_title", ""),
                                  placeholder="defaults to the system name")
    with c3:
        doc_number = st.text_input("Document number", value=config.DOC_NUMBER)

    uploads = st.file_uploader(
        "Connectivity / wiring files — canonical or free-form (.xlsx, .xls, .csv, .docx, .txt, .md)",
        type=["xlsx", "xls", "csv", "docx", "txt", "md"],
        accept_multiple_files=True)

    ref_up = st.file_uploader(
        "Optional: reference document about the system (design notes, spec, "
        "datasheet extracts) — improves the accuracy of the functional "
        "descriptions; skip it to proceed without",
        type=["txt", "md", "docx"], key="refdoc")

    with st.expander("Optional: your expectations for the outputs"):
        exp_bom = st.text_area("BOM expectations (verification depth, naming rules …)")
        exp_manual = st.text_area("Technical manual expectations (sections, emphasis, audience …)")
        bus_mode = st.checkbox(
            "Use signal-bus style for wiring diagrams: group 3+ signals "
            "between the same two components into one shared bus line "
            "with a tick+count label, instead of drawing every wire "
            "individually (you can also request/undo this later for any "
            "module through the chat assistant in the review step)",
            value=ss.get("bus_mode", False))
        st.caption("More detailed instructions, or requests you can't fully "
                   "articulate yet, can also be given later via the chat "
                   "assistant on the Agent Roster and Review steps — you "
                   "don't need to know how many sub-modules exist yet.")

    if uploads and st.button("Analyze Files", type="primary"):
        config.DOC_NUMBER = doc_number or config.DOC_NUMBER
        frames, hints, notes = [], {}, []
        for up in uploads:
            name, ext = up.name, os.path.splitext(up.name)[1].lower()
            try:
                if ext in (".xlsx", ".xls") and not is_canonical(up, name):
                    up.seek(0)
                    df = extract_workbook(up, filename=name, system_name=sys_name)
                    up.seek(0)
                    hints.update(extract_part_hints(up))
                    notes.append(f"🔎 {name}: free-form workbook — extracted "
                                 f"{len(df)} connections heuristically")
                else:
                    up.seek(0)
                    df = parse_any(up, filename=name)
                    notes.append(f"✅ {name}: canonical table — {len(df)} rows")
                frames.append(df)
            except Exception as e:
                notes.append(f"⚠ {name}: {e}")
        if frames:
            df_all = _canonize(pd.concat(frames, ignore_index=True), sys_name)
            df_all = df_all.drop_duplicates(
                subset=["Subsystem_Name", "Component_ID", "Source_Pin",
                        "Target_ID", "Target_Pin"]).reset_index(drop=True)
            ss.sys_name, ss.doc_title, ss.bus_mode = sys_name, doc_title, bus_mode
            ss.reference_context = _read_reference(ref_up) if ref_up else ""
            ss.expectations = "; ".join(x for x in (exp_bom, exp_manual) if x)
            ss.df_conn, ss.part_hints, ss.notes = df_all, hints, notes
            df_all.to_excel("input/draft_connectivity.xlsx", index=False)
            ss.audit = []
            _audit(f"Analyzed {len(uploads)} file(s) → {len(df_all)} connections, "
                   f"{df_all['Subsystem_Name'].nunique()} sub-modules")
            ss.step = 2
            st.rerun()
        else:
            for n in notes:
                st.error(n)

# ── Step 2: Review extracted connections ─────────────────────────────────
elif ss.step == 2:
    _back_button(1)
    st.header("Step 2 · Review Extracted Connections")
    for n in ss.get("notes", []):
        st.write(n)
    st.write(f"**{len(ss.df_conn)} connections** across "
             f"**{ss.df_conn['Subsystem_Name'].nunique()} sub-modules**. "
             "Edit cells, use the row trash icon to delete extraneous/junk "
             "rows, or add missing ones. Tip: rename **Subsystem_Name** "
             "values to split a huge tab into several smaller diagram "
             "sheets / manual chapters.")
    edited = st.data_editor(ss.df_conn[_EDIT_COLS], num_rows="dynamic",
                            width="stretch", height=430, key="conn_editor")

    def _apply_conn(sug):
        if sug["action"] == "drop_rows":
            before = len(ss.df_conn)
            ss.df_conn = ss.df_conn.drop(
                index=[i for i in sug["payload"] if i in ss.df_conn.index]
            ).reset_index(drop=True)
            n = before - len(ss.df_conn)
            _audit(f"SME (connections): removed {n} connection(s) — {sug['text'][:80]}")
            return f"✅ Applied #{sug['n']}: removed {n} connection(s). Table refreshed."
        return f"#{sug['n']} is informational — nothing to apply."

    _sme_chat_panel("connections", "sme_sug_conn", "sme_chat_conn",
                    lambda: sme_connection_review(ss.df_conn), _apply_conn)

    col_a, col_b, col_c = st.columns([1, 2, 1])
    with col_a:
        st.download_button("Download connections (xlsx)",
                           _xlsx_bytes(ss.df_conn), "draft_connectivity.xlsx")
    with col_b:
        up = st.file_uploader("…or upload a corrected connections file",
                              type=["xlsx"], key="conn_up")
        if up is not None:
            dv = pd.read_excel(up, dtype=str).fillna("")
            need = ["Component_ID", "Source_Pin", "Target_ID",
                    "Target_Pin", "Signal_Name"]
            missing = [c for c in need if c not in dv.columns]
            if not missing:
                ss.df_conn = _canonize(dv, ss.sys_name)
                st.success(f"Loaded {len(ss.df_conn)} connections — press Confirm.")
            else:
                st.error(f"Missing columns: {missing}")
    with col_c:
        if st.button("Confirm Connections →", type="primary"):
            before = len(ss.df_conn)
            ss.df_conn = _canonize(edited, ss.sys_name)
            ss.df_conn.to_excel("input/draft_connectivity.xlsx", index=False)
            delta = before - len(ss.df_conn)
            _audit(f"Connections confirmed: {len(ss.df_conn)}"
                   + (f" ({delta} removed in review)" if delta > 0 else ""))
            ss.inventory = build_inventory(ss.df_conn, ss.get("part_hints", {}))
            ss.step = 3
            st.rerun()

# ── Step 3: Component inventory / BOM curation loop ──────────────────────
elif ss.step == 3:
    _back_button(2)
    st.header("Step 3 · Component Inventory & BOM Curation")
    inv = ss.inventory
    n_ref = int((~inv["Item_Type"].isin(
        ["Component", "Module", "Sub-system", "Connector", "Cable"])).sum())
    st.write("Use the row trash icon to delete extraneous/junk rows (stray "
             "tokens the extractor misread as components) — this also "
             "removes the connections that reference them. Set **Item_Type** "
             "for every row: only physical items (components, modules, "
             "connectors, cables) enter the BOM; signal names / test points "
             f"/ terminations ({n_ref} so far) are saved as internal "
             "reference data.")

    edited = st.data_editor(
        inv, width="stretch", height=430, key="inv_editor",
        disabled=["Component_ID", "Connections", "Status"], num_rows="dynamic",
        column_config={"Item_Type": st.column_config.SelectboxColumn(
            "Item_Type", options=ITEM_TYPES, required=True)})

    missing_now = int((edited["Status"] != "OK").sum())
    if missing_now:
        st.warning(f"**{missing_now} of {len(edited)} items** still need "
                   "Make / Model / Part_Number.")
    else:
        st.success(f"All {len(edited)} items have part information. ✔")

    def _apply_inv(sug):
        if sug["action"] == "drop_components":
            ids = set(sug["payload"])
            ss.inventory = ss.inventory[
                ~ss.inventory["Component_ID"].astype(str).isin(ids)
            ].reset_index(drop=True)
            keep = ss.inventory["Component_ID"].astype(str).tolist()
            ss.df_conn, n_removed = drop_components(ss.df_conn, keep)
            _audit(f"SME (BOM): deleted junk components {sorted(ids)} "
                   f"(+{n_removed} connections)")
            return (f"✅ Applied #{sug['n']}: deleted {sorted(ids)} and "
                    f"{n_removed} connection(s) that referenced them.")
        return f"#{sug['n']} is informational — edit the table to act on it."

    _sme_chat_panel("BOM / inventory", "sme_sug_inv", "sme_chat_inv",
                    lambda: sme_inventory_review(ss.inventory), _apply_inv)

    col_a, col_b, col_c, col_d = st.columns([1, 2, 1, 1])
    with col_a:
        st.download_button("Download BOM worksheet",
                           _xlsx_bytes(edited), "bom_worksheet.xlsx")
    with col_b:
        up = st.file_uploader("Upload curated worksheet", type=["xlsx"], key="inv_up")
        if up is not None:
            dv = pd.read_excel(up, dtype=str).fillna("")
            if "Component_ID" in dv.columns:
                merged = inv.set_index("Component_ID")
                dv = dv.set_index("Component_ID")
                for c in ("Item_Type", "Make", "Model", "Part_Number",
                          "Type", "Description"):
                    if c in dv.columns:
                        upd = dv[c].reindex(merged.index)
                        merged[c] = upd.where(
                            upd.notna() & (upd.astype(str).str.strip() != ""),
                            merged[c])
                ss.inventory = _revalidate(merged.reset_index())
                st.success(f"Merged part data from {up.name}")
                st.rerun()
            else:
                st.error("The worksheet must keep the Component_ID column")
    with col_c:
        if st.button("Re-validate"):
            ss.inventory = _revalidate(edited)
            st.rerun()
    with col_d:
        if missing_now:
            if st.button("Review Gaps →", type="primary", width="stretch"):
                ss.inventory = edited
                ss.show_gaps = True
                st.rerun()
        else:
            if st.button("Build BOM →", type="primary", width="stretch"):
                _finalize_bom(edited)

    if ss.show_gaps and missing_now:
        st.divider()
        st.subheader(f"⚠ {missing_now} item(s) still missing part information")
        st.write("One more chance to fill these in before generation starts "
                 "— download the report, complete it, and re-upload above, "
                 "or proceed and they'll be flagged as TBD in the manual.")
        gaps = gaps_report(edited)
        st.dataframe(gaps, width="stretch")
        g1, g2, g3 = st.columns(3)
        with g1:
            st.download_button("Download gaps report (xlsx)",
                               _xlsx_bytes(gaps), "bom_gaps_report.xlsx")
        with g2:
            if st.button("← Back and fix", width="stretch"):
                ss.show_gaps = False
                st.rerun()
        with g3:
            if st.button("Proceed anyway (rest TBD) →", type="primary", width="stretch"):
                _finalize_bom(edited)

    with st.expander("Preview aggregated BOM (physical items only, with quantities)"):
        st.dataframe(inventory_to_bom(edited), width="stretch")

# ── Step 4: Agent roster acceptance + pre-generation chat ────────────────
elif ss.step == 4:
    sv = ss.supervisor
    _back_button(3, "← Back to BOM")
    st.header("Step 4 · Agent Roster — review & accept")
    st.write(f"Hardware detected: **{describe_profile()}**")
    if sv.state.get("bus_mode"):
        st.caption("Diagram style: signal-bus grouping enabled for wiring sheets.")
    st.write("These agents will work on your documentation. Verification "
             "agents run automatically and request rework from the "
             "generation agents when they find problems.")
    st.dataframe(pd.DataFrame(sv.planned_agents()), width="stretch", hide_index=True)

    df_subs_names = [str(x) for x in
                     ss.df_conn["Subsystem_Name"].unique()]
    st.subheader("Planned outputs — how each module will be documented")
    st.caption("Based on your instructions so far. Adjust in the chat below "
               "('make TOP a detailed schematic', 'use block bus for all "
               "dense modules') — this table updates. Accept & Start is your "
               "go-ahead.")
    st.dataframe(pd.DataFrame(sv.planned_outputs()), width="stretch",
                 hide_index=True)

    st.subheader("Chat with the assistant (optional)")
    st.caption("Ask anything about the data being processed ('what is J6 "
               "connected to?', 'list the sub-modules', 'how many "
               "components?') or give instructions in your own words "
               "('use bus-style wiring diagrams', 'make descriptions more "
               "elaborate', 'the power section is critical'). Instructions "
               "are remembered and applied during generation. Skip this if "
               "you have nothing to add.")
    _render_chat(ss.setup_chat)
    note = st.chat_input("Ask a question or give an instruction…")
    if note:
        ss.setup_chat.append({"role": "user", "content": note})
        new_title = _extract_title(note)
        if new_title:
            reply = _apply_title(sv, new_title, rebuild_docs=False)
        elif _is_question(note):
            reply = answer_data_question(note, ss.df_conn, ss.get("inventory"),
                                         extra_context=sv.state.get("expectations", ""))
        else:
            sv.state["expectations"] = (
                (sv.state.get("expectations", "") + "; ") if sv.state.get("expectations") else ""
            ) + note
            _audit(f"Instruction (Step 4): {note[:100]}")
            low = note.lower()
            reply = "Noted — I'll factor this in when generating the manual."
            wants_detail = any(w in low for w in (
                "no bus", "without bus", "individual wire", "full schematic",
                "detailed schematic", "all wiring"))
            mentions_bus = ("bus" in low or "block" in low) and not wants_detail
            # per-module style overrides when modules are named
            named = [m for m in df_subs_names
                     if re.search(r"\b" + re.escape(m.replace("_", " ").lower())
                                  + r"\b", low)
                     or re.search(r"\b" + re.escape(m.lower()) + r"\b", low)]
            if named and (mentions_bus or wants_detail):
                style = "block" if mentions_bus else "detail"
                for m in named:
                    sv.state["diagram_style_overrides"][m] = style
                reply += (f" Diagram style for {', '.join(named)} set to "
                          f"{'block + bus' if style == 'block' else 'detailed schematic'} "
                          "— see the Planned outputs table above.")
            elif mentions_bus:
                sv.state["bus_mode"] = True
                reply += (" Signal-bus style enabled: dense modules (>50 "
                          "connections) become block+bus diagrams; see the "
                          "Planned outputs table above for exactly which.")
            elif wants_detail and not named:
                sv.state["bus_mode"] = False
                sv.state["diagram_style_overrides"].clear()
                reply += " All modules will use detailed pin-level schematics."
            if llm_available():
                para = ask_llm(
                    "Restate this documentation instruction in one precise "
                    f"sentence (add nothing new): \"{note}\"", max_tokens=80)
                if para and 10 < len(para) < 300:
                    reply += f"\n\nMy understanding: {para.strip()}"
        ss.setup_chat.append({"role": "assistant", "content": reply})
        st.rerun()

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Accept & Start ▶", type="primary", width="stretch"):
            ss.step = 5
            st.rerun()
    with c2:
        if st.button("← Back to BOM", width="stretch", key="back4b"):
            ss.step = 3
            st.rerun()

# ── Step 5: Generate → review → chat-driven rework → approve ────────────
elif ss.step == 5:
    sv = ss.supervisor
    _back_button(4, "← Back to Agent Roster")
    st.header("Step 5 · Generate & Review the Manual")
    st.info(f"Status: {sv.state['status']} | Cycle: #{sv.state['cycle_count']}")

    if sv.state["status"] in ("idle", "cancelled"):
        if sv.state["status"] == "cancelled":
            st.warning("The previous run was cancelled at your request — "
                       "partial results were kept. Run again when ready, or "
                       "go back to adjust the inputs.")

        # ── Context so far: every preference & correction made upstream ──
        with st.expander("📋 Context so far — preferences & corrections that "
                         "will shape this run", expanded=True):
            st.markdown(
                f"- **System:** {ss.get('sys_name', 'System')} · "
                f"**Doc title:** {ss.get('doc_title') or '(system name)'} · "
                f"**Doc no:** {config.DOC_NUMBER}\n"
                f"- **Data:** {len(ss.df_conn)} connections, "
                f"{ss.df_conn['Subsystem_Name'].nunique()} sub-modules, "
                f"{len(ss.get('inventory', []))} BOM items "
                f"({int((ss.inventory['Status'] != 'OK').sum()) if 'inventory' in ss else 0} TBD)\n"
                f"- **Diagram style:** "
                f"{'signal-bus grouping' if sv.state.get('bus_mode') else 'individual wires'}\n"
                f"- **Reference document:** "
                f"{'provided (' + str(len(ss.get('reference_context', ''))) + ' chars)' if ss.get('reference_context') else 'none'}")
            if sv.state.get("expectations"):
                st.markdown("**Your instructions:**")
                for e in sv.state["expectations"].split("; "):
                    if e.strip():
                        st.markdown(f"- {e.strip()}")
            if ss.audit:
                st.markdown("**Corrections made during curation:**")
                for a in ss.audit:
                    st.markdown(f"- {a}")

        if st.button("Run Pipeline ▶", type="primary"):
            sv.state["status"] = "generating"
            sv.progress_callback = None       # background thread: log-only
            t = threading.Thread(target=sv.run_generation_cycle, daemon=True)
            t.start()
            ss.pipe_thread = t
            st.rerun()

    elif sv.state["status"] == "generating":
        st.info("⚙ Pipeline running — you can pause or cancel at any time; "
                "it stops at the next safe checkpoint.")
        paused = sv.control.get("pause", False)
        b1, b2, b3 = st.columns(3)
        with b1:
            if not paused and st.button("⏸ Pause", width="stretch"):
                sv.pause()
                st.rerun()
            if paused and st.button("▶ Resume", width="stretch", type="primary"):
                sv.resume()
                st.rerun()
        with b2:
            if st.button("⛔ Cancel", width="stretch"):
                sv.cancel()
                st.rerun()
        with b3:
            st.caption("Paused ⏸ — holding at checkpoint" if paused
                       else "Running…")
        st.code("\n".join(sv.progress_log[-25:]) or "starting…",
                language=None)
        t = ss.get("pipe_thread")
        if t is not None and t.is_alive():
            time.sleep(1.5)
            st.rerun()
        else:
            st.rerun()

    elif sv.state["status"] in ("awaiting_review", "reviewing"):
        st.success("Draft generated — review below. Use the chat assistant "
                   "at the bottom to request changes for any module, "
                   "System Overview, or all of them: name what you want "
                   "changed and the assistant will confirm before applying it.")

        dr = sv.state.get("diagram_reviews", {})
        sr = sv.state.get("sme_reviews", {})
        ok_d = sum(1 for r in dr.values() if r.get("status") == "pass")
        ok_s = sum(1 for r in sr.values() if r.get("status") == "pass")
        st.caption(f"Diagram Review agent: {ok_d}/{len(dr)} sheets verified ✓ · "
                   f"SME Review agent: {ok_s}/{len(sr)} descriptions approved ✓")

        pending_targets = (ss.pending_action or {}).get("targets", [])

        sys_diag = sv.state["diagrams"].get("System_Overview")
        if sys_diag and os.path.exists(sys_diag):
            badge = "🟧 " if "System Overview" in pending_targets else ""
            with st.expander(f"{badge}System Overview", expanded=True):
                st.image(sys_diag)
                for k in sorted(k for k in sv.state["diagrams"]
                                if k.startswith("System_Overview_Sheet")):
                    st.image(sv.state["diagrams"][k])
                st.write(sv.state.get("system_description", ""))

        for sn, d in sv.state["descriptions"].items():
            dmark = "✓" if dr.get(sn, {}).get("status") == "pass" else "⚠"
            smark = "✓" if sr.get(sn, {}).get("status") == "pass" else "⚠"
            badge = "🟧 " if sn in pending_targets else ""
            with st.expander(f"{badge}{sn.replace('_', ' ')}  ·  diagram {dmark} · text {smark}"):
                ip = sv.state["diagrams"].get(sn)
                if ip and os.path.exists(ip):
                    st.image(ip)
                st.caption("Diagram verification: "
                           + dr.get(sn, {}).get("detail", "n/a"))
                if isinstance(d, dict):
                    st.write(d.get("full_description", ""))
                    if d.get("role_in_system"):
                        st.markdown("**Role in the Overall System**")
                        st.write(d["role_in_system"])

        st.divider()
        st.subheader("💬 Chat with the assistant to request changes")
        st.caption("Name a module (e.g. 'BB3'), say 'System Overview', or "
                   "'all modules' — the assistant restates what it "
                   "understood and asks you to confirm before changing "
                   "anything.")
        _render_chat(ss.review_chat)
        user_msg = st.chat_input("What would you like to change?")
        if user_msg:
            ss.review_chat.append({"role": "user", "content": user_msg})
            module_names = _module_display_names(sv)
            intent = _classify_message(user_msg, module_names)

            if ss.pending_action and intent["kind"] == "confirm":
                pa = ss.pending_action
                lines = []
                with st.spinner("Applying feedback..."):
                    box = st.empty()
                    sv.progress_callback = lambda stage, detail, _b=box: _b.write(
                        f"{stage}" + (f" — {detail}" if detail else ""))
                    for tgt in pa["targets"]:
                        if tgt == "System Overview":
                            sv.rework_system(pa["instruction"])
                            lines.append("✅ System Overview updated.")
                        else:
                            sv.rework_module(tgt, pa["instruction"])
                            lines.append(f"✅ {tgt.replace('_', ' ')} updated.")
                    sv.progress_callback = None
                    box.empty()
                ss.review_chat.append({"role": "assistant", "content": "\n".join(lines)})
                ss.pending_action = None
                st.rerun()

            elif ss.pending_action and intent["kind"] == "cancel":
                ss.review_chat.append({"role": "assistant",
                                       "content": "Cancelled — no changes made."})
                ss.pending_action = None
                st.rerun()

            elif _extract_title(user_msg):
                reply = _apply_title(sv, _extract_title(user_msg),
                                     rebuild_docs=True)
                ss.review_chat.append({"role": "assistant", "content": reply})
                st.rerun()

            else:
                targets = intent.get("targets", [])
                instruction = intent.get("instruction", user_msg)
                if not targets:
                    avail = ", ".join(m.replace("_", " ") for m in module_names)
                    reply = (f"I couldn't tell which module you mean. Available: "
                             f"{avail}, or **System Overview**, or say "
                             f"'all modules'. Could you clarify?")
                else:
                    tgt_list = ", ".join(t if t == "System Overview"
                                         else t.replace("_", " ") for t in targets)
                    restated = instruction
                    if llm_available():
                        paraphrase = ask_llm(
                            "Rephrase this documentation change request in one "
                            "short, precise sentence, keeping the same meaning "
                            f"(do not add anything new): \"{instruction}\"",
                            max_tokens=80)
                        if paraphrase and 10 < len(paraphrase) < 300:
                            restated = paraphrase.strip()
                    reply = (f"I'll apply this to **{tgt_list}**: \"{restated}\"\n\n"
                             "Reply **yes** to proceed, or tell me more / a "
                             "different module.")
                    ss.pending_action = {"targets": targets, "instruction": instruction}
                ss.review_chat.append({"role": "assistant", "content": reply})
                st.rerun()

        col1, col2, col3 = st.columns(3)
        with col1:
            if os.path.exists(sv.state["docs"].get("docx", "")):
                with open(sv.state["docs"]["docx"], "rb") as f:
                    st.download_button("Download DOCX", f,
                                       "Technical_User_Manual.docx", width="stretch")
        with col2:
            if os.path.exists(sv.state["docs"].get("pdf", "")):
                with open(sv.state["docs"]["pdf"], "rb") as f:
                    st.download_button("Download PDF", f,
                                       "Technical_User_Manual.pdf", width="stretch")
        with col3:
            st.download_button("Download BOM (xlsx)",
                               _xlsx_bytes(ss.df_bom), "BOM_curated.xlsx",
                               width="stretch")

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("Approve ✔", type="primary", width="stretch"):
                sv.approve()
                st.balloons()
                st.rerun()
        with col2:
            if st.button("↻ Full Regenerate (entire pipeline)", width="stretch"):
                sv.state["status"] = "generating"
                sv.progress_callback = None
                t = threading.Thread(target=sv.run_generation_cycle, daemon=True)
                t.start()
                ss.pipe_thread = t
                st.rerun()
        with col3:
            if st.button("← Back to BOM", width="stretch"):
                ss.step = 3
                st.rerun()

    elif sv.state["status"] == "approved":
        st.success("Manual approved — final documents below.")
        if os.path.exists(sv.state["docs"].get("docx", "")):
            with open(sv.state["docs"]["docx"], "rb") as f:
                st.download_button("Download DOCX", f,
                                   "Technical_User_Manual.docx", width="stretch")
        if os.path.exists(sv.state["docs"].get("pdf", "")):
            with open(sv.state["docs"]["pdf"], "rb") as f:
                st.download_button("Download PDF", f,
                                   "Technical_User_Manual.pdf", width="stretch")
        if st.button("Start a new manual"):
            for k in ("step", "supervisor", "df_conn", "df_bom", "inventory",
                      "part_hints", "notes", "reference_context",
                      "expectations", "show_gaps", "bus_mode",
                      "setup_chat", "review_chat", "pending_action"):
                ss.pop(k, None)
            st.rerun()
