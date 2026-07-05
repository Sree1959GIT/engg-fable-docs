"""agents/component_research_agent.py — Component spec research with offline fallback.

Tries the local LLM (small token budget, JSON output); if it fails or returns
garbage, falls back to the offline knowledge base in component_registry so the
manual never contains 'Unknown' for parts we recognize.
"""
import json
import re

from src.component_registry import infer_refdes_prefix, lookup_known
from src.config import MAX_TOKENS_RESEARCH
from src.llm_client import ask_llm


class ComponentResearchAgent:
    SYSTEM_PROMPT = (
        "You are an electronics component researcher. For the given component, "
        "output ONLY a valid JSON object with exactly these keys: packaging, "
        "dimensions, electrical_characteristics, functional_description, "
        "reference_designator_prefix. No markdown fences, no commentary."
    )

    @staticmethod
    def run(make: str, model: str, feedback: str = "") -> dict:
        raw = ask_llm(
            f"Research the component: Make='{make}', Model='{model}'."
            + (f" Feedback: {feedback}" if feedback else ""),
            ComponentResearchAgent.SYSTEM_PROMPT,
            max_tokens=MAX_TOKENS_RESEARCH,
        )
        result = ComponentResearchAgent._parse_json(raw)
        if result:
            return result
        return ComponentResearchAgent._offline_fallback(make, model)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        if not raw:
            return {}
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        candidate = m.group(1) if m else raw
        # Also try the first {...} block anywhere in the response
        if not m:
            b = re.search(r"\{.*\}", raw, re.DOTALL)
            if b:
                candidate = b.group(0)
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and data.get("functional_description"):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return {}

    @staticmethod
    def _offline_fallback(make: str, model: str) -> dict:
        known = lookup_known(model)
        if known:
            return {
                "packaging": known.get("package", "See datasheet"),
                "dimensions": "See datasheet",
                "electrical_characteristics": known.get("interfaces", ""),
                "functional_description": known.get("description", ""),
                "reference_designator_prefix": known.get("prefix", "U"),
            }
        prefix = infer_refdes_prefix("", model)
        return {
            "packaging": "See datasheet",
            "dimensions": "See datasheet",
            "electrical_characteristics": "Refer to manufacturer datasheet",
            "functional_description": f"{make} {model} — refer to manufacturer documentation.",
            "reference_designator_prefix": prefix,
        }
