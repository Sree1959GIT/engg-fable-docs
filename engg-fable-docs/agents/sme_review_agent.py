"""agents/sme_review_agent.py — Subject-matter-expert review of descriptions.

Every generated description is independently reviewed before it enters the
manual. Two layers:

  1. Deterministic checks (always run, no LLM needed):
     - hallucination scan: designator-like tokens in the text that do not
       exist in the connection data / registry
     - minimum substance (length, mentions the sub-module)
     - style rules from the review spec: no pin-to-pin enumeration
  2. LLM SME pass (when the local server is up): a senior-engineer prompt
     grades accuracy and style and returns actionable feedback.

If the review fails, the supervisor re-runs the description agent with the
reviewer's feedback attached (bounded retries).
"""
import re
from typing import Dict, List

import pandas as pd

from src.config import MAX_TOKENS_SHORT
from src.llm_client import ask_llm

# Tokens that look like designators but are legitimate engineering vocabulary
_WHITELIST = {
    "I2C", "I2S", "SPI1", "RS232", "RS485", "CAN2", "USB2", "USB3", "CAT5",
    "CAT6", "3V3", "5V", "12V", "24V", "48V", "230V", "0V", "A4", "IEC60617",
    "IEC81346", "2X5", "1X13", "8X", "3A", "40V", "10KL",
}

# Allow up to 3 hyphen-joined segments (e.g. "B3-JP6-JTFPG") and underscores
# within a segment (e.g. "...JTFPG_NET"). Both fixes matter together: \b is
# a \w/\W transition and underscore IS a \w character, so without allowing
# it inside the class, "B3-JP6-JTFPG_NET" failed to find a boundary after
# the full designator and the regex backtracked to the truncated, spurious
# "B3-JP6" — a false hallucination flag on a perfectly real signal name.
_DESIGNATOR_RE = re.compile(r"\b[A-Z][A-Z0-9_]{1,10}(?:-[A-Z0-9_]{1,10}){0,3}\b")
_PIN_ENUM_RE = re.compile(r"\(pin [^)]+\)\s+to\s+\w+\s+\(pin", re.IGNORECASE)


class SMEReviewAgent:

    @staticmethod
    def review(subsystem: str, description: Dict, connections: pd.DataFrame,
               registry: Dict[str, dict], all_subsystems: List[str] = None,
               full_connections: pd.DataFrame = None) -> Dict:
        text = " ".join([
            str(description.get("full_description", "")),
            str(description.get("role_in_system", "")),
        ])
        known = (set(connections["Component_ID"].astype(str))
                 | set(connections["Target_ID"].astype(str))
                 | set(connections["Signal_Name"].astype(str))
                 | set(registry.keys()))
        # "Role in the Overall System" legitimately names signals that cross
        # a bridging component but only appear on the OTHER subsystem's own
        # rows (interconnections_for reports the union across both sides) —
        # so the vocabulary must span the whole dataset, not just this slice.
        if full_connections is not None and not full_connections.empty:
            known.update(full_connections["Component_ID"].astype(str))
            known.update(full_connections["Target_ID"].astype(str))
            known.update(full_connections["Signal_Name"].astype(str))
        # system / sub-module names are legitimate vocabulary too — the
        # role-in-system text references NEIGHBOURING sub-modules by name
        for col in ("System_Name", "Subsystem_Name"):
            if col in connections.columns:
                known.update(connections[col].astype(str))
        known.update(all_subsystems or [])
        # every word of every signal/component also counts (GND_POWER → GND)
        for k in list(known):
            known.update(k.replace("-", "_").split("_"))

        issues: List[str] = []

        # 1. hallucinated designators
        suspicious = sorted({
            t for t in _DESIGNATOR_RE.findall(text)
            if any(ch.isdigit() for ch in t)
            and t not in known and t not in _WHITELIST
        })
        if suspicious:
            issues.append("mentions designators not present in the data: "
                          + ", ".join(suspicious[:8]))

        # 2. substance
        if len(text) < 150:
            issues.append("description too short for a manual section")
        name_words = subsystem.replace("_", " ").lower()
        if name_words not in text.lower():
            issues.append("does not reference the sub-module by name")

        # 3. style: stage-level prose, not pin-by-pin enumeration
        if _PIN_ENUM_RE.search(text):
            issues.append("contains pin-to-pin enumeration; rewrite at stage level")

        # 4. optional LLM SME pass
        llm_notes = ""
        if not issues:
            prompt = (
                f"You are a senior electrical engineer reviewing a manual section "
                f"for the '{subsystem.replace('_', ' ')}' sub-module.\n\n"
                f"Section text:\n{text[:2500]}\n\n"
                "Answer with exactly 'APPROVED' if it is accurate, stage-level "
                "prose suitable for a technical manual. Otherwise reply with "
                "one short sentence of corrective feedback.")
            llm_notes = ask_llm(prompt, max_tokens=MAX_TOKENS_SHORT) or ""
            if llm_notes and "APPROVED" not in llm_notes.upper()[:40] \
                    and len(llm_notes) > 15:
                issues.append(f"SME note: {llm_notes[:200]}")

        return {
            "status": "pass" if not issues else "revise",
            "issues": issues,
            "feedback": "; ".join(issues),
            "detail": ("approved" if not issues
                       else f"{len(issues)} issue(s) — rework requested"),
        }
