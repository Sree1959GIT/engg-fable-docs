"""supervisor.py — Orchestrates the multi-agent documentation pipeline.

Pipeline:
  Stage 0  Component registry + cross-subsystem analysis   (pure pandas)
  Stage 1  Component research                              (LLM, bounded)
  Stage 2a Diagrams  → Diagram Review agent (SVG-parse verification with
           bounded rework: every sheet is independently checked against
           the connection data and regenerated on mismatch)
  Stage 2b Descriptions (sequential for continuity) → SME Review agent
           (hallucination/style checks + LLM grading, rework on failure)
  Stage 3  System-level description
  Stage 4  Document assembly (DOCX + PDF)

Orchestration is HARDWARE-AWARE: worker-pool sizes and LLM timeouts come
from src/hardware.detect_profile() — CPU-bound agents scale with cores,
LLM-bound agents stay pinned to the llama.cpp --parallel slot count.

Incremental rework: rework_module(sub, feedback) regenerates ONE
sub-module's description + diagram (re-verified) and reassembles the
documents, instead of rerunning the whole pipeline.
"""
import concurrent.futures

import pandas as pd

from agents import (ComponentResearchAgent, DescriptionAgent, DiagramLeadAgent,
                    DiagramReviewAgent, DocumentationAgent, SMEReviewAgent)
from src.component_registry import build_component_registry
from src.hardware import describe_profile, detect_profile
from src.llm_client import llm_available
from src.system_analysis import find_bridges, interconnections_for

MAX_DIAGRAM_REWORK = 2   # verification-driven regeneration attempts per sheet
MAX_DESC_REWORK = 1      # SME-driven regeneration attempts per description


