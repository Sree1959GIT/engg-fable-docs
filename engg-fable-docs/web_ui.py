"""web_ui.py — Streamlit wizard for the documentation pipeline.

Workflow (matches the production process):

  1. UPLOAD      one or more files — canonical tables OR free-form wiring
                 workbooks (multi-tab, no headers). Free-form files go
                 through the heuristic extractor (src/wiring_extractor.py).
  2. CONNECTIONS review/correct the extracted connectivity in an editable
                 table (delete garbage rows, fix pins/signals/subsystems).
  3. BOM         the app lists every component/module it found; the user
                 adds Make / Model / Part_Number (inline, or download →
                 edit in Excel → upload). Iterate until complete, or
                 proceed with the remainder marked TBD.
  4. GENERATE    multi-agent pipeline → draft manual preview → Approve or
                 Request Rework (with feedback) → download DOCX / PDF.
"""
import io
import os

import pandas as pd
import streamlit as st

from src.input_parser import parse_any
from src.llm_client import llm_available
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

st.set_page_config(page_title="Engineering Documentation Generator", layout="wide")
st.title("Engineering Documentation Generator")

if llm_available():
    st.caption("🟢 Local LLM online — AI-enhanced descriptions enabled")
else:
    st.caption("🟠 Local LLM offline — programmatic mode (documents are still "
               "generated; start llama.cpp on :8080 for AI-enhanced prose)")

ss = st.session_state
ss.setdefault("step", 1)
ss.setdefault("supervisor", None)

_EDIT_COLS = ["Subsystem_Name", "Component_ID", "Source_Pin",
              "Target_ID", "Target_Pin", "Signal_Name"]


def _xlsx_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _canonize(df: pd.DataFrame, sys_name: str) -> pd.DataFrame:
    """Fill in the canonical column set and drop incomplete rows."""
    df = df.copy()
    for c in CANONICAL_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df["System_Name"] = sys_name
    df = df.fillna("")
    df = df[(df["Component_ID"].astype(str).str.strip() != "")
            & (df["Target_ID"].astype(str).str.strip() != "")]
    return df[CANONICAL_COLUMNS].reset_index(drop=True)


# ── Step 1: Upload ────────────────────────────────────────────────────────
if ss.step == 1:
    st.header("Step 1 · Upload Input Data")
    st.write("Upload one or **more** files. Canonical connectivity tables are "
             "used as-is; free-form wiring workbooks (multi-tab, no fixed "
             "columns) are analyzed and their connections extracted for your "
             "review in the next step.")
    sys_name = st.text_input("System name (used on the cover page and diagrams)",
                             value=ss.get("sys_name", "System"))
    uploads = st.file_uploader(
        "Connectivity / wiring files (.xlsx, .xls, .csv, .docx, .txt, .md)",
        type=["xlsx", "xls", "csv", "docx", "txt", "md"],
        accept_multiple_files=True)

    if uploads and st.button("Analyze Files", type="primary"):
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
            df_all = pd.concat(frames, ignore_index=True)
            df_all = _canonize(df_all, sys_name).drop_duplicates(
                subset=["Subsystem_Name", "Component_ID", "Source_Pin",
                        "Target_ID", "Target_Pin"]).reset_index(drop=True)
            ss.sys_name = sys_name
            ss.df_conn = df_all
            ss.part_hints = hints
            ss.notes = notes
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
             "Correct anything the extractor got wrong: edit cells, delete "
             "garbage rows, add missing ones. **Subsystem_Name** groups "
             "connections into diagram sheets / manual chapters.")
    edited = st.data_editor(ss.df_conn[_EDIT_COLS], num_rows="dynamic",
                            use_container_width=True, height=430,
                            key="conn_editor")

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
                st.success(f"Loaded {len(ss.df_conn)} connections from {up.name} "
                           "— press Confirm to continue.")
            else:
                st.error(f"Missing columns: {missing}")
    with col_c:
        if st.button("Confirm Connections →", type="primary"):
            ss.df_conn = _canonize(edited, ss.sys_name)
            ss.inventory = build_inventory(ss.df_conn, ss.get("part_hints", {}))
            ss.step = 3
            st.rerun()

