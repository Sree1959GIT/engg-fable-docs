#!/usr/bin/env python3
"""bootstrap.py — Creates the complete Wiring Diagram Generator project on Windows."""

import os

FILES = {}

FILES["requirements.txt"] = """pandas>=2.2.0
openpyxl>=3.1.2
ollama>=0.4.0
graphviz>=0.20.3
python-docx>=1.1.2
reportlab>=4.2
streamlit>=1.38.0
"""

FILES["src/__init__.py"] = ""

FILES["src/llm_client.py"] = """import ollama
MODEL = "gemma4:12b"
def ask_gemma(prompt: str, system: str = "") -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        resp = ollama.chat(model=MODEL, messages=messages)
        return resp["message"]["content"].strip()
    except Exception as e:
        print(f"  LLM call failed: {e}")
        return ""
"""

FILES["src/excel_parser.py"] = """import pandas as pd
REQUIRED_COLUMNS = [
    'System_Name','Subsystem_Name','Component_ID',
    'Make','Model','Source_Pin','Target_ID','Target_Pin','Signal_Name'
]
def parse_connectivity_data(file_path_or_buffer) -> pd.DataFrame:
    all_sheets = pd.read_excel(file_path_or_buffer, sheet_name=None, engine='openpyxl')
    frames = []
    for sheet_name, df in all_sheets.items():
        if 'Subsystem_Name' not in df.columns:
            df['Subsystem_Name'] = sheet_name
        frames.append(df)
    combined = pd.concat(frames, ignore_index=True)
    missing = [c for c in REQUIRED_COLUMNS if c not in combined.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    combined = combined.dropna(subset=['Component_ID','Make','Model'])
    return combined
"""

FILES["src/bom_generator.py"] = """import pandas as pd
from src.llm_client import ask_gemma
BOM_COLUMNS = ['Reference_IDs','Make','Model','Part_Number','Type','Description','Qty']
def _infer_part_info(make: str, model: str) -> tuple:
    prompt = f"Given component Make='{make}' Model='{model}', respond with exactly: PartNumber|Type|ShortDescription\\nExample: BQ76952|IC|Battery Management IC"
    resp = ask_gemma(prompt)
    try:
        parts = resp.split('|')
        return parts[0].strip(), parts[1].strip(), '|'.join(parts[2:]).strip() if len(parts) > 2 else ""
    except:
        return "Unknown","Unknown","Pending review"
def generate_draft_bom(df: pd.DataFrame) -> pd.DataFrame:
    refs = df.groupby(['Make','Model'])['Component_ID'].apply(lambda x: ', '.join(sorted(x.unique()))).reset_index(name='Reference_IDs')
    qty = df.groupby(['Make','Model'])['Component_ID'].nunique().reset_index(name='Qty')
    bom = refs.merge(qty, on=['Make','Model'])
    pns, typs, descs = [], [], []
    for _, r in bom.iterrows():
        pn, t, d = _infer_part_info(r['Make'], r['Model'])
        pns.append(pn); typs.append(t); descs.append(d)
    bom['Part_Number'] = pns; bom['Type'] = typs; bom['Description'] = descs
    return bom[BOM_COLUMNS]
"""

FILES["agents/__init__.py"] = "from .component_research_agent import ComponentResearchAgent\nfrom .diagram_lead_agent import DiagramLeadAgent\nfrom .description_agent import DescriptionAgent\nfrom .documentation_agent import DocumentationAgent\n"

FILES["agents/component_research_agent.py"] = """import json, re
from src.llm_client import ask_gemma
class ComponentResearchAgent:
    SYSTEM_PROMPT = "You are an electronics component researcher. For the given component, output ONLY valid JSON with keys: packaging, dimensions, electrical_characteristics, functional_description, reference_designator_prefix. Output raw JSON only."
    @staticmethod
    def run(make: str, model: str, feedback: str = "") -> dict:
        prompt = f"Research the component: Make='{make}', Model='{model}'. Feedback: {feedback}"
        raw = ask_gemma(prompt, ComponentResearchAgent.SYSTEM_PROMPT)
        m = re.search(r'```(?:json)?\\s*(\\{.*?\\})\\s*```', raw, re.DOTALL)
        js = m.group(1) if m else raw
        try: return json.loads(js)
        except: return {"packaging":"Unknown","dimensions":"Unknown","electrical_characteristics":raw[:200],"functional_description":raw[:200],"reference_designator_prefix":"U"}
"""

