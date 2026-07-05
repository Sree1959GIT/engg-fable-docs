"""src/component_registry.py — Shared component intelligence.

Responsibilities:
- Build an accurate {component_id -> make/model/type/refdes} registry from the
  connectivity data + validated BOM. (Fixes the bug where a Target_ID inherited
  the *source* row's Make/Model.)
- IEC 81346-2 reference designator classification.
- IEC 60617-inspired shape selection for diagrams.
- Signal classification per master.md color coding (red=power, blue=data,
  green=control, purple=motor).
- Offline knowledge base of common parts so descriptions stay professional
  even when the LLM is unreachable.
"""
import re
from typing import Dict, Optional

import pandas as pd

# ── IEC 81346-2 class letters ─────────────────────────────────────────────
IEC_81346_CLASSES = {
    "U": "Integrated circuit",
    "R": "Resistor",
    "C": "Capacitor",
    "L": "Inductor",
    "J": "Connector",
    "X": "Terminal",
    "D": "Diode / LED",
    "M": "Motor",
    "K": "Relay / Contactor",
    "F": "Fuse / Protective device",
    "B": "Sensor / Transducer",
    "Q": "Transistor / Power switch",
    "T": "Transformer",
    "S": "Switch",
    "BT": "Battery",
}

_TYPE_TO_PREFIX = {
    "ic": "U", "integrated circuit": "U", "microcontroller": "U", "mcu": "U",
    "controller": "U", "driver": "U", "bms": "U", "regulator": "U",
    "resistor": "R", "capacitor": "C", "inductor": "L",
    "connector": "J", "header": "J", "plug": "J", "terminal": "X",
    "terminal block": "J",
    "diode": "D", "schottky": "D", "led": "D", "rectifier": "D", "tvs": "D",
    "motor": "M", "actuator": "M",
    "relay": "K", "contactor": "K",
    "fuse": "F", "sensor": "B", "transistor": "Q", "mosfet": "Q",
    "transformer": "T", "switch": "S", "battery": "BT",
}

_PREFIX_TO_SHAPE = {
    "J": "cds",           # connector: parallelogram-like flag (rendered via HTML in agent)
    "X": "cds",
    "D": "diamond",
    "M": "circle",
    "B": "diamond",
    "BT": "ellipse",
    "K": "box",
    "F": "hexagon",
    "T": "ellipse",
}

