"""agents/diagram_lead_agent.py — Deterministic CAD-style diagram generation.

Design decisions (see IMPROVEMENTS.md):
- Diagrams are generated PROGRAMMATICALLY, never by the LLM. DOT syntax from a
  small local model is unreliable; connectivity data is already structured.
- Components are drawn as IC-style pin tables (HTML-like labels) with one PORT
  per pin. Edges attach to pin cells on the node boundary (tailport/headport),
  which is what eliminates lines passing through blocks.
- splines=polyline for wiring sheets: Graphviz's ortho router IGNORES ports
  (edges get spread evenly along the node side instead of attaching at their
  pin cell — the root cause of wires entering blocks at the wrong row or
  through the bottom border). polyline honors ports exactly, so every wire
  starts and ends at its true pin. The system block diagram (no pin ports)
  keeps splines=ortho for the Manhattan look.
- sep/esep widen the router's clearance margin so wires keep distance from
  block borders instead of hugging them, and outputorder=edgesfirst paints
  nodes on top of edges so no wire can ever be drawn across a component block.
- Feedback signals (target left of source in the left-to-right layout, e.g.
  SPI_MISO, DRV_FAULT) are emitted reversed with dir=back: layout ranks stay
  strictly left-to-right, so the orthogonal router never wraps a return wire
  over the top of the blocks.
- Signal names are drawn as edge xlabels colored by signal class
  (red=power, blue=data, green=control, purple=motor — master.md §6.1).
- Every sheet gets an IEC-style title block (title / doc no / rev / date) and
  a signal-class legend.
- Output: PNG (for DOCX/PDF embedding) + SVG (scalable, for review).
"""
import html
import os
import re
from datetime import date
from typing import Dict, Optional

import graphviz
import pandas as pd

from src.component_registry import (
    SIGNAL_CLASSES,
    build_component_registry,
    classify_signal,
    shape_for_prefix,
)
from src.config import DIAGRAM_DIR, DOC_NUMBER, DOC_VERSION

# Header fill per IEC 81346 class letter
_CLASS_HEADER_COLORS = {
    "U": "#1F4E79", "J": "#7F6000", "X": "#7F6000", "D": "#843C0C",
    "M": "#5B2C6F", "R": "#375623", "C": "#375623", "L": "#375623",
    "F": "#990000", "K": "#3E3E3E", "B": "#0B5345", "Q": "#843C0C",
    "T": "#3E3E3E", "S": "#3E3E3E", "BT": "#1A5276",
}


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s))


def _port_id(pin: str) -> str:
    return "p_" + _safe_id(pin)


def _node_html(cid: str, info: dict, pins: list) -> str:
    """IC-style block: header (refdes + type), make/model row, one row per pin port."""
    header_color = _CLASS_HEADER_COLORS.get(info.get("prefix", "U"), "#1F4E79")
    make = html.escape(info.get("make") or "")
    model = html.escape(info.get("model") or "")
    ctype = html.escape(info.get("type") or "")
    rows = [
        f'<TR><TD BGCOLOR="{header_color}" COLSPAN="1">'
        f'<FONT COLOR="white" POINT-SIZE="11"><B>{html.escape(cid)}</B></FONT></TD></TR>',
        f'<TR><TD BGCOLOR="#EDEDED"><FONT POINT-SIZE="8">{ctype}</FONT></TD></TR>',
    ]
    if make or model:
        rows.append(f'<TR><TD BGCOLOR="#FFFFFF"><FONT POINT-SIZE="8">{make}<BR/>{model}</FONT></TD></TR>')
    for pin in pins:
        rows.append(
            f'<TR><TD PORT="{_port_id(pin)}" BGCOLOR="#FFFFFF" ALIGN="LEFT">'
            f'<FONT POINT-SIZE="9">{html.escape(str(pin))}</FONT></TD></TR>'
        )
    return ('<<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0" CELLPADDING="3">'
            + "".join(rows) + "</TABLE>>")


def _title_block(title: str, sheet: str) -> str:
    today = date.today().isoformat()
    return (
        '<<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4">'
        f'<TR><TD COLSPAN="4"><B>{html.escape(title)}</B></TD></TR>'
        f'<TR><TD>Doc: {html.escape(DOC_NUMBER)}</TD><TD>Rev: {html.escape(DOC_VERSION)}</TD>'
        f'<TD>Date: {today}</TD><TD>Sheet: {html.escape(sheet)}</TD></TR>'
        "</TABLE>>"
    )


def _legend_html(classes_used) -> str:
    cells = "".join(
        f'<TR><TD BGCOLOR="{SIGNAL_CLASSES[c]["color"]}" WIDTH="14"></TD>'
        f'<TD ALIGN="LEFT"><FONT POINT-SIZE="8">{SIGNAL_CLASSES[c]["label"]}</FONT></TD></TR>'
        for c in classes_used
    )
    return ('<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="2" CELLPADDING="2">'
            '<TR><TD COLSPAN="2"><FONT POINT-SIZE="9"><B>Legend</B></FONT></TD></TR>'
            + cells + "</TABLE>>")