FILES["agents/diagram_lead_agent.py"] = """import os, re, graphviz
import pandas as pd
from typing import Dict, Optional
from src.llm_client import ask_gemma
SUBSYSTEM_COLORS = {"Power":"#FFF2CC","Battery":"#D9EAD3","Motor":"#F4CCCC","Control":"#CFE2F3","Communication":"#D9D2E9","Sensor":"#FCE5CD","Default":"#F3F3F3"}
class DiagramLeadAgent:
    SYSTEM_PROMPT = "You are a Lead ECAD Engineer (IEC 60617, IEC 81346). Generate Graphviz DOT code for wiring diagrams.\\nRules: rankdir=LR, HTML pin tables for ICs, label edges with signal names, color-code (red=power, blue=data, green=control). Output ONLY DOT between ```dot and ```."
    @staticmethod
    def run(subsystem: str, connections: pd.DataFrame, specs_cache: Dict[str,dict], feedback: str = "", output_dir: str = "output/diagrams") -> Optional[str]:
        rows = []
        for _, r in connections.iterrows():
            s = specs_cache.get(f"{r['Make']}_{r['Model']}", {})
            rows.append(f"  {s.get('reference_designator_prefix','U')}:{r['Component_ID']} pin {r['Source_Pin']} -> {r['Target_ID']} pin {r['Target_Pin']} signal={r['Signal_Name']}")
        color = "white"
        for k, c in SUBSYSTEM_COLORS.items():
            if k.lower() in subsystem.lower(): color = c; break
        prompt = f"Create wiring diagram for '{subsystem}'. Use fillcolor='{color}'. Connections:\\n" + "\\n".join(rows) + f"\\nFeedback: {feedback}"
        raw = ask_gemma(prompt, DiagramLeadAgent.SYSTEM_PROMPT)
        dm = re.search(r'```(?:dot)?\\s*(digraph\\s+\\w+\\s*\\{.*?\\})\\s*```', raw, re.DOTALL)
        if not dm: dm = re.search(r'(digraph\\s+\\w+\\s*\\{.*?\\})', raw, re.DOTALL)
        if not dm: return None
        os.makedirs(output_dir, exist_ok=True)
        out = os.path.join(output_dir, f"{subsystem}_diagram")
        try:
            graphviz.Source(dm.group(1), format='png').render(out, cleanup=True)
            return f"{out}.png"
        except Exception as e:
            print(f"  Render failed: {e}")
            with open(f"{out}.dot","w") as f: f.write(dm.group(1))
            return None
"""

FILES["agents/description_agent.py"] = """import pandas as pd
from typing import Dict
from src.llm_client import ask_gemma
class DescriptionAgent:
    SYSTEM_PROMPT = "You are a senior technical writer. Write clear, structured functional descriptions."
    @staticmethod
    def run(subsystem: str, connections: pd.DataFrame, specs_cache: Dict[str,dict], feedback: str = "") -> Dict[str,str]:
        details = []
        for _, r in connections.iterrows():
            s = specs_cache.get(f"{r['Make']}_{r['Model']}", {})
            info = f"Component {r['Component_ID']}: {r['Make']} {r['Model']} [{s.get('functional_description','N/A')}]"
            if info not in details: details.append(info)
        lines = connections.apply(lambda r: f"  Pin {r['Source_Pin']} of {r['Component_ID']} -> Pin {r['Target_Pin']} of {r['Target_ID']} ({r['Signal_Name']})", axis=1)
        prompt = f"Document '{subsystem}'.\\nComponents:\\n" + "\\n".join(details) + f"\\nConnections:\\n" + "\\n".join(lines) + f"\\n\\nProvide: SUBSYSTEM OVERVIEW, MODULE DESCRIPTION, COMPONENT ROLES.\\nFeedback: {feedback}"
        raw = ask_gemma(prompt, DescriptionAgent.SYSTEM_PROMPT)
        return {"subsystem_name": subsystem, "full_description": raw, "overview": raw[:500] if raw else ""}
"""

