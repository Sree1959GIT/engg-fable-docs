import concurrent.futures
import pandas as pd
from agents import ComponentResearchAgent, DiagramLeadAgent, DescriptionAgent, DocumentationAgent


class SupervisorAgent:
    def __init__(self, df_connectivity: pd.DataFrame, df_bom: pd.DataFrame):
        self.state = {
            "df_connectivity": df_connectivity,
            "df_bom": df_bom,
            "research_cache": {},
            "descriptions": {},
            "diagrams": {},
            "docs": {"docx": "", "pdf": ""},
            "rework_feedback": "",
            "status": "idle",
            "cycle_count": 0,
        }

    def run_generation_cycle(self) -> dict:
        self.state["status"] = "generating"
        self.state["cycle_count"] += 1
        feedback = self.state["rework_feedback"]
        df_conn = self.state["df_connectivity"]
        df_bom = self.state["df_bom"]
        print(f"\n{'='*60}\nSUPERVISOR: Cycle {self.state['cycle_count']}\n{'='*60}")

        # Stage 1: Parallel Component Research
        print("\nStage 1: Researching components...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = {
                ex.submit(ComponentResearchAgent.run, r['Make'], r['Model'], feedback): f"{r['Make']}_{r['Model']}"
                for _, r in df_bom.iterrows()
            }
            for f in concurrent.futures.as_completed(futures):
                k = futures[f]
                try:
                    self.state['research_cache'][k] = f.result()
                except Exception as e:
                    print(f"  Research failed for {k}: {e}")

        # Stage 2: Parallel Diagrams & Descriptions
        print("\nStage 2: Generating diagrams & descriptions...")
        subs = df_conn['Subsystem_Name'].unique()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            diag_f = {
                ex.submit(DiagramLeadAgent.run, s, df_conn[df_conn['Subsystem_Name'] == s], self.state['research_cache'], feedback): s
                for s in subs
            }
            desc_f = {
                ex.submit(DescriptionAgent.run, s, df_conn[df_conn['Subsystem_Name'] == s], self.state['research_cache'], feedback): s
                for s in subs
            }
            for f in concurrent.futures.as_completed(diag_f):
                self.state['diagrams'][diag_f[f]] = f.result()
            for f in concurrent.futures.as_completed(desc_f):
                self.state['descriptions'][desc_f[f]] = f.result()

        # Generate system-level overview diagram
        print("\nStage 2b: Generating system-level diagram...")
        try:
            sys_diag = DiagramLeadAgent.build_system_diagram(df_conn)
            if sys_diag:
                self.state["diagrams"]["System_Overview"] = sys_diag
                print("  ✓ System overview diagram generated")
        except Exception as e:
            print(f"  ⚠ System diagram failed: {e}")

        # Stage 3: Documentation
        print("\nStage 3: Assembling documents...")
        self.state['docs'] = DocumentationAgent.run(self.state)
        self.state['status'] = 'awaiting_review'
        print("\n✓ Pipeline complete. Awaiting review.")
        return self.state

    def submit_feedback(self, fb: str):
        self.state['rework_feedback'] = fb
        self.state['status'] = 'reviewing'

    def approve(self):
        self.state['status'] = 'approved'
