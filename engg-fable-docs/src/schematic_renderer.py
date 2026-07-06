"""src/schematic_renderer.py — CAD-style schematic sheets with IEC 60617 symbols.

Replaces Graphviz for subsystem wiring sheets (review feedback):
- Discrete parts are drawn with IEC 60617 symbols: resistor = rectangle,
  capacitor = parallel plates, diode = triangle + bar, fuse = rectangle with
  through-line, motor = circle with M, connector = pin strip with contacts.
- ICs are drawn as manufacturer-style schematic symbols: body rectangle with
  pin stubs, pin names inside the body, reference designator above, part
  number below — not as block-diagram tables.
- Wires are routed strictly orthogonally (Manhattan) by a channel router:
  components sit in signal-flow columns, each wire escapes its pin
  horizontally into the adjacent routing channel, runs on a dedicated
  vertical track, and enters the target pin horizontally. No diagonals,
  no wire ever crosses a component body.
- Pure Pillow: subsystem sheets no longer need the Graphviz binary at all.
  PNG (for the documents) + SVG (scalable) are both emitted.
"""
import math
import os
import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from src.component_registry import SIGNAL_CLASSES, classify_signal
from src.config import DOC_NUMBER, DOC_VERSION

# ── Geometry constants (in sheet units; rendered at SCALE px/unit) ────────
SCALE = 2
PIN_PITCH = 26          # vertical spacing between IC pins
STUB = 22               # length of a pin stub outside the symbol body
CHANNEL_W = 130         # routing channel between component columns
TRACK_PITCH = 12        # spacing between vertical routing tracks
MARGIN = 60
TOP_MARGIN = 70
COL_GAP_MIN = 40        # vertical gap between stacked components
BOX_PAD = 10            # clearance around symbol bodies for collision checks

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in (_FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size * SCALE)
            except Exception:
                continue
    return ImageFont.load_default()


def _svg_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


