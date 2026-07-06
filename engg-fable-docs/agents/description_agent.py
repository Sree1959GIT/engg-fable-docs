"""agents/description_agent.py — Multi-level functional description pipeline.

Hierarchy (master.md §6.2), each level referencing the one below it:

    Level 1  COMPONENT  — role of each part, from research cache / knowledge base
    Level 2  MODULE     — how connected component pairs cooperate (per signal group)
    Level 3  SUBSYSTEM  — narrative synthesis of levels 1-2, with power/data/control flow
    Level 4  SYSTEM     — synthesis of all subsystem overviews + cross-subsystem bridges

Every level tries the LLM first (with the lower level's output as context) and
falls back to a rich programmatic template — so output quality stays
professional even fully offline with no LLM running.

Contextual continuity: the supervisor generates subsystems SEQUENTIALLY and
passes previously written overviews in `prior_context`, so later subsystems
can reference earlier ones instead of being described in isolation.
"""
from typing import Dict, List, Optional

import pandas as pd

from src.component_registry import SIGNAL_CLASSES, classify_signal
from src.config import MAX_TOKENS_DESCRIPTION, MAX_TOKENS_SHORT
from src.llm_client import ask_llm
from src.system_analysis import signal_table

_SYSTEM_PROMPT = (
    "You are a senior electrical engineer writing a technical user manual. "
    "Write precise, professional prose in complete sentences. Use the component "
    "reference designators exactly as given (U1, J1, M1...). Never invent "
    "components, pins, or signals that are not in the provided data. "
    "Do not use markdown headers; write flowing paragraphs."
)


def _dominant_class(subsystem: str, connections: pd.DataFrame) -> str:
    """Functional theme of a sub-module: the name is the strongest hint
    (a 'Motor_Drive' block full of PWM control lines is still an actuation
    stage), signal-class majority is the fallback."""
    n = subsystem.lower()
    if any(w in n for w in ("motor", "drive", "actuat")):
        return "motor"
    if any(w in n for w in ("power", "supply", "batt")):
        return "power"
    if any(w in n for w in ("comm", "interface", "bus", "network")):
        return "data"
    if any(w in n for w in ("control", "logic")):
        return "control"
    classes = [classify_signal(str(s)) for s in connections["Signal_Name"]]
    return max(set(classes), key=classes.count) if classes else "other"


def _direction_summary(cid: str, connections: pd.DataFrame) -> str:
    """One sentence on what this component drives/receives in this subsystem."""
    drives, receives = [], []
    for _, r in connections.iterrows():
        sig = str(r["Signal_Name"])
        if str(r["Component_ID"]) == cid:
            drives.append(f"{sig} to {r['Target_ID']}")
        elif str(r["Target_ID"]) == cid:
            receives.append(f"{sig} from {r['Component_ID']}")
    parts = []
    if drives:
        parts.append("drives " + ", ".join(sorted(set(drives))[:5]))
    if receives:
        parts.append("receives " + ", ".join(sorted(set(receives))[:5]))
    return ("; ".join(parts) + ".") if parts else ""