FILES["agents/documentation_agent.py"] = """import os, pandas as pd
from typing import Dict
from docx import Document
from docx.shared import Inches
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, PageBreak, Table, TableStyle
class DocumentationAgent:
    @staticmethod
    def run(state: dict, output_dir: str = "output") -> Dict[str,str]:
        os.makedirs(output_dir, exist_ok=True)
        docx_path, pdf_path = os.path.join(output_dir,"Technical_User_Manual.docx"), os.path.join(output_dir,"Technical_User_Manual.pdf")
        df_conn, df_bom = state.get("df_connectivity",pd.DataFrame()), state.get("df_bom",pd.DataFrame())
        descs, diags = state.get("descriptions",{}), state.get("diagrams",{})
        sys_name = df_conn["System_Name"].iloc[0] if "System_Name" in df_conn.columns else "System"
        # Word
        doc = Document()
        doc.add_heading(f"{sys_name} - Technical User Manual", 0)
        doc.add_heading("1. Bill of Materials", 1)
        if not df_bom.empty:
            t = doc.add_table(rows=1, cols=len(df_bom.columns)); t.style = "Light Grid Accent 1"
            for i,c in enumerate(df_bom.columns): t.rows[0].cells[i].text = str(c)
            for _,r in df_bom.iterrows():
                cells = t.add_row().cells
                for i,c in enumerate(df_bom.columns): cells[i].text = str(r[c])
        doc.add_heading("2. System Overview", 1)
        for _,d in descs.items():
            if isinstance(d,dict): doc.add_paragraph(d.get("full_description","")); break
        doc.add_heading("3. Subsystems & Wiring Diagrams", 1)
        for sn,d in descs.items():
            if isinstance(d,dict):
                doc.add_heading(sn,2); doc.add_paragraph(d.get("full_description",""))
                ip = diags.get(sn)
                if ip and os.path.exists(ip): doc.add_picture(ip, width=Inches(5.5))
        doc.add_heading("4. Connectivity Data", 1)
        for sn,g in df_conn.groupby("Subsystem_Name"):
            doc.add_heading(sn,2); t = doc.add_table(rows=1, cols=len(g.columns)); t.style = "Light Grid Accent 1"
            for i,c in enumerate(g.columns): t.rows[0].cells[i].text = str(c)
            for _,r in g.iterrows():
                cells = t.add_row().cells
                for i,c in enumerate(g.columns): cells[i].text = str(r[c])
        doc.save(docx_path)
        # PDF
        pdf = SimpleDocTemplate(pdf_path, pagesize=letter)
        styles = getSampleStyleSheet(); story = []
        story.append(Paragraph(f"{sys_name} - Technical User Manual", styles["Title"])); story.append(Spacer(1,0.5*inch))
        if not df_bom.empty:
            story.append(Paragraph("1. Bill of Materials", styles["Heading1"]))
            pd2 = [list(df_bom.columns)] + [[str(r[c]) for c in df_bom.columns] for _,r in df_bom.iterrows()]
            t = Table(pd2, repeatRows=1)
            t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor("#1f4e79")),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),0.5,colors.grey),('FONTSIZE',(0,0),(-1,-1),8)]))
            story.append(t); story.append(PageBreak())
        story.append(Paragraph("2. Subsystem Descriptions & Diagrams", styles["Heading1"]))
        for sn,d in descs.items():
            if isinstance(d,dict):
                story.append(Paragraph(sn, styles["Heading2"])); story.append(Paragraph(d.get("full_description",""), styles["Normal"]))
                ip = diags.get(sn)
                if ip and os.path.exists(ip): story.append(RLImage(ip, width=400, height=280, preserveAspectRatio=True))
                story.append(PageBreak())
        pdf.build(story)
        print(f"  Saved: {docx_path}, {pdf_path}")
        return {"docx":docx_path, "pdf":pdf_path}
"""

FILES["supervisor.py"] = """import concurrent.futures
import pandas as pd
from agents import ComponentResearchAgent, DiagramLeadAgent, DescriptionAgent, DocumentationAgent

class SupervisorAgent:
    def __init__(self, df_connectivity: pd.DataFrame, df_bom: pd.DataFrame):
        self.state = {"df_connectivity":df_connectivity,"df_bom":df_bom,"research_cache":{},"descriptions":{},"diagrams":{},"docs":{"docx":"","pdf":""},"rework_feedback":"","status":"idle","cycle_count":0}
    def run_generation_cycle(self) -> dict:
        self.state["status"]="generating"; self.state["cycle_count"]+=1
        df_conn, df_bom, feedback = self.state["df_connectivity"], self.state["df_bom"], self.state["rework_feedback"]
        print(f"\\n{'='*60}\\nSUPERVISOR: Cycle {self.state['cycle_count']}\\n{'='*60}")
        # Stage 1: Parallel research
        print("\\nStage 1: Researching components...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(ComponentResearchAgent.run, r['Make'], r['Model'], feedback): f"{r['Make']}_{r['Model']}" for _, r in df_bom.iterrows()}
            for f in concurrent.futures.as_completed(futs):
                try: self.state['research_cache'][futs[f]] = f.result()
                except Exception as e: print(f"  Research failed: {e}")
        # Stage 2: Parallel diagrams & descriptions
        print("\\nStage 2: Generating diagrams & descriptions...")
        subs = df_conn['Subsystem_Name'].unique()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            df = {ex.submit(DiagramLeadAgent.run, s, df_conn[df_conn['Subsystem_Name']==s], self.state['research_cache'], feedback): s for s in subs}
            dsf = {ex.submit(DescriptionAgent.run, s, df_conn[df_conn['Subsystem_Name']==s], self.state['research_cache'], feedback): s for s in subs}
            for f in concurrent.futures.as_completed(df): self.state['diagrams'][df[f]] = f.result()
            for f in concurrent.futures.as_completed(dsf): self.state['descriptions'][dsf[f]] = f.result()
        # Stage 3: Documentation
        print("\\nStage 3: Assembling documents...")
        self.state['docs'] = DocumentationAgent.run(self.state)
        self.state['status'] = 'awaiting_review'
        return self.state
    def submit_feedback(self, fb: str): self.state['rework_feedback']=fb; self.state['status']='reviewing'
    def approve(self): self.state['status']='approved'
"""

FILES["web_ui.py"] = """import streamlit as st
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
"""


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    for path, content in FILES.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content.lstrip("\\n"))
        print(f"  Created: {path}")
    for d in ["input", "output/diagrams"]:
        os.makedirs(os.path.join(root, d), exist_ok=True)
    print("\\nProject created! Next steps:")
    print("  pip install -r requirements.txt")
    print("  ollama pull gemma4:12b")
    print("  python generate_sample_data.py   (or use your own Excel)")
    print("  streamlit run web_ui.py")

if __name__ == "__main__":
    main()