# ── Offline component knowledge base ─────────────────────────────────────
# Keyed by a substring of the model number (case-insensitive). Extend freely.
KNOWN_COMPONENTS = {
    "BQ76952": {
        "type": "Battery Monitor IC",
        "prefix": "U",
        "description": (
            "16-series battery monitor and protector for Li-ion/LiFePO4 packs. "
            "Provides per-cell voltage measurement, coulomb counting, integrated "
            "protection (OV/UV/OCC/OCD/SCD), and a host interface over I2C/SPI. "
            "The ALERT output signals protection faults to the host controller."
        ),
        "interfaces": "I2C (up to 400 kHz), SPI, ALERT interrupt output",
        "package": "48-pin TQFP",
    },
    "STM32G474": {
        "type": "Microcontroller",
        "prefix": "U",
        "description": (
            "Arm Cortex-M4 microcontroller (170 MHz) optimized for digital power "
            "and motor control, with high-resolution timers for PWM generation, "
            "fast 12-bit ADCs, and rich connectivity (SPI, I2C, UART, CAN-FD). "
            "Acts as the central controller: it supervises the battery monitor "
            "over I2C, commands the gate driver over SPI, and generates the "
            "three-phase PWM signals."
        ),
        "interfaces": "SPI master, I2C master, PWM outputs (HRTIM), GPIO interrupts",
        "package": "LQFP-48/64/128",
    },
    "IMC300A": {
        "type": "Motor Controller IC",
        "prefix": "U",
        "description": (
            "Integrated motor control IC combining a motion-control engine with a "
            "three-phase gate driver. Receives PWM commands, drives the power "
            "stage for phases U/V/W, monitors the DC bus, and reports faults "
            "(overcurrent, overtemperature) via a dedicated FAULT output."
        ),
        "interfaces": "SPI slave, PWM inputs, three-phase outputs, FAULT flag",
        "package": "LQFP-40/48",
    },
    "SS34": {
        "type": "Schottky Diode",
        "prefix": "D",
        "description": (
            "3 A / 40 V Schottky barrier rectifier. Used here for reverse-polarity "
            "protection of the 12 V input: low forward drop (~0.5 V) minimizes "
            "loss while blocking reverse-connected supply voltage."
        ),
        "interfaces": "Anode/Cathode",
        "package": "DO-214AB (SMC)",
    },
    "GRM31CR71E106": {
        "type": "Ceramic Capacitor",
        "prefix": "C",
        "description": (
            "10 uF / 25 V X7R multilayer ceramic capacitor. Provides bulk "
            "decoupling and input filtering on the protected supply rail, "
            "smoothing transients before power reaches downstream ICs."
        ),
        "interfaces": "POS/NEG terminals",
        "package": "1206 SMD",
    },
    "MSTBVA": {
        "type": "Connector",
        "prefix": "J",
        "description": (
            "Phoenix Contact MSTBVA 2.5 series PCB header (5.08 mm pitch, "
            "rated 12 A / 320 V). Serves as the main power entry point for the "
            "external 12 V supply and its return."
        ),
        "interfaces": "Screw-terminal mating plug",
        "package": "Through-hole vertical header",
    },
    "RC0402": {
        "type": "Resistor",
        "prefix": "R",
        "description": (
            "Yageo thick-film chip resistor, 0402, 1% tolerance. Used for "
            "pull-up biasing and voltage sensing dividers."
        ),
        "interfaces": "2-terminal",
        "package": "0402 SMD",
    },
    "EC-I 40": {
        "type": "BLDC Motor",
        "prefix": "M",
        "description": (
            "Maxon EC-i 40 brushless DC motor. Three-phase windings (U/V/W) are "
            "driven by the motor controller's power stage; rotor position "
            "feedback enables commutation."
        ),
        "interfaces": "Phase U/V/W power terminals",
        "package": "40 mm frame",
    },
}


def lookup_known(model: str) -> Optional[dict]:
    m = (model or "").upper().replace("-", "").replace(" ", "")
    for key, info in KNOWN_COMPONENTS.items():
        if key.upper().replace("-", "").replace(" ", "") in m:
            return info
    return None


_RAIL_RE = re.compile(r"^(PWR|VCC|VDD|VBUS|GND|AGND|DGND|[+-]?\d+V\d*|3V3|5V)", re.IGNORECASE)


def is_power_rail(component_id: str) -> bool:
    """Net-style IDs used as pseudo-components (e.g. PWR_3V3, GND) are power rails."""
    return bool(_RAIL_RE.match((component_id or "").strip()))


def infer_refdes_prefix(component_id: str, model: str = "", bom_type: str = "") -> str:
    """IEC 81346-2 class letter. Priority: BOM Type > knowledge base > ID pattern."""
    if is_power_rail(component_id):
        return "W"  # IEC 81346: W = transmission/guiding (rails, busbars, cables)
    t = (bom_type or "").lower()
    for key, prefix in _TYPE_TO_PREFIX.items():
        if key in t:
            return prefix
    known = lookup_known(model)
    if known:
        return known["prefix"]
    m = re.match(r"^([A-Z]+)\d", (component_id or "").upper())
    if m:
        letters = m.group(1)
        if letters == "CON":
            return "J"
        if letters in IEC_81346_CLASSES:
            return letters
        if letters[0] in IEC_81346_CLASSES:
            return letters[0]
    return "U"


def shape_for_prefix(prefix: str) -> str:
    return _PREFIX_TO_SHAPE.get(prefix, "box")


# ── Signal classification (master.md §6.1 color coding) ──────────────────
SIGNAL_CLASSES = {
    "power": {"color": "#C00000", "label": "Power"},
    "ground": {"color": "#7F7F7F", "label": "Ground"},
    "data": {"color": "#0050C8", "label": "Data / Communication"},
    "control": {"color": "#00802B", "label": "Control / Status"},
    "motor": {"color": "#800080", "label": "Motor Phase"},
    "analog": {"color": "#B45F06", "label": "Analog / Sense"},
    "other": {"color": "#444444", "label": "Other"},
}