class DescriptionAgent:

    # ── Level 1: components ───────────────────────────────────────────────
    @staticmethod
    def describe_components(connections: pd.DataFrame, registry: Dict[str, dict],
                            research_cache: Dict[str, dict]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        cids = sorted(set(connections["Component_ID"].astype(str))
                      | set(connections["Target_ID"].astype(str)))
        for cid in cids:
            info = registry.get(cid, {})
            make, model = info.get("make", ""), info.get("model", "")
            ctype = info.get("type", "Component")
            # Best available base description: LLM research > knowledge base > type
            researched = (research_cache or {}).get(f"{make}_{model}", {})
            base = (researched.get("functional_description")
                    or info.get("description")
                    or f"{ctype} used in this subsystem.")
            ident = f"{cid}" + (f" — {make} {model}" if make or model else "")
            role = _direction_summary(cid, connections)
            text = f"{ident} ({ctype}). {base}"
            if role:
                text += f" In this subsystem, {cid} {role}"
            out[cid] = text
        return out

    # ── Level 2: modules (interacting component pairs) ────────────────────
    @staticmethod
    def describe_modules(connections: pd.DataFrame,
                         component_descs: Dict[str, str]) -> List[str]:
        pairs: Dict[tuple, list] = {}
        for _, r in connections.iterrows():
            key = (str(r["Component_ID"]), str(r["Target_ID"]))
            pairs.setdefault(key, []).append(r)

        modules = []
        for (src, tgt), rows in pairs.items():
            sigs = [str(r["Signal_Name"]) for r in rows]
            classes = {classify_signal(s) for s in sigs}
            detail = "; ".join(
                f"{r['Signal_Name']} ({r['Source_Pin']} → {r['Target_Pin']})" for r in rows)
            if classes == {"power"} or classes == {"power", "ground"}:
                verb = "supplies power to"
            elif "data" in classes:
                verb = "communicates with"
            elif "motor" in classes:
                verb = "drives"
            elif "control" in classes:
                verb = "signals"
            else:
                verb = "connects to"
            modules.append(f"{src} {verb} {tgt} via {detail}.")

        # Optional LLM polish, seeded with level-1 output for continuity
        if modules and len(modules) >= 2:
            prompt = (
                "Rewrite the following point-to-point connection facts as one cohesive "
                "paragraph describing how these components work together as a module. "
                "Keep every reference designator and signal name unchanged.\n\n"
                "Component roles:\n" + "\n".join(f"- {d}" for d in component_descs.values())
                + "\n\nConnections:\n" + "\n".join(f"- {m}" for m in modules)
            )
            polished = ask_llm(prompt, _SYSTEM_PROMPT, max_tokens=MAX_TOKENS_DESCRIPTION)
            if polished and len(polished) > 100:
                return [polished]
        return modules

    # ── Level 3: subsystem ────────────────────────────────────────────────
    @staticmethod
    def run(subsystem: str, connections: pd.DataFrame,
            registry: Dict[str, dict] = None,
            research_cache: Dict[str, dict] = None,
            prior_context: str = "",
            interconnections: list = None,
            sys_name: str = "",
            feedback: str = "") -> Dict:
        registry = registry or {}
        comp_descs = DescriptionAgent.describe_components(connections, registry, research_cache or {})
        module_descs = DescriptionAgent.describe_modules(connections, comp_descs)
        signals = signal_table(connections)

        narrative = DescriptionAgent._subsystem_llm(
            subsystem, connections, comp_descs, module_descs, prior_context, feedback)
        if not narrative:
            narrative = DescriptionAgent._subsystem_fallback(
                subsystem, connections, comp_descs, module_descs)

        overview = DescriptionAgent._overview(subsystem, connections, narrative)
        role = DescriptionAgent._role_in_system(
            subsystem, connections, interconnections or [], sys_name, prior_context)

        return {
            "subsystem_name": subsystem,
            "overview": overview,
            "full_description": narrative,
            "role_in_system": role,
            "component_descriptions": comp_descs,
            "module_descriptions": module_descs,
            "signals": signals,
        }

    @staticmethod
    def _role_in_system(subsystem, connections, interconnections,
                        sys_name, prior_context) -> str:
        """Explain this sub-module's role in the context of the ENTIRE system."""
        name = subsystem.replace("_", " ")
        system = (sys_name or "the system").replace("_", " ")
        dominant = _dominant_class(subsystem, connections)
        stage = {
            "power": "It forms the power entry and conditioning stage: every "
                     "downstream function depends on the rails it establishes.",
            "data": "It forms the supervisory and communication stage, closing the "
                    "loop between measurement, protection and actuation.",
            "control": "It provides the command and coordination layer of the system.",
            "motor": "It forms the actuation stage, converting the controller's "
                     "commands into the system's physical output.",
        }.get(dominant, "It provides supporting functions to the rest of the system.")

        link_sentences = []
        for ic in interconnections:
            other = ic["other_subsystem"].replace("_", " ")
            link_sentences.append(
                f"It interfaces with the {other} sub-module through "
                f"{ic['shared_components']} (signals: {ic['signals']}).")

        base = (f"Within the {system}, the {name} sub-module is one of the "
                f"principal functional stages. {stage} "
                + " ".join(link_sentences))

        prompt = (
            f"Rewrite as one flowing paragraph for a technical manual, keeping all "
            f"reference designators and signal names exactly as given:\n{base}"
            + (f"\n\nContext of other sub-modules:\n{prior_context}" if prior_context else ""))
        polished = ask_llm(prompt, _SYSTEM_PROMPT, max_tokens=MAX_TOKENS_SHORT)
        return polished if polished and len(polished) > 80 else base

    @staticmethod
    def _subsystem_llm(subsystem, connections, comp_descs, module_descs,
                       prior_context, feedback) -> str:
        prompt_parts = [
            f"Write the functional description for the '{subsystem.replace('_', ' ')}' "
            "sub-module of an electrical system, as 2-3 paragraphs for a technical manual.",
            "\nBackground data (context only — do NOT reproduce it in the output):",
            "\n".join(f"- {d}" for d in comp_descs.values()),
            "\nConnections (context only):",
            "\n".join(f"- {m}" for m in module_descs),
        ]
        if prior_context:
            prompt_parts.append(
                "\nPreviously documented sub-modules (maintain continuity, reference "
                "them where signals cross the boundary):\n" + prior_context)
        if feedback:
            prompt_parts.append(f"\nReviewer feedback to address: {feedback}")
        prompt_parts.append(
            "\nStructure: (1) the purpose of the sub-module within the system, "
            "(2) its overall functional operation described at stage level, "
            "(3) how it interacts with the other sub-modules. "
            "IMPORTANT style rules: describe the sub-module as ONE functional "
            "stage. Do NOT describe the functionality of individual components "
            "(diodes, resistors, capacitors, inductors, ICs). Do NOT enumerate "
            "pin-to-pin connections. Signal names and reference designators may "
            "be mentioned only as waypoints of the overall flow.")
        raw = ask_llm("\n".join(prompt_parts), _SYSTEM_PROMPT, max_tokens=MAX_TOKENS_DESCRIPTION)
        return raw if raw and len(raw) > 120 else ""

    @staticmethod
    def _subsystem_fallback(subsystem, connections, comp_descs, module_descs) -> str:
        """Professional template narrative — no LLM required.

        Review feedback: the description stays at STAGE level. No per-component
        functionality (diode/resistor/capacitor/IC) and no pin-to-pin
        enumeration — nets are summarized by signal class instead.
        """
        name = subsystem.replace("_", " ")
        n_comp = len(comp_descs)
        dominant = _dominant_class(subsystem, connections)
        purpose = {
            "power": f"The {name} sub-module conditions and distributes electrical power to the rest of the system.",
            "data": f"The {name} sub-module provides the digital communication backbone between the system's intelligent devices.",
            "control": f"The {name} sub-module carries the command and status signals that coordinate system operation.",
            "motor": f"The {name} sub-module converts control commands into the phase currents that drive the motor.",
        }.get(dominant, f"The {name} sub-module interconnects {n_comp} components of the system.")

        # Net-level functional flow, grouped by signal class
        by_class: Dict[str, List[str]] = {}
        for _, r in connections.iterrows():
            sig = str(r["Signal_Name"])
            c = classify_signal(sig)
            if sig not in by_class.setdefault(c, []):
                by_class[c].append(sig)

        sentences = []
        if "power" in by_class:
            nets = by_class["power"]
            if len(nets) > 1:
                sentences.append(
                    f"Power enters the stage on {nets[0]} and, after conditioning, "
                    f"is made available as {', '.join(nets[1:])} for downstream use.")
            else:
                sentences.append(f"The stage carries the supply net {nets[0]}.")
        if "data" in by_class:
            sentences.append(
                "Digital communication between the stage's devices is carried over "
                + ", ".join(by_class["data"]) + ".")
        if "control" in by_class:
            sentences.append(
                "Coordination and protection status are exchanged through the control "
                "signals " + ", ".join(by_class["control"]) + ".")
        if "analog" in by_class:
            sentences.append(
                "Operating conditions are monitored via " + ", ".join(by_class["analog"]) + ".")
        if "motor" in by_class:
            sentences.append(
                "The stage delivers the three-phase outputs " + ", ".join(by_class["motor"])
                + " that drive the motor.")
        if "ground" in by_class:
            sentences.append("A common ground reference is maintained throughout the stage.")

        summary = (f"The sub-module integrates {n_comp} components operating together "
                   f"as a single functional stage; the individual parts are listed in "
                   f"the Bill of Materials.")
        return "\n\n".join([purpose, " ".join(sentences), summary])

    @staticmethod
    def _overview(subsystem, connections, narrative) -> str:
        first = narrative.split("\n")[0]
        if len(first) > 60:
            return first[:400]
        n_comp = len(set(connections["Component_ID"]) | set(connections["Target_ID"]))
        n_sig = connections["Signal_Name"].nunique()
        return (f"The {subsystem.replace('_', ' ')} subsystem comprises {n_comp} components "
                f"exchanging {n_sig} signals.")

    # ── Level 4: system ───────────────────────────────────────────────────
    @staticmethod
    def describe_system(sys_name: str, descriptions: Dict[str, dict],
                        bridges: List[dict], feedback: str = "") -> str:
        sub_overviews = "\n".join(
            f"- {sn.replace('_', ' ')}: {d.get('overview', '')}"
            for sn, d in descriptions.items())
        bridge_lines = "\n".join(
            f"- {b['component']} links {' and '.join(b['subsystems'])} "
            f"({', '.join(b['signal_classes'])})" for b in bridges)

        prompt = (
            f"Write a 2-3 paragraph system-level functional description of the "
            f"'{sys_name.replace('_', ' ')}' for the opening section of its technical manual.\n\n"
            f"Subsystem summaries:\n{sub_overviews}\n\n"
            f"Cross-subsystem links:\n{bridge_lines}\n\n"
            "Explain the end-to-end operation: how power enters, how the controller "
            "coordinates the subsystems, and how the output is produced. Reference each "
            "subsystem by name." + (f"\nReviewer feedback: {feedback}" if feedback else ""))
        raw = ask_llm(prompt, _SYSTEM_PROMPT, max_tokens=MAX_TOKENS_DESCRIPTION)
        if raw and len(raw) > 120:
            return raw

        # Programmatic fallback
        name = sys_name.replace("_", " ")
        paras = [
            f"The {name} is organized into {len(descriptions)} functional subsystems: "
            + ", ".join(sn.replace("_", " ") for sn in descriptions) + "."
        ]
        for sn, d in descriptions.items():
            paras.append(f"{sn.replace('_', ' ')}: {d.get('overview', '')}")
        if bridges:
            links = " ".join(
                f"{b['component']} spans {' and '.join(s.replace('_', ' ') for s in b['subsystems'])}, "
                f"carrying {', '.join(b['signal_classes']).lower()} signals across the boundary."
                for b in bridges)
            paras.append("Subsystem integration: " + links)
        return "\n\n".join(paras)