# ── Step 3: Component inventory / BOM curation loop ──────────────────────
elif ss.step == 3:
    st.header("Step 3 · Component Inventory & BOM Curation")
    inv = ss.inventory
    missing = int((inv["Status"] != "OK").sum())
    total = len(inv)
    if missing:
        st.warning(f"**{missing} of {total} components** still need "
                   "Make / Model / Part_Number. Fill them in below (or "
                   "download the worksheet, complete it in Excel and upload "
                   "it back), then press **Re-validate**. You may also "
                   "proceed now — the remaining entries are marked TBD and "
                   "the manual flags them for confirmation.")
    else:
        st.success(f"All {total} components have part information. ✔")

    edited = st.data_editor(
        inv, use_container_width=True, height=430, key="inv_editor",
        disabled=["Component_ID", "Connections", "Status"], num_rows="fixed")

    col_a, col_b, col_c, col_d = st.columns([1, 2, 1, 1])
    with col_a:
        st.download_button("Download BOM worksheet",
                           _xlsx_bytes(inv), "bom_worksheet.xlsx")
    with col_b:
        up = st.file_uploader("Upload curated worksheet", type=["xlsx"],
                              key="inv_up")
        if up is not None:
            dv = pd.read_excel(up, dtype=str).fillna("")
            if "Component_ID" in dv.columns:
                merged = inv.set_index("Component_ID")
                dv = dv.set_index("Component_ID")
                for c in ("Make", "Model", "Part_Number", "Type", "Description"):
                    if c in dv.columns:
                        upd = dv[c].reindex(merged.index)
                        merged[c] = upd.where(upd.notna() & (upd.astype(str).str.strip() != ""),
                                              merged[c])
                inv2 = merged.reset_index()
                inv2["Status"] = [
                    "OK" if (str(r["Make"]).strip() or str(r["Model"]).strip()
                             or str(r["Part_Number"]).strip())
                    else "MISSING — add Make/Model/Part_Number"
                    for _, r in inv2.iterrows()]
                ss.inventory = inv2
                st.success(f"Merged part data from {up.name}")
                st.rerun()
            else:
                st.error("The worksheet must keep the Component_ID column")
    with col_c:
        if st.button("Re-validate"):
            ed = edited.copy()
            ed["Status"] = [
                "OK" if (str(r["Make"]).strip() or str(r["Model"]).strip()
                         or str(r["Part_Number"]).strip())
                else "MISSING — add Make/Model/Part_Number"
                for _, r in ed.iterrows()]
            ss.inventory = ed
            st.rerun()
    with col_d:
        label = "Proceed (rest TBD) →" if missing else "Build BOM →"
        if st.button(label, type="primary"):
            ss.inventory = edited
            ss.df_conn = merge_inventory(ss.df_conn, edited)
            ss.df_bom = inventory_to_bom(edited)
            ss.supervisor = SupervisorAgent(ss.df_conn, ss.df_bom)
            ss.step = 4
            st.rerun()

    with st.expander("Preview aggregated BOM (as it will appear in the manual)"):
        st.dataframe(inventory_to_bom(edited), use_container_width=True)

# ── Step 4: Generate → review → rework/approve → download ────────────────
elif ss.step == 4:
    sv = ss.supervisor
    st.header("Step 4 · Generate & Review the Manual")
    st.info(f"Status: {sv.state['status']} | Cycle: #{sv.state['cycle_count']}")

    if sv.state["status"] in ["idle", "reviewing"]:
        fb = st.text_area("Rework feedback (optional — addressed on the next run):")
        if st.button("Run / Rerun Pipeline", type="primary"):
            sv.state["rework_feedback"] = fb
            progress = st.status("Running pipeline...", expanded=True)
            sv.progress_callback = lambda stage, detail: progress.write(
                f"{stage}" + (f" — {detail}" if detail else ""))
            with st.spinner("Agents working..."):
                sv.run_generation_cycle()
            progress.update(label="Pipeline complete", state="complete")
            st.rerun()

    elif sv.state["status"] == "awaiting_review":
        st.success("Draft generated — review below, then Approve or Request Rework.")

        sys_diag = sv.state["diagrams"].get("System_Overview")
        if sys_diag and os.path.exists(sys_diag):
            with st.expander("System Overview", expanded=True):
                st.image(sys_diag)
                st.write(sv.state.get("system_description", ""))

        for sn, d in sv.state["descriptions"].items():
            with st.expander(sn.replace("_", " "), expanded=False):
                ip = sv.state["diagrams"].get(sn)
                if ip and os.path.exists(ip):
                    st.image(ip)
                if isinstance(d, dict):
                    st.write(d.get("full_description", ""))
                    if d.get("role_in_system"):
                        st.markdown("**Role in the Overall System**")
                        st.write(d["role_in_system"])

        col1, col2, col3 = st.columns(3)
        with col1:
            if os.path.exists(sv.state["docs"].get("docx", "")):
                with open(sv.state["docs"]["docx"], "rb") as f:
                    st.download_button("Download DOCX", f,
                                       "Technical_User_Manual.docx",
                                       use_container_width=True)
        with col2:
            if os.path.exists(sv.state["docs"].get("pdf", "")):
                with open(sv.state["docs"]["pdf"], "rb") as f:
                    st.download_button("Download PDF", f,
                                       "Technical_User_Manual.pdf",
                                       use_container_width=True)
        with col3:
            st.download_button("Download BOM (xlsx)",
                               _xlsx_bytes(ss.df_bom), "BOM_curated.xlsx",
                               use_container_width=True)
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("Approve ✔", type="primary", use_container_width=True):
                sv.approve()
                st.balloons()
                st.rerun()
        with col2:
            if st.button("Request Rework ↺", use_container_width=True):
                sv.state["status"] = "reviewing"
                st.rerun()
        with col3:
            if st.button("← Back to BOM", use_container_width=True):
                ss.step = 3
                st.rerun()

    elif sv.state["status"] == "approved":
        st.success("Manual approved — final documents below.")
        if os.path.exists(sv.state["docs"].get("docx", "")):
            with open(sv.state["docs"]["docx"], "rb") as f:
                st.download_button("Download DOCX", f, "Technical_User_Manual.docx",
                                   use_container_width=True)
        if os.path.exists(sv.state["docs"].get("pdf", "")):
            with open(sv.state["docs"]["pdf"], "rb") as f:
                st.download_button("Download PDF", f, "Technical_User_Manual.pdf",
                                   use_container_width=True)
        if st.button("Start a new manual"):
            for k in ("step", "supervisor", "df_conn", "df_bom", "inventory",
                      "part_hints", "notes"):
                ss.pop(k, None)
            st.rerun()
