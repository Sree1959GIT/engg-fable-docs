"""Generates functional descriptions. Falls back to programmatic if LLM fails."""
import pandas as pd
from typing import Dict
from src.llm_client import ask_gemma


class DescriptionAgent:
    """Generates functional descriptions. Tries LLM first, falls back to programmatic."""

    SYSTEM_PROMPT = (
        "You are a senior technical writer documenting an electrical system. "
        "Write clear, structured, hierarchical functional descriptions. "
        "Use proper engineering terminology."
    )

    @staticmethod
    def run(subsystem: str, connections: pd.DataFrame,
            specs_cache: Dict[str, dict] = None,
            feedback: str = "") -> Dict[str, str]:
        llm_result = DescriptionAgent._try_llm(subsystem, connections, specs_cache, feedback)
        if llm_result and len(llm_result.get("full_description", "")) > 50:
            return llm_result
        print(f"  [Description] Using fallback for '{subsystem}'")
        return DescriptionAgent._fallback_description(subsystem, connections)

    @staticmethod
    def _try_llm(subsystem, connections, specs_cache, feedback):
        details = []
        for _, r in connections.iterrows():
            spec = specs_cache.get(f"{r['Make']}_{r['Model']}", {}) if specs_cache else {}
            info = f"Component {r['Component_ID']}: {r['Make']} {r['Model']}"
            if spec.get("functional_description"):
                info += f" [{spec['functional_description']}]"
            if info not in details:
                details.append(info)
        lines = connections.apply(
            lambda r: f"  Pin {r['Source_Pin']} of {r['Component_ID']} -> "
                      f"Pin {r['Target_Pin']} of {r['Target_ID']} ({r['Signal_Name']})",
            axis=1
        )
        prompt = (
            f"Document the '{subsystem}' subsystem.\n\nComponents:\n"
            + "\n".join(details) +
            f"\n\nConnections:\n" + "\n".join(lines) +
            f"\n\nProvide:\n1. SUBSYSTEM OVERVIEW\n2. MODULE DESCRIPTION\n"
            f"3. COMPONENT ROLES\n4. INTERCONNECTIONS\n"
            f"Feedback: {feedback}"
        )
        raw = ask_gemma(prompt, DescriptionAgent.SYSTEM_PROMPT)
        return {"subsystem_name": subsystem, "full_description": raw, "overview": raw[:500]}

    @staticmethod
    def _fallback_description(subsystem, connections):
        components = {}
        for _, r in connections.iterrows():
            for cid, make, model in [
                (r["Component_ID"], r["Make"], r["Model"]),
                (r["Target_ID"], r["Make"], r["Model"]),
            ]:
                if cid not in components:
                    components[cid] = (make, model)
        comp_lines = [f"  - {cid}: {make} {model}" for cid, (make, model) in components.items()]
        signals = connections["Signal_Name"].unique()
        signal_lines = [f"  - {s}" for s in signals]
        flow_lines = [
            f"  {r['Component_ID']}({r['Source_Pin']}) -> {r['Target_ID']}({r['Target_Pin']}): {r['Signal_Name']}"
            for _, r in connections.iterrows()
        ]
        description = (
            f"**{subsystem} - Functional Description**\n\n"
            f"**Overview:**\nThe {subsystem} subsystem consists of {len(components)} components "
            f"connected via {len(signals)} unique signals.\n\n"
            f"**Components:**\n" + "\n".join(comp_lines) + "\n\n"
            f"**Signals:**\n" + "\n".join(signal_lines) + "\n\n"
            f"**Signal Flow:**\n" + "\n".join(flow_lines)
        )
        return {"subsystem_name": subsystem, "full_description": description,
                "overview": f"Subsystem with {len(components)} components and {len(signals)} signals."}