class SupervisorAgent:
    def __init__(self, df_connectivity: pd.DataFrame, df_bom: pd.DataFrame,
                 doc_title: str = "", reference_context: str = "",
                 expectations: str = "", bus_mode: bool = False):
        self.profile = detect_profile()
        self.state = {
            "df_connectivity": df_connectivity,
            "df_bom": df_bom,
            "doc_title": doc_title,
            "reference_context": reference_context,
            "expectations": expectations,
            "bus_mode": bus_mode,
            "registry": {},
            "research_cache": {},
            "descriptions": {},
            "diagrams": {},
            "diagram_reviews": {},
            "sme_reviews": {},
            "interconnections": {},
            "bridges": [],
            "system_description": "",
            "docs": {"docx": "", "pdf": ""},
            "rework_feedback": "",
            "status": "idle",
            "cycle_count": 0,
        }
        self.progress_callback = None  # optional fn(stage: str, detail: str)

    # ── introspection for the UI's agent-acceptance step ─────────────────
    def planned_agents(self) -> list:
        p = self.profile
        n_sub = self.state["df_connectivity"]["Subsystem_Name"].nunique()
        llm = "🟢 LLM-assisted" if llm_available() else "🟠 programmatic (LLM offline)"
        return [
            {"agent": "Component Research Agent", "role": "Looks up part characteristics for the BOM entries",
             "workers": p["llm_workers"], "mode": llm},
            {"agent": "Diagram Lead Agent", "role": f"Draws {n_sub} IEC 60617 wiring sheets + system block diagram",
             "workers": p["diagram_workers"], "mode": "deterministic renderer"},
            {"agent": "Diagram Review Agent", "role": "Verifies every sheet against the connection data (SVG parse); requests rework on mismatch",
             "workers": p["diagram_workers"], "mode": "deterministic verifier"},
            {"agent": "Description Agent", "role": "Writes stage-level functional descriptions with cross-module continuity",
             "workers": 1, "mode": llm},
            {"agent": "SME Review Agent", "role": "Reviews each description for hallucinated designators, substance and style; requests rework",
             "workers": 1, "mode": llm},
            {"agent": "Documentation Agent", "role": "Assembles the DOCX + PDF manual",
             "workers": 1, "mode": "deterministic"},
        ]

    def _progress(self, stage: str, detail: str = ""):
        print(f"\n{stage}" + (f" — {detail}" if detail else ""))
        if self.progress_callback:
            try:
                self.progress_callback(stage, detail)
            except Exception:
                pass

    # ── verified single-sheet generation (used by full runs and rework) ──
    def _diagram_with_review(self, sub: str, g: pd.DataFrame, feedback: str = "",
                             report_progress: bool = True):
        """report_progress must be False when this runs inside a worker
        thread (Stage 2a's ThreadPoolExecutor): Streamlit UI calls are not
        thread-safe and calling the progress_callback from a background
        thread corrupts the app (the 'missing ScriptRunContext' warning is
        the tell — it previously caused the review UI to go blank).
        Console logging still happens either way."""
        registry = self.state["registry"]
        path, report = None, {"status": "skipped"}
        for attempt in range(1, MAX_DIAGRAM_REWORK + 1):
            path = DiagramLeadAgent.run(sub, g, registry, feedback,
                                        bus_mode=self.state.get("bus_mode", False))
            report = DiagramReviewAgent.verify(sub, g, path)
            if report["status"] != "fail":
                break
            feedback = f"verification found: {report['detail']}"
            msg = f"{sub}: rework {attempt} — {report['detail']}"
            if report_progress:
                self._progress("Diagram Review", msg)
            else:
                print(f"\nDiagram Review — {msg}")
        report["attempts"] = attempt
        return path, report

    def _description_with_review(self, sub: str, g: pd.DataFrame,
                                 prior_context: str, feedback: str = ""):
        d, review = None, {"status": "pass"}
        fb = feedback
        for attempt in range(1, MAX_DESC_REWORK + 2):
            d = DescriptionAgent.run(
                sub, g, registry=self.state["registry"],
                research_cache=self.state["research_cache"],
                prior_context=prior_context,
                interconnections=self.state["interconnections"].get(sub, []),
                sys_name=self._sys_name(), feedback=fb,
                reference_context=self.state["reference_context"])
            review = SMEReviewAgent.review(
                sub, d, g, self.state["registry"],
                all_subsystems=list(self.state["interconnections"].keys()),
                full_connections=self.state["df_connectivity"])
            if review["status"] == "pass" or attempt > MAX_DESC_REWORK:
                break
            fb = (feedback + "; " if feedback else "") + review["feedback"]
            self._progress("SME Review", f"{sub}: rework — {review['detail']}")
        review["attempts"] = attempt
        return d, review

    def _sys_name(self) -> str:
        df = self.state["df_connectivity"]
        return (str(df["System_Name"].iloc[0])
                if "System_Name" in df.columns and not df.empty else "System")

    # ── full pipeline ─────────────────────────────────────────────────────
    def run_generation_cycle(self) -> dict:
        self.state["status"] = "generating"
        self.state["cycle_count"] += 1
        feedback = self.state["rework_feedback"]
        if self.state["expectations"]:
            feedback = (feedback + "; " if feedback else "") + \
                f"User expectations for the outputs: {self.state['expectations'][:800]}"
        df_conn = self.state["df_connectivity"]
        df_bom = self.state["df_bom"]
        print(f"\n{'=' * 60}\nSUPERVISOR: Cycle {self.state['cycle_count']}\n{'=' * 60}")
        self._progress("Hardware profile", describe_profile(self.profile))
        if not llm_available():
            print("  (LLM offline — running with programmatic generation only)")

        # ── Stage 0 ──
        self._progress("Stage 0: Building component registry")
        registry = build_component_registry(df_conn, df_bom)
        self.state["registry"] = registry
        self.state["bridges"] = find_bridges(df_conn)
        subs = [str(s) for s in df_conn["Subsystem_Name"].unique()]
        self.state["interconnections"] = {s: interconnections_for(s, df_conn) for s in subs}

        # ── Stage 1: research (LLM-bound pool) ──
        self._progress("Stage 1: Researching components",
                       f"{self.profile['llm_workers']} workers")
        unique_parts = df_bom[["Make", "Model"]].drop_duplicates() if not df_bom.empty \
            else df_conn[["Make", "Model"]].drop_duplicates()
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.profile["llm_workers"]) as ex:
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

        # ── Stage 2a: diagrams + verification (CPU-bound pool) ──
        self._progress("Stage 2a: Generating diagrams",
                       f"{self.profile['diagram_workers']} workers, SVG verification on")
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.profile["diagram_workers"]) as ex:
            diag_f = {
                ex.submit(self._diagram_with_review, s,
                          df_conn[df_conn["Subsystem_Name"] == s], feedback,
                          False): s          # report_progress=False: worker thread
                for s in subs
            }
            for f in concurrent.futures.as_completed(diag_f):
                s = diag_f[f]
                try:
                    path, report = f.result()
                    self.state["diagrams"][s] = path
                    self.state["diagram_reviews"][s] = report
                    mark = "✓" if report["status"] == "pass" else "⚠"
                    self._progress("Diagram Review",
                                   f"{s}: {mark} {report.get('detail', report['status'])}")
                except Exception as e:
                    print(f"  ⚠ Diagram failed for {s}: {e}")
        try:
            sys_diag = DiagramLeadAgent.build_system_diagram(df_conn, registry)
            if sys_diag:
                self.state["diagrams"]["System_Overview"] = sys_diag
            for i, p in enumerate(
                    DiagramLeadAgent.build_system_diagram_sheets(df_conn, registry),
                    start=1):
                self.state["diagrams"][f"System_Overview_Sheet{i}"] = p
        except Exception as e:
            print(f"  ⚠ System diagram failed: {e}")

        # ── Stage 2b: descriptions + SME review (sequential) ──
        self._progress("Stage 2b: Writing subsystem descriptions",
                       "SME review on")
        prior_context = ""
        for s in subs:
            self._progress("Stage 2b", s)
            d, review = self._description_with_review(
                s, df_conn[df_conn["Subsystem_Name"] == s], prior_context, feedback)
            self.state["descriptions"][s] = d
            self.state["sme_reviews"][s] = review
            mark = "✓" if review["status"] == "pass" else "⚠"
            self._progress("SME Review", f"{s}: {mark} {review.get('detail', '')}")
            prior_context += f"- {s.replace('_', ' ')}: {d.get('overview', '')}\n"

        # ── Stage 3 ──
        self._progress("Stage 3: Writing system description")
        self.state["system_description"] = DescriptionAgent.describe_system(
            self._sys_name(), self.state["descriptions"], self.state["bridges"],
            feedback, reference_context=self.state["reference_context"])

        # ── Stage 4 ──
        self._progress("Stage 4: Assembling documents")
        self.state["docs"] = DocumentationAgent.run(self.state)
        self.state["status"] = "awaiting_review"
        print("\n✓ Pipeline complete. Awaiting review.")
        return self.state

    # ── incremental per-module rework (final-draft feedback buttons) ─────
    def rework_module(self, sub: str, feedback: str) -> dict:
        """Regenerate ONE sub-module's description + diagram from reviewer
        feedback, re-verify both, and reassemble the documents."""
        df_conn = self.state["df_connectivity"]
        g = df_conn[df_conn["Subsystem_Name"] == sub]
        if g.empty:
            return self.state
        self._progress("Module rework", f"{sub}: applying feedback")
        path, dreport = self._diagram_with_review(sub, g, feedback)
        self.state["diagrams"][sub] = path
        self.state["diagram_reviews"][sub] = dreport

        prior_context = "".join(
            f"- {s.replace('_', ' ')}: {d.get('overview', '')}\n"
            for s, d in self.state["descriptions"].items() if s != sub)
        d, review = self._description_with_review(sub, g, prior_context, feedback)
        self.state["descriptions"][sub] = d
        self.state["sme_reviews"][sub] = review

        self._progress("Module rework", f"{sub}: reassembling documents")
        self.state["docs"] = DocumentationAgent.run(self.state)
        self.state["status"] = "awaiting_review"
        return self.state

    def rework_system(self, feedback: str) -> dict:
        """Regenerate the system-level description (and, if the feedback
        mentions the diagram, the system block diagram) from reviewer
        feedback, and reassemble the documents."""
        self._progress("System rework", "applying feedback")
        fb_low = feedback.lower()
        if any(w in fb_low for w in ("diagram", "wiring", "block", "bus", "picture")):
            try:
                registry = self.state["registry"]
                df_conn = self.state["df_connectivity"]
                sys_diag = DiagramLeadAgent.build_system_diagram(df_conn, registry)
                if sys_diag:
                    self.state["diagrams"]["System_Overview"] = sys_diag
                for i, p in enumerate(
                        DiagramLeadAgent.build_system_diagram_sheets(df_conn, registry),
                        start=1):
                    self.state["diagrams"][f"System_Overview_Sheet{i}"] = p
            except Exception as e:
                print(f"  ⚠ System diagram rework failed: {e}")

        self.state["system_description"] = DescriptionAgent.describe_system(
            self._sys_name(), self.state["descriptions"], self.state["bridges"],
            feedback, reference_context=self.state["reference_context"])

        self._progress("System rework", "reassembling documents")
        self.state["docs"] = DocumentationAgent.run(self.state)
        self.state["status"] = "awaiting_review"
        return self.state

    def submit_feedback(self, fb: str):
        self.state["rework_feedback"] = fb
        self.state["status"] = "reviewing"

    def approve(self):
        self.state["status"] = "approved"
