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

import pandas as pd
import streamlit as st

from src import config
from src.hardware import describe_profile, detect_profile
from src.input_parser import parse_any
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
                              " entire manual", " whole manual", " everything")):
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
            ss.df_conn = _canonize(edited, ss.sys_name)
            ss.df_conn.to_excel("input/draft_connectivity.xlsx", index=False)
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

    st.subheader("Tell the assistant about this system (optional)")
    st.caption("You don't need to know the sub-module names yet — describe "
               "what you want in your own words: 'use bus-style wiring "
               "diagrams', 'make the descriptions more elaborate', 'the "
               "power section is critical, be extra thorough there', "
               "paste in extra background, etc. Skip this if you have "
               "nothing to add.")
    _render_chat(ss.setup_chat)
    note = st.chat_input("Type instructions for the assistant, or leave blank and press Accept & Start")
    if note:
        ss.setup_chat.append({"role": "user", "content": note})
        sv.state["expectations"] = (
            (sv.state.get("expectations", "") + "; ") if sv.state.get("expectations") else ""
        ) + note
        low = note.lower()
        reply = "Noted — I'll factor this in when generating the manual."
        if "bus" in low and "no bus" not in low and "without bus" not in low:
            sv.state["bus_mode"] = True
            reply += " Signal-bus style wiring diagrams are now enabled."
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

    if sv.state["status"] == "idle":
        if st.button("Run Pipeline ▶", type="primary"):
            _run_with_progress(sv.run_generation_cycle,
                              spinner_label="Agents working...")
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
                _run_with_progress(sv.run_generation_cycle,
                                   spinner_label="Regenerating everything...")
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
