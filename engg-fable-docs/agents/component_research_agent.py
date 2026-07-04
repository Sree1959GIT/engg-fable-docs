import json, re
from src.llm_client import ask_gemma
class ComponentResearchAgent:
    SYSTEM_PROMPT = "You are an electronics component researcher. For the given component, output ONLY valid JSON with keys: packaging, dimensions, electrical_characteristics, functional_description, reference_designator_prefix. Output raw JSON only."
    @staticmethod
    def run(make: str, model: str, feedback: str = "") -> dict:
        prompt = f"Research the component: Make='{make}', Model='{model}'. Feedback: {feedback}"
        raw = ask_gemma(prompt, ComponentResearchAgent.SYSTEM_PROMPT)
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        js = m.group(1) if m else raw
        try: return json.loads(js)
        except: return {"packaging":"Unknown","dimensions":"Unknown","electrical_characteristics":raw[:200],"functional_description":raw[:200],"reference_designator_prefix":"U"}