class DiagramLeadAgent:
    """Generates subsystem wiring diagrams and the system-level block diagram."""

    @staticmethod
    def run(subsystem: str, connections: pd.DataFrame,
            registry: Dict[str, dict] = None,
            feedback: str = "",
            output_dir: str = DIAGRAM_DIR) -> Optional[str]:
        if registry is None:
            registry = build_component_registry(connections)
        dot = DiagramLeadAgent._subsystem_dot(subsystem, connections, registry)
        return DiagramLeadAgent._render(dot, _safe_id(subsystem), output_dir)

    # ── Subsystem wiring diagram ──────────────────────────────────────────
    @staticmethod
    def _subsystem_dot(subsystem: str, connections: pd.DataFrame,
                       registry: Dict[str, dict]) -> str:
        # Which pins does each component use in THIS subsystem?
        pins: Dict[str, list] = {}
        for _, r in connections.iterrows():
            src, tgt = str(r["Component_ID"]), str(r["Target_ID"])
            sp, tp = str(r["Source_Pin"]), str(r["Target_Pin"])
            pins.setdefault(src, [])
            pins.setdefault(tgt, [])
            if sp not in pins[src]:
                pins[src].append(sp)
            if tp not in pins[tgt]:
                pins[tgt].append(tp)

        classes_used = sorted({classify_signal(s) for s in connections["Signal_Name"]})

        # Dominant flow direction per component pair. A minority-direction
        # edge (e.g. the single SPI_MISO return against three SPI outputs) is
        # a feedback signal: drawing it as-is makes Graphviz route it around /
        # over the blocks. We emit it reversed with dir=back instead, so the
        # layout stays strictly left-to-right and the arrowhead still points
        # at the true target.
        pair_count: Dict[tuple, int] = {}
        for _, r in connections.iterrows():
            key = (str(r["Component_ID"]), str(r["Target_ID"]))
            pair_count[key] = pair_count.get(key, 0) + 1

        def _is_feedback(a: str, b: str) -> bool:
            return pair_count.get((b, a), 0) > pair_count.get((a, b), 0)

        lines = [
            f"digraph {_safe_id(subsystem)} {{",
            "  rankdir=LR;",
            "  splines=polyline;",  # NOT ortho: ortho ignores pin ports (see module docstring)
            "  nodesep=0.9;",
            "  ranksep=1.8;",
            '  sep="+24";',    # clearance the router keeps around nodes
            '  esep="+16";',   # clearance around edges (must be < sep)
            "  outputorder=edgesfirst;",  # nodes drawn over edges: wires can never cross a block
            "  concentrate=false;",
            '  fontname="Helvetica";',
            f"  label={_title_block(subsystem.replace('_', ' ') + ' — Wiring Diagram', subsystem)};",
            "  labelloc=b;",
            '  node [fontname="Helvetica", shape=plaintext];',
            '  edge [fontname="Helvetica", fontsize=8, arrowsize=0.6];',
            "",
            f"  legend [label={_legend_html(classes_used)}];",
            "",
        ]

        for cid, pin_list in pins.items():
            info = registry.get(cid, {"prefix": "U", "type": "Component", "make": "", "model": ""})
            if info.get("is_rail"):
                # Power rails are drawn as supply flags, not IC blocks
                ports = "".join(
                    f'<TR><TD PORT="{_port_id(p)}"><FONT POINT-SIZE="8">{html.escape(str(p))}</FONT></TD></TR>'
                    for p in pin_list)
                lbl = ('<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" BGCOLOR="#FBE5D6">'
                       f'<TR><TD><FONT POINT-SIZE="10"><B>▽ {html.escape(cid)}</B></FONT></TD></TR>'
                       + ports + "</TABLE>>")
                lines.append(f"  {_safe_id(cid)} [label={lbl}];")
            else:
                lines.append(f"  {_safe_id(cid)} [label={_node_html(cid, info, pin_list)}];")

        lines.append("")
        for _, r in connections.iterrows():
            src_raw, tgt_raw = str(r["Component_ID"]), str(r["Target_ID"])
            src, tgt = _safe_id(src_raw), _safe_id(tgt_raw)
            sp, tp = _port_id(str(r["Source_Pin"])), _port_id(str(r["Target_Pin"]))
            sig = str(r["Signal_Name"])
            cls = classify_signal(sig)
            color = SIGNAL_CLASSES[cls]["color"]
            pw = "2.2" if cls in ("power", "motor") else "1.3"
            style = "dashed" if cls == "ground" else "solid"
            attrs = (
                f'xlabel=<<FONT COLOR="{color}" POINT-SIZE="8">{html.escape(sig)}</FONT>>, '
                f'color="{color}", penwidth={pw}, style="{style}"'
            )
            if _is_feedback(src_raw, tgt_raw):
                # Reversed emission: layout sees target→source (forward), the
                # arrowhead is drawn at the tail — i.e. still at the true target.
                lines.append(f"  {tgt}:{tp}:e -> {src}:{sp}:w [dir=back, {attrs}];")
            else:
                lines.append(f"  {src}:{sp}:e -> {tgt}:{tp}:w [{attrs}];")

        lines.append("}")
        return "\n".join(lines)

    # ── System-level block diagram ────────────────────────────────────────
    @staticmethod
    def build_system_diagram(df_conn: pd.DataFrame,
                             registry: Dict[str, dict] = None,
                             output_dir: str = DIAGRAM_DIR) -> Optional[str]:
        """One block per subsystem (listing its components); edges show the
        bridging components / signal classes that link subsystems."""
        if registry is None:
            registry = build_component_registry(df_conn)
        sys_name = str(df_conn["System_Name"].iloc[0]) if "System_Name" in df_conn.columns else "System"

        sub_components: Dict[str, set] = {}
        for sub, g in df_conn.groupby("Subsystem_Name"):
            ids = set(g["Component_ID"].astype(str)) | set(g["Target_ID"].astype(str))
            sub_components[str(sub)] = ids

        # Order sub-modules by signal flow (power entry -> control -> actuation),
        # matching CAD block-diagram convention (numbered stages left to right).
        def _flow_rank(sub: str) -> int:
            g = df_conn[df_conn["Subsystem_Name"].astype(str) == sub]
            classes = [classify_signal(str(s)) for s in g["Signal_Name"]]
            dominant = max(set(classes), key=classes.count) if classes else "other"
            return {"power": 0, "ground": 0, "data": 1, "control": 1,
                    "analog": 2, "motor": 3}.get(dominant, 2)

        subs = sorted(sub_components, key=_flow_rank)
        lines = [
            "digraph System_Overview {",
            "  rankdir=LR;",
            "  splines=ortho;",
            "  nodesep=1.0;",
            "  ranksep=1.8;",
            '  sep="+24";',
            '  esep="+16";',
            "  outputorder=edgesfirst;",
            '  fontname="Helvetica";',
            f"  label={_title_block(sys_name.replace('_', ' ') + ' — System Block Diagram', 'System')};",
            "  labelloc=b;",
            '  node [fontname="Helvetica", shape=plaintext];',
            '  edge [fontname="Helvetica", fontsize=9];',
            "",
        ]

        for n, sub in enumerate(subs, start=1):
            comp_rows = "".join(
                f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="9">{html.escape(c)} — '
                f'{html.escape(registry.get(c, {}).get("type", ""))}</FONT></TD></TR>'
                for c in sorted(sub_components[sub])
            )
            lbl = ('<<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4">'
                   f'<TR><TD BGCOLOR="#1F4E79"><FONT COLOR="white" POINT-SIZE="12">'
                   f'<B>{n}. {html.escape(str(sub).replace("_", " "))}</B></FONT></TD></TR>'
                   + comp_rows + "</TABLE>>")
            lines.append(f"  {_safe_id(sub)} [label={lbl}];")

        lines.append("")
        # Bridging components: appear in more than one subsystem
        drawn = set()
        for i, a in enumerate(subs):
            for b in subs[i + 1:]:
                shared = sub_components[a] & sub_components[b]
                if not shared:
                    continue
                # signal classes carried by the shared components in either subsystem
                mask = (df_conn["Subsystem_Name"].astype(str).isin([a, b])
                        & (df_conn["Component_ID"].astype(str).isin(shared)
                           | df_conn["Target_ID"].astype(str).isin(shared)))
                classes = sorted({classify_signal(s) for s in df_conn[mask]["Signal_Name"]})
                color = SIGNAL_CLASSES[classes[0]]["color"] if len(classes) == 1 else "#333333"
                label = "via " + ", ".join(sorted(shared))
                key = (a, b)
                if key not in drawn:
                    drawn.add(key)
                    lines.append(
                        f'  {_safe_id(a)} -> {_safe_id(b)} [dir=both, color="{color}", '
                        f'penwidth=1.6, xlabel=<<FONT POINT-SIZE="9">{html.escape(label)}</FONT>>];'
                    )

        lines.append("}")
        return DiagramLeadAgent._render("\n".join(lines), "System_Overview", output_dir)

    # ── Rendering ─────────────────────────────────────────────────────────
    @staticmethod
    def _render(dot_code: str, name: str, output_dir: str) -> Optional[str]:
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{name}_diagram")
        try:
            src = graphviz.Source(dot_code)
            src.render(out_path, format="png", cleanup=True)
            try:
                src.render(out_path, format="svg", cleanup=True)
            except Exception:
                pass  # SVG is a bonus; PNG is what the documents embed
            print(f"  ✓ [Diagram] Rendered: {out_path}.png")
            return f"{out_path}.png"
        except Exception as e:
            print(f"  ⚠ [Diagram] Render failed: {e}")
            print("     Install Graphviz binaries: https://graphviz.org/download/")
            with open(f"{out_path}.dot", "w", encoding="utf-8") as f:
                f.write(dot_code)
            return None
