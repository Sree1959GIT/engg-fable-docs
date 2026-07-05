"""supervisor.py — Orchestrates the multi-level documentation pipeline.

Pipeline:
  Stage 0  Component registry + cross-subsystem analysis   (pure pandas, instant)
  Stage 1  Component research                              (LLM optional, bounded concurrency)
  Stage 2a Subsystem diagrams + system diagram             (parallel, no LLM)
  Stage 2b Subsystem descriptions                          (SEQUENTIAL — each one
           receives the previously written overviews for contextual continuity)
  Stage 3  System-level description                        (synthesizes stage 2b)
  Stage 4  Document assembly (DOCX + PDF)

Concurrency note: research runs with at most LLM_MAX_CONCURRENT workers because
llama.cpp serializes requests; flooding it with 10 threads was the root cause
of the empty-response failures.
"""
import concurrent.futures

import pandas as pd

from agents import (ComponentResearchAgent, DescriptionAgent, DiagramLeadAgent,
                    DocumentationAgent)
from src.component_registry import build_component_registry
from src.config import LLM_MAX_CONCURRENT
from src.llm_client import llm_available
from src.system_analysis import find_bridges, interconnections_for


class SupervisorAgent:
    def __init__(self, df_connectivity: pd.DataFrame, df_bom: pd.DataFrame):
        self.state = {
            "df_connectivity": df_connectivity,
            "df_bom": df_bom,
            "registry": {},
            "research_cache": {},
            "descriptions": {},
            "diagrams": {},
            "interconnections": {},
            "bridges": [],
            "system_description": "",
            "docs": {"docx": "", "pdf": ""},
            "rework_feedback": "",
            "status": "idle",
            "cycle_count": 0,
        }
        self.progress_callback = None  # optional fn(stage: str, detail: str)

    def _progress(self, stage: str, detail: str = ""):
        print(f"\n{stage}" + (f" — {detail}" if detail else ""))
        if self.progress_callback:
            try:
                self.progress_callback(stage, detail)
            except Exception:
                pass

    def run_generation_cycle(self) -> dict:
        self.state["status"] = "generating"
        self.state["cycle_count"] += 1
        feedback = self.state["rework_feedback"]
        df_conn = self.state["df_connectivity"]
        df_bom = self.state["df_bom"]
        print(f"\n{'=' * 60}\nSUPERVISOR: Cycle {self.state['cycle_count']}\n{'=' * 60}")
        if not llm_available():
            print("  (LLM offline — running with programmatic generation only)")

        # ── Stage 0: registry + cross-subsystem analysis ──
        self._progress("Stage 0: Building component registry")
        registry = build_component_registry(df_conn, df_bom)
        self.state["registry"] = registry
        self.state["bridges"] = find_bridges(df_conn)
        subs = [str(s) for s in df_conn["Subsystem_Name"].unique()]
        self.state["interconnections"] = {s: interconnections_for(s, df_conn) for s in subs}

        # ── Stage 1: component research (bounded LLM concurrency) ──
        self._progress("Stage 1: Researching components")
        unique_parts = df_bom[["Make", "Model"]].drop_duplicates() if not df_bom.empty \
            else df_conn[["Make", "Model"]].drop_duplicates()
        workers = max(1, LLM_MAX_CONCURRENT)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(ComponentResearchAgent.run, str(r["Make"]), str(r["Model"]), feedback):
                    f"{r['Make']}_{r['Model']}"
                for _, r in unique_parts.iterrows()
            }
            for f in concurrent.futures.as_completed(futures):
                k = futures[f]
                try:
                    self.state["research_cache"][k] = f.result()
                except Exception as e:
                    print(f"  ⚠ Research failed for {k}: {e}")

        # ── Stage 2a: diagrams (parallel — purely programmatic) ──
        self._progress("Stage 2a: Generating diagrams")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            diag_f = {
                ex.submit(DiagramLeadAgent.run, s,
                          df_conn[df_conn["Subsystem_Name"] == s], registry, feedback): s
                for s in subs
            }
            for f in concurrent.futures.as_completed(diag_f):
                try:
                    self.state["diagrams"][diag_f[f]] = f.result()
                except Exception as e:
                    print(f"  ⚠ Diagram failed for {diag_f[f]}: {e}")
        try:
            sys_diag = DiagramLeadAgent.build_system_diagram(df_conn, registry)
            if sys_diag:
                self.state["diagrams"]["System_Overview"] = sys_diag
        except Exception as e:
            print(f"  ⚠ System diagram failed: {e}")

        # ── Stage 2b: descriptions (sequential for contextual continuity) ──
        self._progress("Stage 2b: Writing subsystem descriptions")
        prior_context = ""
        for s in subs:
            self._progress("Stage 2b", s)
            d = DescriptionAgent.run(
                s, df_conn[df_conn["Subsystem_Name"] == s],
                registry=registry,
                research_cache=self.state["research_cache"],
                prior_context=prior_context,
                feedback=feedback,
            )
            self.state["descriptions"][s] = d
            prior_context += f"- {s.replace('_', ' ')}: {d.get('overview', '')}\n"

        # ── Stage 3: system-level description ──
        self._progress("Stage 3: Writing system description")
        sys_name = (str(df_conn["System_Name"].iloc[0])
                    if "System_Name" in df_conn.columns else "System")
        self.state["system_description"] = DescriptionAgent.describe_system(
            sys_name, self.state["descriptions"], self.state["bridges"], feedback)

        # ── Stage 4: documents ──
        self._progress("Stage 4: Assembling documents")
        self.state["docs"] = DocumentationAgent.run(self.state)
        self.state["status"] = "awaiting_review"
        print("\n✓ Pipeline complete. Awaiting review.")
        return self.state

    def submit_feedback(self, fb: str):
        self.state["rework_feedback"] = fb
        self.state["status"] = "reviewing"

    def approve(self):
        self.state["status"] = "approved"
