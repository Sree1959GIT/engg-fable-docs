"""agents/diagram_lead_agent.py — Fallback diagram builder (no LLM needed).
Generates DOT with proper routing, IEC 60617/81346 conventions, and system-level overview."""
import os
import graphviz
import pandas as pd
from typing import Dict, Optional

# IEC 81346 Reference Designator Prefixes
TYPE_PREFIXES = {
    "connector": "J", "header": "J", "terminal": "X", "plug": "J",
    "diode": "D", "schottky": "D", "led": "D", "rectifier": "D",
    "motor": "M", "actuator": "M",
    "resistor": "R", "capacitor": "C", "inductor": "L",
    "transistor": "Q", "mosfet": "Q", "bjt": "Q",
    "sensor": "B", "detector": "B",
    "battery": "BT", "cell": "BT",
    "switch": "S", "relay": "K", "fuse": "F",
    "transformer": "T", "amplifier": "A", "filter": "Z",
}

SUBSYSTEM_COLORS = {
    "power": "#FFF2CC", "battery": "#D9EAD3", "motor": "#F4CCCC",
    "drive": "#F4CCCC", "control": "#CFE2F3", "communication": "#D9D2E9",
    "sensor": "#FCE5CD", "interface": "#E6D5F5", "safety": "#FFE0E0",
}


def _refdes(model: str) -> str:
    m = model.lower()
    for key, prefix in TYPE_PREFIXES.items():
        if key in m:
            return prefix
    return "U"


def _shape(model: str) -> str:
    m = model.lower()
    if any(w in m for w in ["connector", "header", "terminal"]):
        return "parallelogram"
    if any(w in m for w in ["diode", "schottky", "led"]):
        return "diamond"
    if any(w in m for w in ["motor"]):
        return "invtriangle"
    if any(w in m for w in ["sensor"]):
        return "diamond"
    if any(w in m for w in ["battery"]):
        return "ellipse"
    return "box"


def _color(subsystem: str) -> str:
    s = subsystem.lower()
    for key, clr in SUBSYSTEM_COLORS.items():
        if key in s:
            return clr
    return "#F3F3F3"


def _signal_color(signal: str) -> str:
    s = signal.lower()
    if any(w in s for w in ["vcc", "vdd", "vbat", "v_", "3v3", "5v", "12v", "gnd"]):
        return "red"
    if any(w in s for w in ["spi", "i2c", "uart", "data", "clk", "miso", "mosi"]):
        return "blue"
    if any(w in s for w in ["pwm", "fault", "alert", "enable"]):
        return "green"
    if any(w in s for w in ["phase"]):
        return "#800080"
    return "#555555"


