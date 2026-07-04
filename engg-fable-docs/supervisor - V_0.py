import concurrent.futures
import pandas as pd
from agents import ComponentResearchAgent, DiagramLeadAgent, DescriptionAgent, DocumentationAgent

class SupervisorAgent:
    def __init__(self, df_connectivity: pd.DataFrame, df_bom: pd.DataFrame):
        self.state = {"df_connectivity":df_connectivity,"df_bom":df_bom,"research_cache":{},"descriptions":{},"diagrams":{},"docs":{"docx":"","pdf":""},"rework_feedback":"","status":"idle","cycle_count":0}
    def run_generation_cycle(self) -> dict:
        self.state["status"]="generating"; self.state["cycle_count"]+=1
        df_conn, df_bom, feedback = self.state["df_connectivity"], self.state["df_bom"], self.state["rework_feedback"]
        print(f"\n{'='*60}\nSUPERVISOR: Cycle {self.state['cycle_count']}\n{'='*60}")
        # Stage 1: Parallel research
        print("\nStage 1: Researching components...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(ComponentResearchAgent.run, r['Make'], r['Model'], feedback): f"{r['Make']}_{r['Model']}" for _, r in df_bom.iterrows()}
            for f in concurrent.futures.as_completed(futs):
                try: self.state['research_cache'][futs[f]] = f.result()
                except Exception as e: print(f"  Research failed: {e}")
        # Stage 2: Parallel diagrams & descriptions
        print("\nStage 2: Generating diagrams & descriptions...")
        subs = df_conn['Subsystem_Name'].unique()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            df = {ex.submit(DiagramLeadAgent.run, s, df_conn[df_conn['Subsystem_Name']==s], self.state['research_cache'], feedback): s for s in subs}
            dsf = {ex.submit(DescriptionAgent.run, s, df_conn[df_conn['Subsystem_Name']==s], self.state['research_cache'], feedback): s for s in subs}
            for f in concurrent.futures.as_completed(df): self.state['diagrams'][df[f]] = f.result()
            for f in concurrent.futures.as_completed(dsf): self.state['descriptions'][dsf[f]] = f.result()
        # Stage 3: Documentation
        print("\nStage 3: Assembling documents...")
        self.state['docs'] = DocumentationAgent.run(self.state)
        self.state['status'] = 'awaiting_review'
        return self.state
    def submit_feedback(self, fb: str): self.state['rework_feedback']=fb; self.state['status']='reviewing'
    def approve(self): self.state['status']='approved'
