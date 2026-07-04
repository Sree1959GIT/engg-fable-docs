import streamlit as st
import pandas as pd, os
from src.excel_parser import parse_connectivity_data
from src.bom_generator import generate_draft_bom
from supervisor import SupervisorAgent

st.set_page_config(page_title="Wiring Diagram Generator", layout="wide")
st.title("Wiring Diagram & BOM Generator")
if "supervisor" not in st.session_state: st.session_state.supervisor = None
if "step" not in st.session_state: st.session_state.step = 1

if st.session_state.step == 1:
    st.header("Step 1: Upload Connectivity Data")
    raw = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx"])
    if raw and st.button("Analyze & Generate Draft BOM", type="primary"):
        with st.spinner("Parsing..."): st.session_state.df_conn = parse_connectivity_data(raw)
        with st.spinner("Gemma 4 inferring parts..."): st.session_state.df_bom = generate_draft_bom(st.session_state.df_conn)
        st.session_state.step = 2; st.rerun()

elif st.session_state.step == 2:
    st.header("Step 2: Review & Validate BOM")
    st.dataframe(st.session_state.df_bom, use_container_width=True)
    v = st.file_uploader("Upload Validated BOM", type=["xlsx"], key="bom")
    if v is not None:
        dv = pd.read_excel(v)
        req = ["Reference_IDs","Make","Model","Part_Number","Type","Description","Qty"]
        if all(c in dv.columns for c in req):
            st.session_state.df_bom = dv
            st.session_state.supervisor = SupervisorAgent(st.session_state.df_conn, dv)
            st.session_state.step = 3; st.rerun()
        else: st.error("Missing columns")

elif st.session_state.step == 3:
    sv = st.session_state.supervisor
    st.header("Step 3: Multi-Agent Pipeline")
    st.info(f"Status: {sv.state['status']} | Cycle: #{sv.state['cycle_count']}")
    if sv.state['status'] in ['idle','reviewing']:
        fb = st.text_area("Rework feedback (optional):")
        if st.button("Run / Rerun Pipeline", type="primary"):
            sv.state['rework_feedback'] = fb
            with st.spinner("Agents working..."): sv.run_generation_cycle()
            st.rerun()
    elif sv.state['status'] == 'awaiting_review':
        st.success("Draft generated!")
        for sn,d in sv.state['descriptions'].items():
            with st.expander(f"{sn}", expanded=True):
                if isinstance(d,dict): st.write(d.get("full_description",""))
                ip = sv.state['diagrams'].get(sn)
                if ip and os.path.exists(ip): st.image(ip)
        col1,col2 = st.columns(2)
        with col1:
            if os.path.exists(sv.state['docs'].get('docx','')):
                with open(sv.state['docs']['docx'],'rb') as f: st.download_button("Download DOCX",f,"Manual.docx",use_container_width=True)
        with col2:
            if os.path.exists(sv.state['docs'].get('pdf','')):
                with open(sv.state['docs']['pdf'],'rb') as f: st.download_button("Download PDF",f,"Manual.pdf",use_container_width=True)
        col1,col2 = st.columns(2)
        with col1:
            if st.button("Approve", type="primary", use_container_width=True): sv.approve(); st.balloons(); st.rerun()
        with col2:
            if st.button("Request Rework", use_container_width=True): sv.state['status']='reviewing'; st.rerun()
    elif sv.state['status'] == 'approved':
        st.balloons(); st.success("Complete!")
        with open(sv.state['docs']['docx'],'rb') as f: st.download_button("Download DOCX",f,"Manual.docx",use_container_width=True)
        with open(sv.state['docs']['pdf'],'rb') as f: st.download_button("Download PDF",f,"Manual.pdf",use_container_width=True)
