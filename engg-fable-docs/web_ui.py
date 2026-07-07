"""web_ui.py — Streamlit wizard for the documentation pipeline (v10).

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
                  hardware) for acceptance before work starts
  5. GENERATE     pipeline with live agent status (diagram verification +
                  SME review) → draft preview with a feedback box next to
                  every module (highlighted while pending) → targeted
                  rework of only the flagged modules → Approve → downloads

All inputs and curated data are also saved under input/ for traceability.
"""
import io
import os

import pandas as pd
import streamlit as st

from src import config
from src.hardware import describe_profile, detect_profile
from src.input_parser import parse_any
from src.llm_client import llm_available
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

_EDIT_COLS = ["Subsystem_Name", "Component_ID", "Source_Pin",
              "Target_ID", "Target_Pin", "Signal_Name"]
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
            "individually (you can also request/undo this per-module later "
            "in the review step by typing 'bus' in that module's feedback box)",
            value=ss.get("bus_mode", False))

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

# ── Step 4: Agent roster acceptance ───────────────────────────────────────
elif ss.step == 4:
    sv = ss.supervisor
    st.header("Step 4 · Agent Roster — review & accept")
    st.write(f"Hardware detected: **{describe_profile()}**")
    if ss.get("bus_mode"):
        st.caption("Diagram style: signal-bus grouping enabled for wiring sheets.")
    st.write("These agents will work on your documentation. Verification "
             "agents run automatically and request rework from the "
             "generation agents when they find problems.")
    st.dataframe(pd.DataFrame(sv.planned_agents()), width="stretch", hide_index=True)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Accept & Start ▶", type="primary", width="stretch"):
            ss.step = 5
            st.rerun()
    with c2:
        if st.button("← Back to BOM", width="stretch"):
            ss.step = 3
            st.rerun()

# ── Step 5: Generate → review → per-module feedback → approve ───────────
elif ss.step == 5:
    sv = ss.supervisor
    st.header("Step 5 · Generate & Review the Manual")
    st.info(f"Status: {sv.state['status']} | Cycle: #{sv.state['cycle_count']}")

    if sv.state["status"] == "idle":
        if st.button("Run Pipeline ▶", type="primary"):
            progress = st.status("Running pipeline...", expanded=True)
            sv.progress_callback = lambda stage, detail: progress.write(
                f"{stage}" + (f" — {detail}" if detail else ""))
            with st.spinner("Agents working..."):
                sv.run_generation_cycle()
            progress.update(label="Pipeline complete", state="complete")
            st.rerun()

    elif sv.state["status"] in ("awaiting_review", "reviewing"):
        st.success("Draft generated — expand a module, click 💬 Feedback to "
                   "add corrections or style instructions (e.g. 'use bus "
                   "style for this diagram'), then use Request Rework below "
                   "to apply ONLY the modules you've flagged.")

        dr = sv.state.get("diagram_reviews", {})
        sr = sv.state.get("sme_reviews", {})
        ok_d = sum(1 for r in dr.values() if r.get("status") == "pass")
        ok_s = sum(1 for r in sr.values() if r.get("status") == "pass")
        st.caption(f"Diagram Review agent: {ok_d}/{len(dr)} sheets verified ✓ · "
                   f"SME Review agent: {ok_s}/{len(sr)} descriptions approved ✓")

        sys_diag = sv.state["diagrams"].get("System_Overview")
        if sys_diag and os.path.exists(sys_diag):
            with st.expander("System Overview", expanded=True):
                st.image(sys_diag)
                for k in sorted(k for k in sv.state["diagrams"]
                                if k.startswith("System_Overview_Sheet")):
                    st.image(sv.state["diagrams"][k])
                st.write(sv.state.get("system_description", ""))

        for sn, d in sv.state["descriptions"].items():
            dmark = "✓" if dr.get(sn, {}).get("status") == "pass" else "⚠"
            smark = "✓" if sr.get(sn, {}).get("status") == "pass" else "⚠"
            fb_key = f"fbbox_{sn}"
            toggle_key = f"fbshow_{sn}"
            ss.setdefault(toggle_key, False)
            pending = bool(st.session_state.get(fb_key, "").strip())
            badge = "🟧 " if pending else ""

            header_col, btn_col = st.columns([6, 1])
            with header_col:
                exp = st.expander(
                    f"{badge}{sn.replace('_', ' ')}  ·  diagram {dmark} · text {smark}",
                    expanded=pending)
            with btn_col:
                if st.button("💬 Feedback", key=f"fbbtn_{sn}"):
                    ss[toggle_key] = not ss[toggle_key]

            with exp:
                if pending:
                    st.caption("🟧 Feedback pending — will be applied on Request Rework")
                if ss[toggle_key]:
                    left, right = st.columns([3, 2])
                else:
                    left, right = st.container(), None
                with left:
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
                if right is not None:
                    with right:
                        st.text_area(
                            "Feedback for this module (corrections, or "
                            "presentation/style instructions for the text "
                            "and/or diagram)", key=fb_key, height=220)
                        if st.button(f"Rework now ▶", key=f"nowbtn_{sn}"):
                            fb_text = st.session_state.get(fb_key, "").strip()
                            if fb_text:
                                with st.spinner(f"Reworking {sn.replace('_', ' ')}..."):
                                    sv.rework_module(sn, fb_text)
                                st.session_state[fb_key] = ""
                                st.rerun()
                            else:
                                st.warning("Enter feedback text first.")

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

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if st.button("Approve ✔", type="primary", width="stretch"):
                sv.approve()
                st.balloons()
                st.rerun()
        with col2:
            if st.button("Request Rework ↺ (flagged modules only)",
                         width="stretch"):
                targets = [sn for sn in sv.state["descriptions"]
                          if st.session_state.get(f"fbbox_{sn}", "").strip()]
                if not targets:
                    st.info("Nothing to rework — no feedback was entered "
                            "for any module.")
                else:
                    prog = st.status(f"Reworking {len(targets)} module(s)...",
                                     expanded=True)
                    for sn in targets:
                        fb_text = st.session_state[f"fbbox_{sn}"].strip()
                        prog.write(f"Reworking {sn.replace('_', ' ')}...")
                        sv.rework_module(sn, fb_text)
                        st.session_state[f"fbbox_{sn}"] = ""
                        prog.write(f"✓ {sn.replace('_', ' ')} done")
                    prog.update(label="Rework complete", state="complete")
                    st.rerun()
        with col3:
            if st.button("↻ Full Regenerate (entire pipeline)", width="stretch"):
                progress = st.status("Regenerating everything...", expanded=True)
                sv.progress_callback = lambda stage, detail: progress.write(
                    f"{stage}" + (f" — {detail}" if detail else ""))
                with st.spinner("Agents working..."):
                    sv.run_generation_cycle()
                progress.update(label="Pipeline complete", state="complete")
                st.rerun()
        with col4:
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
                      "expectations", "show_gaps", "bus_mode"):
                ss.pop(k, None)
            st.rerun()