class Sheet:
    """Dual-target drawing surface: PIL image (PNG) + SVG element list."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.img = Image.new("RGB", (w * SCALE, h * SCALE), "white")
        self.draw = ImageDraw.Draw(self.img)
        self.svg: List[str] = []
        self._fonts: Dict[Tuple[int, bool], ImageFont.FreeTypeFont] = {}

    def _font(self, size: int, bold: bool) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self._fonts:
            self._fonts[key] = _find_font(size, bold)
        return self._fonts[key]

    # segments: list of (x, y) points; orthogonal polyline
    def polyline(self, pts, color: str, width: int = 2, dashed: bool = False):
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            self.line(x1, y1, x2, y2, color, width, dashed)

    def line(self, x1, y1, x2, y2, color: str, width: int = 2, dashed: bool = False):
        if dashed:
            seg, gap, pos = 7.0, 5.0, 0.0
            length = math.hypot(x2 - x1, y2 - y1)
            if length == 0:
                return
            ux, uy = (x2 - x1) / length, (y2 - y1) / length
            while pos < length:
                end = min(pos + seg, length)
                self.draw.line([(x1 + ux * pos) * SCALE, (y1 + uy * pos) * SCALE,
                                (x1 + ux * end) * SCALE, (y1 + uy * end) * SCALE],
                               fill=color, width=width * SCALE)
                pos = end + gap
            self.svg.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                f'stroke-width="{width}" stroke-dasharray="7,5"/>')
        else:
            self.draw.line([x1 * SCALE, y1 * SCALE, x2 * SCALE, y2 * SCALE],
                           fill=color, width=width * SCALE)
            self.svg.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                f'stroke-width="{width}"/>')

    def rect(self, x, y, w, h, outline: str = "black", width: int = 2,
             fill: Optional[str] = None):
        self.draw.rectangle([x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE],
                            outline=outline, width=width * SCALE, fill=fill)
        f = fill or "none"
        self.svg.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{f}" '
            f'stroke="{outline}" stroke-width="{width}"/>')

    def circle(self, cx, cy, r, outline: str = "black", width: int = 2,
               fill: Optional[str] = None):
        self.draw.ellipse([(cx - r) * SCALE, (cy - r) * SCALE,
                           (cx + r) * SCALE, (cy + r) * SCALE],
                          outline=outline, width=width * SCALE, fill=fill)
        f = fill or "none"
        self.svg.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{f}" '
                        f'stroke="{outline}" stroke-width="{width}"/>')

    def polygon(self, pts, fill: str = "black", outline: Optional[str] = None):
        self.draw.polygon([(x * SCALE, y * SCALE) for x, y in pts],
                          fill=fill, outline=outline or fill)
        p = " ".join(f"{x},{y}" for x, y in pts)
        self.svg.append(f'<polygon points="{p}" fill="{fill}" '
                        f'stroke="{outline or fill}"/>')

    def text(self, x, y, s: str, size: int = 10, color: str = "black",
             bold: bool = False, anchor: str = "mm", bg: Optional[str] = None):
        font = self._font(size, bold)
        if bg:
            bb = self.draw.textbbox((x * SCALE, y * SCALE), s, font=font, anchor=anchor)
            self.draw.rectangle([bb[0] - 2 * SCALE, bb[1] - SCALE,
                                 bb[2] + 2 * SCALE, bb[3] + SCALE], fill=bg)
        self.draw.text((x * SCALE, y * SCALE), s, fill=color, font=font, anchor=anchor)
        ta = {"l": "start", "m": "middle", "r": "end"}[anchor[0]]
        va = {"t": "hanging", "m": "central", "b": "text-after-edge",
              "s": "alphabetic"}[anchor[1]]
        w = (bold and "bold") or "normal"
        self.svg.append(
            f'<text x="{x}" y="{y}" font-size="{size}" font-family="Helvetica,Arial,sans-serif" '
            f'font-weight="{w}" fill="{color}" text-anchor="{ta}" '
            f'dominant-baseline="{va}">{_svg_escape(s)}</text>')

    def text_w(self, s: str, size: int = 10, bold: bool = False) -> float:
        bb = self.draw.textbbox((0, 0), s, font=self._font(size, bold))
        return (bb[2] - bb[0]) / SCALE

    def save(self, png_path: str, svg_path: str):
        self.img.save(png_path)
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
                    f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">\n'
                    '<rect width="100%" height="100%" fill="white"/>\n'
                    + "\n".join(self.svg) + "\n</svg>\n")


# ── Symbols (IEC 60617) ───────────────────────────────────────────────────
class Symbol:
    """Base: placed at top-left (x, y); pins have fixed stub-end positions."""

    def __init__(self, cid: str, info: dict, left_pins: List[str],
                 right_pins: List[str]):
        self.cid = cid
        self.info = info
        self.left_pins = left_pins
        self.right_pins = right_pins
        self.x = self.y = 0.0
        self.w, self.h = self._size()

    def _size(self) -> Tuple[float, float]:
        raise NotImplementedError

    def place(self, x: float, y: float):
        self.x, self.y = x, y

    def bbox(self) -> Tuple[float, float, float, float]:
        return (self.x - BOX_PAD, self.y - 16 - BOX_PAD,
                self.x + self.w + BOX_PAD, self.y + self.h + 24 + BOX_PAD)

    def pin_pos(self, pin: str) -> Tuple[float, float, int]:
        """Return (x, y, facing) of the stub end. facing: -1 = left, +1 = right."""
        raise NotImplementedError

    def draw(self, sh: Sheet):
        raise NotImplementedError

    def _draw_labels(self, sh: Sheet, above_y: float, below_y: float,
                     cx: float) -> float:
        """Refdes above the symbol, make/model on separate short lines below
        (long one-line part numbers would spill into the routing channels).
        Returns the y below the last line written."""
        sh.text(cx, above_y, self.cid, size=12, bold=True, anchor="mb")
        y = below_y
        for line in (self.info.get("make") or "", self.info.get("model") or ""):
            if line:
                sh.text(cx, y, line, size=8, color="#333333", anchor="mt",
                        bg="white")
                y += 12
        return y


class ICSymbol(Symbol):
    """Manufacturer-style IC symbol: body with pin stubs, names inside."""
    BODY_W = 120

    def _size(self):
        rows = max(len(self.left_pins), len(self.right_pins), 1)
        body_h = rows * PIN_PITCH + 14
        return self.BODY_W + 2 * STUB, body_h

    def _pin_y(self, idx: int) -> float:
        return self.y + 14 + idx * PIN_PITCH + PIN_PITCH / 2 - 7

    def pin_pos(self, pin):
        if pin in self.left_pins:
            return (self.x, self._pin_y(self.left_pins.index(pin)), -1)
        return (self.x + self.w, self._pin_y(self.right_pins.index(pin)), +1)

    def draw(self, sh: Sheet):
        bx = self.x + STUB
        sh.rect(bx, self.y, self.BODY_W, self.h, width=2)
        for i, p in enumerate(self.left_pins):
            py = self._pin_y(i)
            sh.line(self.x, py, bx, py, "black", 2)
            sh.text(bx + 5, py, p, size=9, anchor="lm")
        for i, p in enumerate(self.right_pins):
            py = self._pin_y(i)
            sh.line(bx + self.BODY_W, py, self.x + self.w, py, "black", 2)
            sh.text(bx + self.BODY_W - 5, py, p, size=9, anchor="rm")
        next_y = self._draw_labels(sh, self.y - 4, self.y + self.h + 3,
                                   bx + self.BODY_W / 2)
        ctype = self.info.get("type") or ""
        if ctype:
            sh.text(bx + self.BODY_W / 2, next_y, ctype,
                    size=8, color="#666666", anchor="mt")


class TwoTerminalSymbol(Symbol):
    """Horizontal two-lead symbol; subclasses draw the IEC body."""
    BODY_W = 44
    BODY_H = 16
    LEAD = 26

    def __init__(self, cid, info, left_pins, right_pins):
        # exactly one pin per side; if the flow analysis put both pins on the
        # same side, restore the symbol's natural left/right order
        pins = left_pins + right_pins
        lp, rp = self._natural_sides(pins, left_pins, right_pins)
        super().__init__(cid, info, lp, rp)

    def _natural_sides(self, pins, left_pins, right_pins):
        if len(left_pins) == 1 and len(right_pins) == 1:
            return left_pins, right_pins
        if len(pins) >= 2:
            return [pins[0]], [pins[1]]
        # single used pin: keep the flow-assigned side so the wire leaves
        # toward its peer instead of wrapping around the sheet
        return left_pins, right_pins

    def _size(self):
        return self.BODY_W + 2 * self.LEAD, max(self.BODY_H, 18)

    def _cy(self) -> float:
        return self.y + self.h / 2

    def pin_pos(self, pin):
        if pin in self.left_pins:
            return (self.x, self._cy(), -1)
        return (self.x + self.w, self._cy(), +1)

    def draw(self, sh: Sheet):
        cy = self._cy()
        bx = self.x + self.LEAD
        if self.left_pins:
            sh.line(self.x, cy, bx, cy, "black", 2)
        if self.right_pins:
            sh.line(bx + self.BODY_W, cy, self.x + self.w, cy, "black", 2)
        self._draw_body(sh, bx, cy)
        self._draw_labels(sh, self.y - 2, self.y + self.h + 4,
                          bx + self.BODY_W / 2)

    def _draw_body(self, sh: Sheet, bx: float, cy: float):
        raise NotImplementedError


class ResistorSymbol(TwoTerminalSymbol):
    """IEC 60617: resistor = plain rectangle."""

    def _draw_body(self, sh, bx, cy):
        sh.rect(bx, cy - self.BODY_H / 2, self.BODY_W, self.BODY_H, width=2)


class CapacitorSymbol(TwoTerminalSymbol):
    """IEC 60617: two parallel plates."""
    BODY_W = 14
    BODY_H = 30

    def _natural_sides(self, pins, left_pins, right_pins):
        # polarity-aware: POS/+ on the side power arrives from (keep flow
        # sides when unambiguous, else POS left)
        if len(left_pins) == 1 and len(right_pins) == 1:
            return left_pins, right_pins
        if len(pins) >= 2:
            pos_first = [p for p in pins if "POS" in p.upper() or "+" in p]
            if pos_first:
                other = [p for p in pins if p not in pos_first]
                return [pos_first[0]], [other[0]] if other else []
            return [pins[0]], [pins[1]]
        return left_pins, right_pins  # single pin: keep flow side

    def _draw_body(self, sh, bx, cy):
        gap = 6
        x1 = bx + self.BODY_W / 2 - gap / 2
        x2 = bx + self.BODY_W / 2 + gap / 2
        sh.line(bx, cy, x1, cy, "black", 2)
        sh.line(x2, cy, bx + self.BODY_W, cy, "black", 2)
        sh.line(x1, cy - self.BODY_H / 2, x1, cy + self.BODY_H / 2, "black", 3)
        sh.line(x2, cy - self.BODY_H / 2, x2, cy + self.BODY_H / 2, "black", 3)


class DiodeSymbol(TwoTerminalSymbol):
    """IEC 60617: triangle (anode) + bar (cathode); anode left."""
    BODY_W = 26
    BODY_H = 22

    def _natural_sides(self, pins, left_pins, right_pins):
        anode = [p for p in pins if "ANOD" in p.upper() or p.upper() == "A"]
        cathode = [p for p in pins if "CATH" in p.upper() or p.upper() in ("K", "C")]
        if anode or cathode:
            return anode[:1], cathode[:1]
        return super()._natural_sides(pins, left_pins, right_pins)

    def _size(self):
        # a diode may appear with only one of its pins used on a sheet;
        # keep full symbol width regardless
        return self.BODY_W + 2 * self.LEAD, max(self.BODY_H, 18)

    def _draw_body(self, sh, bx, cy):
        h = self.BODY_H / 2
        tip = bx + self.BODY_W - 6
        sh.polygon([(bx, cy - h), (bx, cy + h), (tip, cy)], fill="black")
        sh.line(tip, cy - h, tip, cy + h, "black", 3)
        sh.line(tip, cy, bx + self.BODY_W, cy, "black", 2)


class FuseSymbol(TwoTerminalSymbol):
    """IEC 60617: rectangle with a line through it."""

    def _draw_body(self, sh, bx, cy):
        sh.rect(bx, cy - self.BODY_H / 2, self.BODY_W, self.BODY_H, width=2)
        sh.line(bx, cy, bx + self.BODY_W, cy, "black", 2)


class InductorSymbol(TwoTerminalSymbol):
    """IEC 60617 (simplified): solid rectangle."""

    def _draw_body(self, sh, bx, cy):
        sh.rect(bx, cy - 6, self.BODY_W, 12, width=1, fill="black")


class ConnectorSymbol(Symbol):
    """Pin strip: body with contact circles + stubs on the wiring side."""
    BODY_W = 58

    def _size(self):
        pins = self.left_pins + self.right_pins
        return self.BODY_W + STUB, max(len(pins), 1) * PIN_PITCH + 12

    def _side(self) -> int:
        return -1 if self.left_pins else +1

    def _pins(self) -> List[str]:
        return self.left_pins or self.right_pins

    def _pin_y(self, idx: int) -> float:
        return self.y + 12 + idx * PIN_PITCH + PIN_PITCH / 2 - 6

    def pin_pos(self, pin):
        py = self._pin_y(self._pins().index(pin))
        if self._side() < 0:
            return (self.x, py, -1)
        return (self.x + self.w, py, +1)

    def draw(self, sh: Sheet):
        side = self._side()
        bx = self.x + (STUB if side < 0 else 0)
        sh.rect(bx, self.y, self.BODY_W, self.h, width=2)
        edge = bx if side < 0 else bx + self.BODY_W
        for i, p in enumerate(self._pins()):
            py = self._pin_y(i)
            stub_out = edge + side * STUB
            sh.line(edge, py, stub_out, py, "black", 2)
            sh.circle(edge, py, 3, width=2, fill="white")
            tx = bx + self.BODY_W / 2
            sh.text(tx, py, p, size=9, anchor="mm")
        self._draw_labels(sh, self.y - 4, self.y + self.h + 3,
                          bx + self.BODY_W / 2)


class MotorSymbol(Symbol):
    """IEC 60617: circle with M; phase leads on the wiring side."""
    R = 30

    def _size(self):
        return 2 * self.R + STUB, 2 * self.R

    def _pins(self) -> List[str]:
        return self.left_pins or self.right_pins

    def _side(self) -> int:
        return -1 if self.left_pins else +1

    def pin_pos(self, pin):
        pins = self._pins()
        n = len(pins)
        idx = pins.index(pin)
        dy = (idx - (n - 1) / 2) * 18
        if self._side() < 0:
            return (self.x, self.y + self.R + dy, -1)
        return (self.x + self.w, self.y + self.R + dy, +1)

    def draw(self, sh: Sheet):
        side = self._side()
        cx = self.x + (STUB + self.R if side < 0 else self.R)
        cy = self.y + self.R
        sh.circle(cx, cy, self.R, width=2)
        sh.text(cx, cy, "M", size=16, bold=True, anchor="mm")
        sh.text(cx, cy + 12, "3~", size=9, anchor="mm")
        pins = self._pins()
        for p in pins:
            px, py, f = self.pin_pos(p)
            dy = py - cy
            edge_x = cx - side * -1 * 0  # placeholder to keep math clear
            r_off = math.sqrt(max(self.R ** 2 - dy ** 2, 1))
            edge_x = cx + side * r_off
            sh.line(edge_x, py, px, py, "black", 2)
            sh.text(edge_x - side * 8, py, p, size=9,
                    anchor="lm" if side > 0 else "rm")
        self._draw_labels(sh, self.y - 4, self.y + self.h + 3, cx)


class RailSymbol(Symbol):
    """Supply rail flag: bar for power, IEC earth for ground-like rails."""
    W = 46
    H = 34

    def _size(self):
        return self.W + STUB, self.H

    def _pins(self):
        return self.left_pins or self.right_pins or ["NET"]

    def _side(self):
        return -1 if self.left_pins else +1

    def pin_pos(self, pin):
        cy = self.y + self.H / 2
        if self._side() < 0:
            return (self.x, cy, -1)
        return (self.x + self.w, cy, +1)

    def draw(self, sh: Sheet):
        side = self._side()
        cx = self.x + (STUB + self.W / 2 if side < 0 else self.W / 2)
        cy = self.y + self.H / 2
        stub_x = cx
        is_gnd = bool(re.match(r"^(GND|AGND|DGND|0V)", self.cid.upper()))
        # horizontal lead from pin to the flag stem
        px, py, _ = self.pin_pos(self._pins()[0])
        sh.line(px, py, stub_x, cy, "black", 2)
        if is_gnd:
            sh.line(stub_x, cy, stub_x, cy + 8, "black", 2)
            for i, wdt in enumerate((16, 10, 4)):
                yy = cy + 8 + i * 4
                sh.line(stub_x - wdt / 2, yy, stub_x + wdt / 2, yy, "black", 2)
            sh.text(cx, cy - 8, self.cid, size=10, bold=True, anchor="mb")
        else:
            sh.line(stub_x, cy, stub_x, cy - 10, "black", 2)
            sh.line(stub_x - 12, cy - 10, stub_x + 12, cy - 10, "black", 3)
            sh.text(cx, cy - 14, self.cid, size=10, bold=True, anchor="mb")


_SYMBOL_BY_PREFIX = {
    "R": ResistorSymbol,
    "C": CapacitorSymbol,
    "D": DiodeSymbol,
    "L": InductorSymbol,
    "F": FuseSymbol,
    "J": ConnectorSymbol,
    "X": ConnectorSymbol,
    "M": MotorSymbol,
}


# ── Layout + routing ──────────────────────────────────────────────────────
def _component_ranks(edges: List[dict], nodes: List[str]) -> Dict[str, int]:
    """Longest-path rank following the majority flow direction per pair
    (feedback edges do not pull a component backwards)."""
    pair_count: Dict[Tuple[str, str], int] = {}
    for e in edges:
        pair_count[(e["src"], e["tgt"])] = pair_count.get((e["src"], e["tgt"]), 0) + 1
    fwd: Dict[str, set] = {n: set() for n in nodes}
    for (a, b), n in pair_count.items():
        if n >= pair_count.get((b, a), 0) and a != b:
            fwd[a].add(b)
    rank = {n: 0 for n in nodes}
    for _ in range(len(nodes)):  # relaxation, cycle-safe
        changed = False
        for a in nodes:
            for b in fwd[a]:
                if rank[b] < rank[a] + 1:
                    rank[b] = rank[a] + 1
                    changed = True
        if not changed:
            break
    return rank


class _TrackAlloc:
    """Greedy interval-based allocator for parallel routing tracks."""

    def __init__(self, origin: float, pitch: float, direction: int = 1):
        self.origin = origin
        self.pitch = pitch
        self.direction = direction
        self.tracks: List[List[Tuple[float, float]]] = []

    def alloc(self, lo: float, hi: float) -> float:
        lo, hi = min(lo, hi) - 4, max(lo, hi) + 4
        for i, intervals in enumerate(self.tracks):
            if all(hi < a or lo > b for a, b in intervals):
                intervals.append((lo, hi))
                return self.origin + self.direction * i * self.pitch
        self.tracks.append([(lo, hi)])
        return self.origin + self.direction * (len(self.tracks) - 1) * self.pitch


def render_schematic(subsystem: str, connections, registry: Dict[str, dict],
                     output_dir: str) -> Optional[str]:
    """Render one subsystem wiring sheet. Returns the PNG path."""
    edges = []
    for _, r in connections.iterrows():
        edges.append({
            "src": str(r["Component_ID"]), "sp": str(r["Source_Pin"]),
            "tgt": str(r["Target_ID"]), "tp": str(r["Target_Pin"]),
            "sig": str(r["Signal_Name"]),
        })
    nodes = sorted({e["src"] for e in edges} | {e["tgt"] for e in edges})
    rank = _component_ranks(edges, nodes)

    # ── pin side votes: each edge pulls its pin toward the peer's column ──
    side_votes: Dict[Tuple[str, str], List[int]] = {}
    drives: Dict[Tuple[str, str], bool] = {}
    for e in edges:
        dr = rank[e["tgt"]] - rank[e["src"]]
        side_votes.setdefault((e["src"], e["sp"]), []).append(+1 if dr >= 0 else -1)
        side_votes.setdefault((e["tgt"], e["tp"]), []).append(-1 if dr >= 0 else +1)
        drives[(e["src"], e["sp"])] = True

    pin_side: Dict[Tuple[str, str], int] = {}
    for key, votes in side_votes.items():
        s = sum(votes)
        if s == 0:
            s = +1 if drives.get(key) else -1
        pin_side[key] = +1 if s > 0 else -1

    # ── build symbols ──
    pins_of: Dict[str, List[str]] = {n: [] for n in nodes}
    for e in edges:
        if e["sp"] not in pins_of[e["src"]]:
            pins_of[e["src"]].append(e["sp"])
        if e["tp"] not in pins_of[e["tgt"]]:
            pins_of[e["tgt"]].append(e["tp"])

    symbols: Dict[str, Symbol] = {}
    for n in nodes:
        info = registry.get(n, {"prefix": "U", "type": "", "make": "", "model": ""})
        lp = [p for p in pins_of[n] if pin_side[(n, p)] < 0]
        rp = [p for p in pins_of[n] if pin_side[(n, p)] > 0]
        if info.get("is_rail"):
            cls = RailSymbol
        else:
            cls = _SYMBOL_BY_PREFIX.get(info.get("prefix", "U"), ICSymbol)
        symbols[n] = cls(n, info, lp, rp)

    # ── column layout ──
    ncols = max(rank.values()) + 1
    cols: List[List[str]] = [[] for _ in range(ncols)]
    for n in nodes:
        cols[rank[n]].append(n)

    col_w = [max((symbols[n].w for n in col), default=80) for col in cols]
    col_x: List[float] = []
    x = MARGIN + CHANNEL_W / 2
    for i in range(ncols):
        col_x.append(x)
        x += col_w[i] + CHANNEL_W

    # order within column by barycenter of already-placed neighbours
    placed_y: Dict[str, float] = {}
    for ci in range(ncols):
        def _bary(n: str) -> float:
            ys = [placed_y[e["src"]] for e in edges
                  if e["tgt"] == n and e["src"] in placed_y]
            ys += [placed_y[e["tgt"]] for e in edges
                   if e["src"] == n and e["tgt"] in placed_y]
            return sum(ys) / len(ys) if ys else 0.0
        cols[ci].sort(key=_bary)
        y = TOP_MARGIN
        for n in cols[ci]:
            sym = symbols[n]
            # center smaller symbols within the column width
            sym.place(col_x[ci] + (col_w[ci] - sym.w) / 2, y)
            placed_y[n] = y + sym.h / 2
            y += sym.h + COL_GAP_MIN + 24

    body_bottom = max((s.y + s.h for s in symbols.values()), default=200)

    # ── routing ──
    # channel c sits to the LEFT of column c (c in 0..ncols)
    def channel_center(c: int) -> float:
        if c == 0:
            return MARGIN + CHANNEL_W / 4
        if c >= ncols:
            return col_x[ncols - 1] + col_w[ncols - 1] + CHANNEL_W / 2
        return col_x[c] - CHANNEL_W / 2

    channels = {c: _TrackAlloc(channel_center(c) - (CHANNEL_W / 2 - 18),
                               TRACK_PITCH) for c in range(ncols + 1)}
    corridor = _TrackAlloc(body_bottom + 46, TRACK_PITCH)

    boxes = [symbols[n].bbox() for n in nodes]

    def _clear_run(y: float, c_from: int, c_to: int) -> bool:
        """Is a horizontal run at y clear of all symbol bodies between
        channels c_from and c_to (exclusive columns c_from..c_to-1)?"""
        for ci in range(min(c_from, c_to), max(c_from, c_to)):
            for n in cols[ci]:
                x0, y0, x1, y1 = symbols[n].bbox()
                if y0 <= y <= y1:
                    return False
        return True

    # route order: short spans first for tidier track nesting
    def _span(e):
        _, y1, _ = symbols[e["src"]].pin_pos(e["sp"])
        _, y2, _ = symbols[e["tgt"]].pin_pos(e["tp"])
        return abs(y2 - y1)

    routes = []
    for e in sorted(edges, key=_span):
        x1, y1, f1 = symbols[e["src"]].pin_pos(e["sp"])
        x2, y2, f2 = symbols[e["tgt"]].pin_pos(e["tp"])
        c1 = rank[e["src"]] + (1 if f1 > 0 else 0)
        c2 = rank[e["tgt"]] + (1 if f2 > 0 else 0)
        if c1 == c2:
            t = channels[c1].alloc(y1, y2)
            pts = [(x1, y1), (t, y1), (t, y2), (x2, y2)]
        elif _clear_run(y2, c1, c2):
            t = channels[c1].alloc(y1, y2)
            pts = [(x1, y1), (t, y1), (t, y2), (x2, y2)]
        elif _clear_run(y1, c1, c2):
            t = channels[c2].alloc(y1, y2)
            pts = [(x1, y1), (t, y1), (t, y2), (x2, y2)]
        else:
            yc = corridor.alloc(channel_center(c1), channel_center(c2))
            t1 = channels[c1].alloc(y1, yc)
            t2 = channels[c2].alloc(yc, y2)
            pts = [(x1, y1), (t1, y1), (t1, yc), (t2, yc), (t2, y2), (x2, y2)]
        routes.append((e, pts, f2))

    corridor_bottom = (corridor.origin
                       + len(corridor.tracks) * TRACK_PITCH + 20)

    # ── sheet size ──
    classes_used = sorted({classify_signal(e["sig"]) for e in edges})
    legend_h = 20 + len(classes_used) * 18
    title_h = 54
    sheet_w = int(x - CHANNEL_W + MARGIN + CHANNEL_W / 2)
    sheet_h = int(max(corridor_bottom, body_bottom + 60) + legend_h + title_h + 50)
    sh = Sheet(max(sheet_w, 640), sheet_h)

    # wires first, symbols on top
    fanout: Dict[Tuple[str, str], int] = {}
    for e in edges:
        fanout[(e["src"], e["sp"])] = fanout.get((e["src"], e["sp"]), 0) + 1

    for e, pts, f2 in routes:
        cls = classify_signal(e["sig"])
        color = SIGNAL_CLASSES[cls]["color"]
        width = 3 if cls in ("power", "motor") else 2
        dashed = cls == "ground"
        sh.polyline(pts, color, width, dashed)
        # arrowhead into the target pin (points opposite the stub facing)
        ax, ay = pts[-1]
        d = -f2
        sh.polygon([(ax, ay), (ax - d * 8, ay - 4), (ax - d * 8, ay + 4)],
                   fill=color)
        # junction dot at fan-out source pins
        if fanout[(e["src"], e["sp"])] > 1:
            sh.circle(pts[0][0], pts[0][1], 2.5, fill="black", outline="black")

    # signal labels: pick a spot on the wire that doesn't collide with
    # labels already placed (two nets often share a horizontal corridor)
    placed_labels: List[Tuple[float, float, float, float]] = []

    def _label_bbox(cx, cy, tw, anchor):
        if anchor == "mm":
            return (cx - tw / 2 - 3, cy - 8, cx + tw / 2 + 3, cy + 8)
        return (cx - 3, cy - 8, cx + tw + 3, cy + 8)

    def _collides(bb):
        return any(not (bb[2] < o[0] or bb[0] > o[2]
                        or bb[3] < o[1] or bb[1] > o[3])
                   for o in placed_labels)

    for e, pts, _ in routes:
        cls = classify_signal(e["sig"])
        color = SIGNAL_CLASSES[cls]["color"]
        tw = sh.text_w(e["sig"], 9)
        candidates = []
        hsegs = sorted([s for s in zip(pts, pts[1:]) if s[0][1] == s[1][1]],
                       key=lambda s: -abs(s[1][0] - s[0][0]))
        for (hx1, hy), (hx2, _) in hsegs:
            if abs(hx2 - hx1) < tw + 10:
                continue
            for frac in (0.5, 0.3, 0.7):
                candidates.append((hx1 + (hx2 - hx1) * frac, hy - 7, "mm"))
        vsegs = sorted([s for s in zip(pts, pts[1:]) if s[0][0] == s[1][0]],
                       key=lambda s: -abs(s[1][1] - s[0][1]))
        for (vx, vy1), (_, vy2) in vsegs:
            if abs(vy2 - vy1) < 24:
                continue
            for frac in (0.5, 0.35, 0.65):
                candidates.append((vx + 5, vy1 + (vy2 - vy1) * frac, "lm"))
        if hsegs:  # last resort: midpoint of the longest horizontal
            (hx1, hy), (hx2, _) = hsegs[0]
            candidates.append(((hx1 + hx2) / 2, hy - 7, "mm"))
        for cx, cy, anchor in candidates:
            bb = _label_bbox(cx, cy, tw, anchor)
            if not _collides(bb):
                placed_labels.append(bb)
                sh.text(cx, cy, e["sig"], size=9, color=color,
                        anchor=anchor, bg="white")
                break
        else:
            if candidates:
                cx, cy, anchor = candidates[0]
                placed_labels.append(_label_bbox(cx, cy, tw, anchor))
                sh.text(cx, cy, e["sig"], size=9, color=color,
                        anchor=anchor, bg="white")

    for n in nodes:
        symbols[n].draw(sh)

    # ── legend ──
    ly = sheet_h - title_h - legend_h - 16
    lw = 170
    sh.rect(MARGIN, ly, lw, legend_h, width=1)
    sh.text(MARGIN + lw / 2, ly + 10, "Legend", size=10, bold=True, anchor="mm")
    for i, c in enumerate(classes_used):
        yy = ly + 24 + i * 18
        sh.rect(MARGIN + 8, yy - 6, 16, 12, width=1,
                fill=SIGNAL_CLASSES[c]["color"])
        sh.text(MARGIN + 30, yy, SIGNAL_CLASSES[c]["label"], size=9, anchor="lm")

    # ── title block ──
    tb_w = min(560, sheet_w - 2 * MARGIN)
    tb_x = (max(sheet_w, 640) - tb_w) / 2
    tb_y = sheet_h - title_h - 8
    sh.rect(tb_x, tb_y, tb_w, 26, width=2)
    sh.text(tb_x + tb_w / 2, tb_y + 13,
            subsystem.replace("_", " ") + " — Wiring Diagram",
            size=13, bold=True, anchor="mm")
    cells = [f"Doc: {DOC_NUMBER}", f"Rev: {DOC_VERSION}",
             f"Date: {date.today().isoformat()}", f"Sheet: {subsystem}"]
    cw = tb_w / len(cells)
    for i, ctext in enumerate(cells):
        sh.rect(tb_x + i * cw, tb_y + 26, cw, 22, width=2)
        sh.text(tb_x + i * cw + cw / 2, tb_y + 37, ctext, size=10, anchor="mm")

    os.makedirs(output_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_]", "_", subsystem)
    png = os.path.join(output_dir, f"{safe}_diagram.png")
    svg = os.path.join(output_dir, f"{safe}_diagram.svg")
    sh.save(png, svg)
    print(f"  ✓ [Schematic] Rendered: {png}")
    return png