class DiagramLeadAgent:
    """Generates wiring diagrams. Falls back to programmatic DOT if LLM fails."""

    @staticmethod
    def run(subsystem: str, connections: pd.DataFrame,
            specs_cache: Dict[str, dict] = None,
            feedback: str = "",
            output_dir: str = "output/diagrams") -> Optional[str]:
        return DiagramLeadAgent._build_and_render(subsystem, connections, output_dir)

    @staticmethod
    def build_system_diagram(df_conn: pd.DataFrame,
                             output_dir: str = "output/diagrams") -> Optional[str]:
        """Generate an overall system-level block diagram showing all subsystems."""
        lines = []
        lines.append("digraph System_Overview {")
        lines.append("  rankdir=TB;")  # Top-to-bottom for system view
        lines.append("  nodesep=0.8;")
        lines.append("  ranksep=1.2;")
        lines.append("  splines=true;")
        lines.append('  label="System Block Diagram";')
        lines.append('  fontsize=16;')
        lines.append("")

        # One cluster per subsystem
        for sub_name, group in df_conn.groupby("Subsystem_Name"):
            fill = _color(sub_name)
            safe_sub = sub_name.replace(" ", "_").replace("-", "_")
            lines.append(f'  subgraph cluster_{safe_sub} {{')
            lines.append(f'    label="{sub_name}";')
            lines.append(f'    style="filled,rounded";')
            lines.append(f'    bgcolor="{fill}";')
            lines.append(f'    fontsize=14;')
            lines.append('    node [style=filled, fillcolor=white, fontsize=10];')

            # Collect unique component IDs in this subsystem
            comps = set()
            for _, r in group.iterrows():
                comps.add(r["Component_ID"])
                comps.add(r["Target_ID"])
            for cid in comps:
                lines.append(f'    {cid.replace("-","_").replace(".","_")} '
                             f'[label="{cid}", shape=box, width=1.2, height=0.4];')
            lines.append("  }")
            lines.append("")

        # Inter-subsystem connections
        lines.append("  // Inter-subsystem connections")
        for sub_name, group in df_conn.groupby("Subsystem_Name"):
            for _, r in group.iterrows():
                src = r["Component_ID"].replace("-", "_").replace(".", "_")
                tgt = r["Target_ID"].replace("-", "_").replace(".", "_")
                sig = r["Signal_Name"]
                lines.append(f'  {src} -> {tgt} [label="{sig}", '
                             f'color="{_signal_color(sig)}", penwidth=1.2];')

        lines.append("}")

        return DiagramLeadAgent._render_dot("\n".join(lines), "System_Overview", output_dir)

    @staticmethod
    def _build_and_render(subsystem: str, connections: pd.DataFrame,
                          output_dir: str) -> Optional[str]:
        """Build DOT code programmatically and render to PNG."""
        dot = DiagramLeadAgent._generate_dot(subsystem, connections)
        if not dot:
            return None
        return DiagramLeadAgent._render_dot(dot, subsystem, output_dir)

    @staticmethod
    def _render_dot(dot_code: str, name: str, output_dir: str) -> Optional[str]:
        """Render DOT code to PNG."""
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{name}_diagram")
        try:
            graphviz.Source(dot_code, format="png").render(out_path, cleanup=True)
            print(f"  ✓ [Diagram] Rendered: {out_path}.png")
            return f"{out_path}.png"
        except Exception as e:
            print(f"  ⚠ [Diagram] Render failed: {e}")
            print("     Install Graphviz: https://graphviz.org/download/")
            with open(f"{out_path}.dot", "w") as f:
                f.write(dot_code)
            return None

    @staticmethod
    def _generate_dot(subsystem: str, connections: pd.DataFrame) -> str:
        """Generate DOT code from connectivity data with proper routing."""
        fill = _color(subsystem)
        lines = []
        safe_name = subsystem.replace(" ", "_").replace("-", "_")
        lines.append(f"digraph {safe_name} {{")
        lines.append("  rankdir=LR;")
        lines.append("  nodesep=0.8;")          # More spacing to prevent overlap
        lines.append("  ranksep=1.5;")           # More rank spacing
        lines.append("  splines=true;")          # Curved splines avoid overlap better
        lines.append("  overlap=false;")
        lines.append(f'  label="{subsystem}";')
        lines.append('  fontname="Arial";')
        lines.append('  node [fontname="Arial", fontsize=10];')
        lines.append('  edge [fontname="Arial", fontsize=9];')
        lines.append("")

        # Collect all unique components
        comps = {}
        for _, r in connections.iterrows():
            for cid, make, model in [
                (r["Component_ID"], r["Make"], r["Model"]),
                (r["Target_ID"], r["Make"], r["Model"]),
            ]:
                if cid not in comps:
                    comps[cid] = (make, model)

        # Add node definitions with IEC 81346 labels
        lines.append("  // Components")
        for cid, (make, model) in comps.items():
            rd = _refdes(model)
            sh = _shape(model)
            safe_cid = cid.replace("-", "_").replace(".", "_")
            label = f"{rd}:{cid}\\n{make}\\n{model}"
            lines.append(f'  {safe_cid} [shape={sh}, style=filled, fillcolor=white, '
                         f'label="{label}"];')

        lines.append("")
        lines.append("  // Connections")

        # Group edges by type to avoid crossing
        power_edges = []
        data_edges = []
        ctrl_edges = []
        motor_edges = []
        other_edges = []

        for _, r in connections.iterrows():
            src = r["Component_ID"].replace("-", "_").replace(".", "_")
            tgt = r["Target_ID"].replace("-", "_").replace(".", "_")
            sig = r["Signal_Name"]
            sp = r["Source_Pin"]
            tp = r["Target_Pin"]
            color = _signal_color(sig)
            pw = "2.0" if color == "red" else "1.5" if color == "#800080" else "1.0"
            style = "dashed" if sig.lower() in ["gnd", "gnd_ref", "ground"] else "solid"
            label = f"{sig}\\n({sp}→{tp})"
            edge = f'  {src} -> {tgt} [color="{color}", penwidth={pw}, style="{style}", label="{label}"];'

            if color == "red":
                power_edges.append(edge)
            elif color == "blue":
                data_edges.append(edge)
            elif color == "green":
                ctrl_edges.append(edge)
            elif color == "#800080":
                motor_edges.append(edge)
            else:
                other_edges.append(edge)

        # Output edges grouped by type
        if power_edges:
            lines.append("  // Power")
            lines.extend(power_edges)
        if data_edges:
            lines.append("  // Data / Communication")
            lines.extend(data_edges)
        if ctrl_edges:
            lines.append("  // Control")
            lines.extend(ctrl_edges)
        if motor_edges:
            lines.append("  // Motor Phases")
            lines.extend(motor_edges)
        if other_edges:
            lines.append("  // Other")
            lines.extend(other_edges)

        lines.append("}")
        return "\n".join(lines)