def classify_signal(signal: str) -> str:
    s = (signal or "").lower()
    if any(w in s for w in ("gnd", "ground", "0v", "vss")):
        return "ground"
    if any(w in s for w in ("phase", "_u", "_v", "_w")) and ("phase" in s or s.startswith("out")):
        return "motor"
    if "phase" in s:
        return "motor"
    if any(w in s for w in ("12v", "5v", "3v3", "24v", "48v", "v_bat", "vbat",
                            "vcc", "vdd", "dc_bus", "vin", "_in", "prot", "sys")):
        # signals like 12V_IN, 12V_PROT, V_BAT, 3V3_SYS, DC_BUS
        if any(w in s for w in ("12v", "5v", "3v3", "24v", "48v", "v_bat", "vbat",
                                "vcc", "vdd", "dc_bus", "vin")):
            return "power"
    if any(w in s for w in ("i2c", "spi", "uart", "usart", "can", "miso", "mosi",
                            "sck", "sda", "scl", "clk", "data", "cs", "nss", "tx", "rx")):
        return "data"
    if any(w in s for w in ("sense", "adc", "vref", "current", "temp")):
        return "analog"
    if any(w in s for w in ("pwm", "fault", "alert", "enable", "en_", "reset",
                            "irq", "int", "status", "pullup")):
        return "control"
    return "other"


def signal_color(signal: str) -> str:
    return SIGNAL_CLASSES[classify_signal(signal)]["color"]


# ── Registry construction ─────────────────────────────────────────────────
def build_component_registry(df_conn: pd.DataFrame,
                             df_bom: Optional[pd.DataFrame] = None) -> Dict[str, dict]:
    """Return {component_id: {make, model, type, prefix, refdes, description, interfaces, package}}.

    Make/Model in a connectivity row describes Component_ID only. Target_IDs are
    resolved from rows where they appear as a source, or from the BOM's
    Reference_IDs column — never from the source row's Make/Model.
    """
    registry: Dict[str, dict] = {}

    # Pass 1: authoritative make/model from rows where the ID is the source
    for _, r in df_conn.iterrows():
        cid = str(r["Component_ID"]).strip()
        if cid and cid not in registry:
            registry[cid] = {"make": str(r.get("Make", "") or ""),
                             "model": str(r.get("Model", "") or "")}

    # Pass 2: any target IDs never seen as a source
    for _, r in df_conn.iterrows():
        tid = str(r["Target_ID"]).strip()
        if tid and tid not in registry:
            registry[tid] = {"make": "", "model": ""}

    # Pass 3: BOM overrides/fills via Reference_IDs
    bom_type_by_id: Dict[str, str] = {}
    if df_bom is not None and not df_bom.empty and "Reference_IDs" in df_bom.columns:
        def _clean(v) -> str:
            v = str(v or "").strip()
            return "" if v.upper() in ("TBD", "UNKNOWN", "NAN", "NONE") else v

        for _, r in df_bom.iterrows():
            refs = [x.strip() for x in str(r["Reference_IDs"]).split(",") if x.strip()]
            for ref in refs:
                entry = registry.setdefault(ref, {"make": "", "model": ""})
                if not entry["make"]:
                    entry["make"] = _clean(r.get("Make"))
                if not entry["model"]:
                    entry["model"] = _clean(r.get("Model"))
                bom_type_by_id[ref] = _clean(r.get("Type"))

    # Pass 4: classification + offline knowledge
    for cid, entry in registry.items():
        known = lookup_known(entry["model"]) or {}
        prefix = infer_refdes_prefix(cid, entry["model"], bom_type_by_id.get(cid, ""))
        entry["prefix"] = prefix
        entry["is_rail"] = is_power_rail(cid)
        entry["class_name"] = "Power Rail" if entry["is_rail"] else IEC_81346_CLASSES.get(prefix, "Component")
        entry["type"] = ("Power Rail" if entry["is_rail"]
                         else known.get("type") or bom_type_by_id.get(cid) or entry["class_name"])
        # Keep the user's ID as the refdes if it already follows IEC style,
        # otherwise prepend the class letter for traceability.
        entry["refdes"] = cid if re.match(rf"^{prefix}\d", cid.upper()) else f"{prefix}:{cid}"
        entry["description"] = known.get("description", "")
        entry["interfaces"] = known.get("interfaces", "")
        entry["package"] = known.get("package", "")

    return registry
