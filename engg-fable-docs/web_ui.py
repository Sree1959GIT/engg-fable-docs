"""web_ui.py — Streamlit wizard for the documentation pipeline.

Decision (IMPROVEMENTS.md): Streamlit is kept for now — it is offline-friendly,
zero-build, and sufficient for a 3-step wizard. A React/React-Flow front end is
Phase 4 on the roadmap.
"""
import os

import pandas as pd
import streamlit as st

from src.bom_generator import generate_draft_bom
from src.input_parser import parse_any
from src.llm_client import llm_available
from supervisor import SupervisorAgent

st.set_page_config(page_title="Wiring Diagram Generator", layout="wide")
st.title("Engineering Documentation Generator")

# ── LLM status banner ──
if llm_available():
    st.caption("🟢 Local LLM online — AI-enhanced descriptions enabled")
else:
    st.caption("🟠 Local LLM offline — running in programmatic mode (documents will "
               "still be generated; start llama.cpp on :8080 for AI-enhanced prose)")

if "supervisor" not in st.session_state:
    st.session_state.supervisor = None
if "step" not in st.session_state:
    st.session_state.step = 1

if st.session_state.step == 1:
    st.header("Step 1: Upload Connectivity Data")
    raw = st.file_uploader("Upload connectivity data (.xlsx, .csv, .docx, .txt, .md)",
                           type=["xlsx", "xls", "csv", "docx", "txt", "md"])
    if raw and st.button("Analyze & Generate Draft BOM", type="primary"):
        with st.spinner("Parsing..."):
            st.session_state.df_conn = parse_any(raw, filename=raw.name)
        with st.spinner("Inferring part data..."):
            st.session_state.df_bom = generate_draft_bom(st.session_state.df_conn)
        st.session_state.step = 2
        st.rerun()

elif st.session_state.step == 2:
    st.header("Step 2: Review & Validate BOM")
    st.dataframe(st.session_state.df_bom, use_container_width=True)
    st.download_button(
        "Download Draft BOM (CSV)",
        st.session_state.df_bom.to_csv(index=False).encode(),
        "draft_bom.csv",
    )
    v = st.file_uploader("Upload Validated BOM", type=["xlsx"], key="bom")
    col_a, col_b = st.columns(2)
    with col_a:
        if v is not None:
            dv = pd.read_excel(v)
            req = ["Reference_IDs", "Make", "Model", "Part_Number", "Type", "Description", "Qty"]
            if all(c in dv.columns for c in req):
                st.session_state.df_bom = dv
                st.session_state.supervisor = SupervisorAgent(st.session_state.df_conn, dv)
                st.session_state.step = 3
                st.rerun()
            else:
                st.error(f"Missing columns: {[c for c in req if c not in dv.columns]}")
    with col_b:
        if st.button("Use Draft BOM As-Is"):
            st.session_state.supervisor = SupervisorAgent(
                st.session_state.df_conn, st.session_state.df_bom)
            st.session_state.step = 3
            st.rerun()

elif st.session_state.step == 3:
    sv = st.session_state.supervisor
    st.header("Step 3: Multi-Agent Pipeline")
    st.info(f"Status: {sv.state['status']} | Cycle: #{sv.state['cycle_count']}")

    if sv.state["status"] in ["idle", "reviewing"]:
        fb = st.text_area("Rework feedback (optional):")
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
        st.success("Draft generated!")

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

        col1, col2 = st.columns(2)
        with col1:
            if os.path.exists(sv.state["docs"].get("docx", "")):
                with open(sv.state["docs"]["docx"], "rb") as f:
                    st.download_button("Download DOCX", f, "Technical_User_Manual.docx",
                                       use_container_width=True)
        with col2:
            if os.path.exists(sv.state["docs"].get("pdf", "")):
                with open(sv.state["docs"]["pdf"], "rb") as f:
                    st.download_button("Download PDF", f, "Technical_User_Manual.pdf",
                                       use_container_width=True)
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Approve", type="primary", use_container_width=True):
                sv.approve()
                st.balloons()
                st.rerun()
        with col2:
            if st.button("Request Rework", use_container_width=True):
                sv.state["status"] = "reviewing"
                st.rerun()

    elif sv.state["status"] == "approved":
        st.success("Complete!")
        if os.path.exists(sv.state["docs"].get("docx", "")):
            with open(sv.state["docs"]["docx"], "rb") as f:
                st.download_button("Download DOCX", f, "Technical_User_Manual.docx",
                                   use_container_width=True)
        if os.path.exists(sv.state["docs"].get("pdf", "")):
            with open(sv.state["docs"]["pdf"], "rb") as f:
                st.download_button("Download PDF", f, "Technical_User_Manual.pdf",
                                   use_container_width=True)
