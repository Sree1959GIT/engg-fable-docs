"""web_ui.py — Streamlit wizard for the documentation pipeline (v9).

Workflow:
  1. UPLOAD       multiple files (canonical or free-form) + document title /
                  number + optional reference document + output expectations
  2. CONNECTIONS  review/correct extracted connectivity
  3. BOM          component inventory with Item_Type; only physical items
                  enter the BOM (signals/test points → internal reference);
                  iterate until part data is complete or proceed with TBD
  4. AGENTS       the app presents the agent roster (sized to the detected
                  hardware) for acceptance before work starts
  5. GENERATE     pipeline with live agent status (diagram verification +
                  SME review) → draft preview → per-module rework buttons →
                  Approve → download DOCX / PDF / BOM
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
    extract_part_hints,
    extract_workbook,
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

_EDIT_COLS = ["Subsystem_Name", "Component_ID", "Source_Pin",
              "Target_ID", "Target_Pin", "Signal_Name"]


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
            ss.sys_name, ss.doc_title = sys_name, doc_title
            ss.reference_context = _read_reference(ref_up) if ref_up else ""
            ss.expectations = "; ".join(x for x in (exp_bom, exp_manual) if x)
            ss.df_conn, ss.part_hints, ss.notes = df_all, hints, notes
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
             "Edit cells, delete garbage rows, add missing ones. "
             "Tip: rename **Subsystem_Name** values to split a huge tab into "
             "several smaller diagram sheets / manual chapters.")
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
            ss.inventory = build_inventory(ss.df_conn, ss.get("part_hints", {}))
            ss.step = 3
            st.rerun()

# ── Step 3: Component inventory / BOM curation loop ──────────────────────
elif ss.step == 3:
    st.header("Step 3 · Component Inventory & BOM Curation")
    inv = ss.inventory
    missing = int((inv["Status"] != "OK").sum())
    total = len(inv)
    n_ref = int((~inv["Item_Type"].isin(
        ["Component", "Module", "Sub-system", "Connector", "Cable"])).sum())
    if missing:
        st.warning(f"**{missing} of {total} items** still need Make / Model / "
                   "Part_Number. Set **Item_Type** for every row — only "
                   "physical items (components, modules, connectors, cables) "
                   "enter the BOM; signal names / test points / terminations "
                   f"({n_ref} so far) are saved as internal reference data. "
                   "You may proceed with the rest marked TBD.")
    else:
        st.success(f"All {total} items have part information. ✔")

    edited = st.data_editor(
        inv, width="stretch", height=430, key="inv_editor",
        disabled=["Component_ID", "Connections", "Status"], num_rows="fixed",
        column_config={"Item_Type": st.column_config.SelectboxColumn(
            "Item_Type", options=ITEM_TYPES, required=True)})

    col_a, col_b, col_c, col_d = st.columns([1, 2, 1, 1])
    with col_a:
        st.download_button("Download BOM worksheet",
                           _xlsx_bytes(inv), "bom_worksheet.xlsx")
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
        label = "Proceed (rest TBD) →" if missing else "Build BOM →"
        if st.button(label, type="primary"):
            ss.inventory = edited
            ss.df_conn = merge_inventory(ss.df_conn, edited)
            ss.df_bom = inventory_to_bom(edited)
            _, reference_items = split_reference_items(edited)
            os.makedirs("input", exist_ok=True)
            reference_items.to_excel("input/system_reference.xlsx", index=False)
            ss.supervisor = SupervisorAgent(
                ss.df_conn, ss.df_bom,
                doc_title=ss.get("doc_title", ""),
                reference_context=ss.get("reference_context", ""),
                expectations=ss.get("expectations", ""))
            ss.step = 4
            st.rerun()

    with st.expander("Preview aggregated BOM (physical items only, with quantities)"):
        st.dataframe(inventory_to_bom(edited), width="stretch")

# ── Step 4: Agent roster acceptance ───────────────────────────────────────
elif ss.step == 4:
    sv = ss.supervisor
    st.header("Step 4 · Agent Roster — review & accept")
    st.write(f"Hardware detected: **{describe_profile()}**")
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

# ── Step 5: Generate → review → per-module rework → approve ─────────────
elif ss.step == 5:
    sv = ss.supervisor
    st.header("Step 5 · Generate & Review the Manual")
    st.info(f"Status: {sv.state['status']} | Cycle: #{sv.state['cycle_count']}")

    if sv.state["status"] in ["idle", "reviewing"]:
        fb = st.text_area("Global rework feedback (optional):")
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
        st.success("Draft generated — review each module below. Use the "
                   "per-module feedback boxes for incremental rework, or "
                   "Approve when satisfied.")

        # verification summary
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
            with st.expander(f"{sn.replace('_', ' ')}  ·  diagram {dmark} · text {smark}"):
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
                fb_key = f"fb_{sn}"
                mod_fb = st.text_input("Feedback for THIS module only", key=fb_key)
                if st.button(f"Rework {sn.replace('_', ' ')} ↺", key=f"rw_{sn}"):
                    if mod_fb.strip():
                        with st.spinner(f"Reworking {sn}..."):
                            sv.rework_module(sn, mod_fb.strip())
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
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("Approve ✔", type="primary", width="stretch"):
                sv.approve()
                st.balloons()
                st.rerun()
        with col2:
            if st.button("Request Full Rework ↺", width="stretch"):
                sv.state["status"] = "reviewing"
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
                      "part_hints", "notes", "reference_context", "expectations"):
                ss.pop(k, None)
            st.rerun()
