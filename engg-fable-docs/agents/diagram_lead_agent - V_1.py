import os, re, graphviz
import pandas as pd
from typing import Dict, Optional
from src.llm_client import ask_gemma
SUBSYSTEM_COLORS = {"Power":"#FFF2CC","Battery":"#D9EAD3","Motor":"#F4CCCC","Control":"#CFE2F3","Communication":"#D9D2E9","Sensor":"#FCE5CD","Default":"#F3F3F3"}
class DiagramLeadAgent:
    SYSTEM_PROMPT = "You are a Lead ECAD Engineer (IEC 60617, IEC 81346). Generate Graphviz DOT code for wiring diagrams.\nRules: rankdir=LR, HTML pin tables for ICs, label edges with signal names, color-code (red=power, blue=data, green=control). Output ONLY DOT between ```dot and ```."
    @staticmethod
    def run(subsystem: str, connections: pd.DataFrame, specs_cache: Dict[str,dict], feedback: str = "", output_dir: str = "output/diagrams") -> Optional[str]:
        rows = []
        for _, r in connections.iterrows():
            s = specs_cache.get(f"{r['Make']}_{r['Model']}", {})
            rows.append(f"  {s.get('reference_designator_prefix','U')}:{r['Component_ID']} pin {r['Source_Pin']} -> {r['Target_ID']} pin {r['Target_Pin']} signal={r['Signal_Name']}")
        color = "white"
        for k, c in SUBSYSTEM_COLORS.items():
            if k.lower() in subsystem.lower(): color = c; break
        prompt = f"Create wiring diagram for '{subsystem}'. Use fillcolor='{color}'. Connections:\n" + "\n".join(rows) + f"\nFeedback: {feedback}"
        raw = ask_gemma(prompt, DiagramLeadAgent.SYSTEM_PROMPT)
        dm = re.search(r'```(?:dot)?\s*(digraph\s+\w+\s*\{.*?\})\s*```', raw, re.DOTALL)
        if not dm: dm = re.search(r'(digraph\s+\w+\s*\{.*?\})', raw, re.DOTALL)
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
