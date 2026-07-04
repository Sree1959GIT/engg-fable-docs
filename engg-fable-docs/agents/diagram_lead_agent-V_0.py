"""agents/diagram_lead_agent.py — Self-contained fallback diagram builder (no LLM needed)."""
import os
import re
import graphviz
import pandas as pd
from typing import Dict, Optional, List, Tuple


# ── IEC 81346 Reference Designator Prefixes ──
TYPE_TO_REFDES = {
    "IC": "U", "Integrated Circuit": "U", "Microcontroller": "U", "Microprocessor": "U",
    "Resistor": "R", "Capacitor": "C", "Inductor": "L",
    "Connector": "J", "Terminal": "X", "Header": "J",
    "Switch": "S", "Relay": "K", "Transformer": "T",
    "Diode": "D", "LED": "D", "Transistor": "Q",
    "Fuse": "F", "Motor": "M", "Sensor": "B",
    "Amplifier": "A", "Filter": "Z", "Battery": "BT",
    "Default": "U",
}


def _get_refdes(make: str, model: str) -> str:
    """Guess IEC prefix from model name keywords."""
    m = model.lower()
    if any(w in m for w in ["connector", "header", "terminal", "plug", "jack"]):
        return "J"
    if any(w in m for w in ["diode", "schottky", "led", "rectifier"]):
        return "D"
    if any(w in m for w in ["motor", "actuator"]):
        return "M"
    if any(w in m for w in ["resistor", "r."]):
        return "R"
    if any(w in m for w in ["capacitor", "cap", "electrolytic"]):
        return "C"
    if any(w in m for w in ["inductor", "coil"]):
        return "L"
    if any(w in m for w in ["transistor", "mosfet", "bjt"]):
        return "Q"
    if any(w in m for w in ["sensor", "detector"]):
        return "B"
    if any(w in m for w in ["battery", "cell"]):
        return "BT"
    return "U"  # Default for ICs


def _get_shape(model: str) -> str:
    """Return Graphviz node shape based on component type."""
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
    if any(w in m for w in ["resistor"]):
        return "box"
    if any(w in m for w in ["capacitor"]):
        return "box"
    return "box"  # Default


def _get_fillcolor(subsystem: str) -> str:
    """Return fill color for a subsystem."""
    color_map = {
        "power": "#FFF2CC", "battery": "#D9EAD3", "motor": "#F4CCCC",
        "drive": "#F4CCCC", "control": "#CFE2F3", "comm": "#D9D2E9",
        "sensor": "#FCE5CD", "interface": "#E6D5F5", "safety": "#FFE0E0",
    }
    s = subsystem.lower()
    for key, color in color_map.items():
        if key in s:
            return color
    return "#F3F3F3"


def _get_edge_color(signal_name: str) -> str:
    """Return edge color based on signal name keywords."""
    s = signal_name.lower()
    if any(w in s for w in ["vcc", "vdd", "vbat", "power", "v_", "3v3", "5v", "12v", "gnd", "vdd"]):
        return "red"
    if any(w in s for w in ["spi", "i2c", "uart", "data", "clk", "miso", "mosi", "sda", "scl"]):
        return "blue"
    if any(w in s for w in ["pwm", "fault", "alert", "en", "enable", "ctrl"]):
        return "green"
    if any(w in s for w in ["phase", "phase_u", "phase_v", "phase_w"]):
        return "#800080"  # Purple for motor phases
    return "#333333"


def _get_edge_penwidth(signal_name: str) -> float:
    """Return edge thickness based on signal type."""
    s = signal_name.lower()
    if any(w in s for w in ["phase", "power", "vcc", "vdd", "vbat", "gnd", "12v"]):
        return 2.0
    if any(w in s for w in ["pwm", "fault", "alert"]):
        return 1.2
    return 1.0


def _get_edge_style(signal_name: str) -> str:
    """Return edge style (dashed for GND, solid for others)."""
    if signal_name.lower() in ["gnd", "gnd_ref", "ground"]:
        return "dashed"
    return "solid"


