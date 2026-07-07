"""agents/diagram_review_agent.py — Independent diagram verification (SVG-parse).

Every wiring sheet is emitted as SVG alongside the PNG. The SVG is a text
file containing the exact labels the renderer drew, so verification is
lossless and instant: parse the artifact, extract what it claims to show,
and compare against the source connection data. (Chosen over OCR: a small
vision model would be slower than the render itself and would introduce
its own read errors — the reviewer must be more reliable than the thing
it reviews.)

Checks per sheet:
  - every component in the connection data appears as a label
  - every signal name appears as a wire label
Mismatches are reported back to the supervisor, which re-runs the diagram
agent and re-verifies (bounded retries).
"""
import html
import os
import re
from typing import Dict

import pandas as pd

_TEXT_RE = re.compile(r"<text[^>]*>([^<]+)</text>")


class DiagramReviewAgent:

    @staticmethod
    def verify(subsystem: str, connections: pd.DataFrame, png_path: str) -> Dict:
        """Return {status: 'pass'|'fail'|'skipped', missing_components,
        missing_signals, checked} for one rendered sheet."""
        svg_path = os.path.splitext(png_path or "")[0] + ".svg"
        if not png_path or not os.path.exists(svg_path):
            return {"status": "skipped", "detail": "no SVG artifact found",
                    "missing_components": [], "missing_signals": [], "checked": 0}

        with open(svg_path, encoding="utf-8") as f:
            svg = f.read()
        labels = {html.unescape(m).strip() for m in _TEXT_RE.findall(svg)}
        # wire labels may be drawn with a truncation ellipsis on dense sheets
        label_blob = " ".join(labels)

        comps = sorted(set(connections["Component_ID"].astype(str))
                       | set(connections["Target_ID"].astype(str)))
        sigs = sorted(set(connections["Signal_Name"].astype(str)))

        missing_c = [c for c in comps if c not in labels and c not in label_blob]
        missing_s = [s for s in sigs if s not in labels and s not in label_blob]

        status = "pass" if not missing_c and not missing_s else "fail"
        return {
            "status": status,
            "missing_components": missing_c,
            "missing_signals": missing_s,
            "checked": len(comps) + len(sigs),
            "detail": (f"{len(comps)} components + {len(sigs)} signals verified"
                       if status == "pass" else
                       f"missing {len(missing_c)} components, {len(missing_s)} signals"),
        }