def _build_html_pin_table(comp_id: str, make: str, model: str,
                          connections: pd.DataFrame) -> str:
    """Build an HTML-like pin table for an IC or complex component."""
    # Get all unique pins for this component from the data
    src_pins = connections[connections["Component_ID"] == comp_id]["Source_Pin"].unique()
    tgt_pins = connections[connections["Target_ID"] == comp_id]["Target_Pin"].unique()
    all_pins = sorted(set(list(src_pins) + list(tgt_pins)))

    # Organize into left and right columns
    left_pins = all_pins[:len(all_pins)//2 + 1] if len(all_pins) > 1 else all_pins
    right_pins = all_pins[len(left_pins):]

    # Build HTML table
    rows = []
    rows.append(f'<TR><TD COLSPAN="2" BGCOLOR="lightblue"><B>{comp_id}</B><BR/>{model}</TD></TR>')
    for i in range(max(len(left_pins), len(right_pins), 1)):
        left = left_pins[i] if i < len(left_pins) else ""
        right = right_pins[i] if i < len(right_pins) else ""
        rows.append(f'<TR><TD PORT="p{i*2}">{left}</TD><TD PORT="p{i*2+1}">{right}</TD></TR>')

    return (
        '<<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0">'
        + "".join(rows) +
        '</TABLE>>'
    )


class DiagramLeadAgent:
    """
    Fallback diagram builder — generates DOT code directly from Excel data.
    No LLM required. Follows IEC 60617/81346 conventions.
    """

    @staticmethod
    def run(subsystem: str, connections: pd.DataFrame,
            specs_cache: Dict[str, dict] = None,
            feedback: str = "",
            output_dir: str = "output/diagrams") -> Optional[str]:
        """Generate wiring diagram for a subsystem. Returns PNG path or None."""
        return DiagramLeadAgent._build_and_render(subsystem, connections, output_dir)

    @staticmethod
    def _build_and_render(subsystem: str, connections: pd.DataFrame,
                          output_dir: str) -> Optional[str]:
        """Build DOT code programmatically and render to PNG."""
        dot_code = DiagramLeadAgent._generate_dot(subsystem, connections)
        if not dot_code:
            return None

        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{subsystem}_diagram")

        try:
            src = graphviz.Source(dot_code, format="png")
            src.render(out_path, cleanup=True)
            print(f"  ✓ [Diagram] Rendered: {out_path}.png")
            return f"{out_path}.png"
        except Exception as e:
            print(f"  ⚠ [Diagram] Render failed: {e}")
            print("  ⚠ [Diagram] Make sure Graphviz binaries are installed:")
            print("     https://graphviz.org/download/")
            # Save DOT for debugging
            with open(f"{out_path}.dot", "w") as f:
                f.write(dot_code)
            print(f"  ⚠ [Diagram] DOT saved to {out_path}.dot")
            return None

    @staticmethod
    def _generate_dot(subsystem: str, connections: pd.DataFrame) -> str:
        """Generate DOT code from connectivity data."""
        fill = _get_fillcolor(subsystem)
        lines = []
        lines.append(f"digraph {subsystem.replace(' ', '_')} {{")
        lines.append("  rankdir=LR;")
        lines.append("  nodesep=0.6;")
        lines.append("  ranksep=1.0;")
        lines.append("  splines=ortho;")
        lines.append(f'  label="{subsystem}";')
        lines.append('  fontname="Helvetica,Arial,sans-serif";')
        lines.append('  node [fontname="Helvetica,Arial,sans-serif", fontsize=10];')
        lines.append('  edge [fontname="Helvetica,Arial,sans-serif", fontsize=9];')
        lines.append("")

        # Collect all unique components
        all_components = {}
        for _, row in connections.iterrows():
            src_id = row["Component_ID"]
            tgt_id = row["Target_ID"]
            for cid, make, model in [
                (src_id, row["Make"], row["Model"]),
                (tgt_id, row["Make"], row["Model"]),  # Use same make/model for target
            ]:
                if cid not in all_components:
                    all_components[cid] = (make, model)

        # Add node definitions
        lines.append(f"  // Nodes")
        for comp_id, (make, model) in all_components.items():
            refdes = _get_refdes(make, model)
            shape = _get_shape(model)
            label = f'"{refdes}:{comp_id}\\n{make}\\n{model}"'

            # Determine if this component has many pins (use HTML table for ICs)
            comp_conns = connections[
                (connections["Component_ID"] == comp_id) |
                (connections["Target_ID"] == comp_id)
            ]
            unique_pins = set(
                list(comp_conns[comp_conns["Component_ID"] == comp_id]["Source_Pin"].unique()) +
                list(comp_conns[comp_conns["Target_ID"] == comp_id]["Target_Pin"].unique())
            )

            if len(unique_pins) >= 3 and shape == "box":
                # Use HTML pin table for multi-pin ICs
                html_label = _build_html_pin_table(comp_id, make, model, connections)
                lines.append(f'  {comp_id} [shape=plaintext, label={html_label}];')
            else:
                lines.append(f'  {comp_id} [shape={shape}, style=filled, '
                             f'fillcolor=white, label={label}];')

        lines.append("")
        lines.append(f"  // Connections")

        # Add edges
        for _, row in connections.iterrows():
            src = row["Component_ID"]
            tgt = row["Target_ID"]
            signal = row["Signal_Name"]
            src_pin = row["Source_Pin"]
            tgt_pin = row["Target_Pin"]

            color = _get_edge_color(signal)
            pw = _get_edge_penwidth(signal)
            style = _get_edge_style(signal)
            label_text = f"{signal}\\n({src_pin}→{tgt_pin})"

            # Check if source has an HTML pin table
            has_html = (src in all_components and
                       _get_shape(all_components[src][1]) == "box" and
                       len(set(connections[connections["Component_ID"] == src]["Source_Pin"].unique())) >= 3)

            if has_html:
                # Use pin ports
                pin_idx = 0
                src_conns = connections[connections["Component_ID"] == src]
                src_pins = sorted(src_conns["Source_Pin"].unique())
                if src_pin in src_pins:
                    pin_idx = src_pins.index(src_pin)
                lines.append(f'  {src}:p{pin_idx*2} -> {tgt} '
                             f'[color="{color}", penwidth={pw}, style="{style}", '
                             f'label="{label_text}"];')
            else:
                lines.append(f'  {src} -> {tgt} '
                             f'[color="{color}", penwidth={pw}, style="{style}", '
                             f'label="{label_text}"];')

        lines.append("}")
        return "\n".join(lines)

    @staticmethod
    def generate_fallback_dot(subsystem: str, connections: pd.DataFrame) -> str:
        """Alias for _generate_dot — explicit fallback entry point."""
        return DiagramLeadAgent._generate_dot(subsystem, connections)
