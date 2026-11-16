"""PartHive – sensible electrical pin types.

EasyEDA's part data mostly leaves pin types "undefined" (-> KiCad
"unspecified", ERC warnings everywhere) and sometimes gets them wrong (a
resistor with two *inputs*, an RP2040 whose GND is a *signal*). The converter
also turns every EasyEDA "power" pin into a power *input* — a regulator's VOUT
included. "Smart" mode fixes that with KiCad's own conventions:

1. Discrete parts (R, C, L, D, Q, Y, F, FB, J, SW, ...): every pin passive.
2. ICs, by pin name (overriding EasyEDA when it is undefined or contradicts
   the name):
   * ground / supply pins (GND, VSS, VCC, VDD, VIN, VBAT, ...)  -> power in
   * supply outputs (VOUT, VO, VREG_OUT, LDO_OUT)                -> power out
     — only the FIRST pin of a name; repeats (AMS1117's VOUT tab) are
     passive, or ERC would see two power outputs on one net
   * NC / N.C. / DNC                                             -> no connect
   * exposed pads (EP, EPAD, PAD)                                -> passive
   * anything else: EasyEDA's type if it set one, else bidirectional.

Nets fed only through connectors or resistors then need a PWR_FLAG in the
schematic — KiCad's way of saying "this net is powered from outside".

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import re
from collections import Counter

MODE_SMART, MODE_KEEP, MODE_PASSIVE = "smart", "keep", "passive"
MODES = (MODE_SMART, MODE_KEEP, MODE_PASSIVE)

# Reference prefixes whose parts KiCad's own libraries draw with passive pins.
DISCRETE_PREFIXES = {
    "R", "RN", "RV", "VR", "C", "CP", "L", "FB", "D", "LED", "Q", "Y", "X", "F",
    "J", "P", "CN", "CON", "SW", "S", "K", "T", "TP", "BT", "BZ", "LS", "H", "MH",
    "RT", "TH", "NTC", "PTC", "Z", "ZD", "E", "M",
}

# "(^|_)" lets block-prefixed names match too: USB_VDD, ADC_AVDD, VREG_VIN, USB_GND.
_GROUND = re.compile(
    r"(^|_)(A|D|P|S|C|E)?GND(A|D)?\d*$|^GND_\w+$|(^|_)(A|D)?VSS(A|D|IO|Q)?\d*$|"
    r"^VEE\d*$|^V-$")
_SUPPLY_IN = re.compile(
    r"(^|_)(A|D|P|IO)?V(CC|DD)(A|D|IO|Q|CORE)?\d*$|^(A|D|P|IO)?V(CC|DD)_\w+$|"
    r"^IOVDD\d*$|^VIO\d*$|(^|_)VIN\d*$|^VBAT\d*$|^VBUS\d*$|^V\+$|^VS\d*$|^VSUP\d*$|"
    r"^VSYS$|^VM\d*$|^PVIN\d*$|^VPP$|^VCORE$|^VLOGIC$|^VL$|^\+?\d+V\d*$|"
    r"^(VREG|LDO|REG)_?V?IN\d*$")  # regulator inputs (RP2040 VREG_IN); not any *_IN
_SUPPLY_OUT = re.compile(r"(^|_)V_?OUT\d*$|^VO\d*$|^VREG_?OUT$|^LDO_?OUT\d*$")
_NO_CONNECT = re.compile(r"^(NC\d*|N\.C\.?|N/C|DNC)$")
_PAD = re.compile(r"^(EP|EPAD|E\.P\.|PAD|EXP|THERMAL(_?PAD)?)\d*$")


def _aliases(name: str) -> list:
    """'GND/EP' -> ['GND', 'EP']; strips overbar markup like ~{RESET}."""
    clean = re.sub(r"~\{([^}]*)\}", r"\1", name or "").upper().replace(" ", "")
    return [a for a in re.split(r"[/|,]", clean) if a]


def prefix_of(reference_prefix: str) -> str:
    """'R?' / 'U1' -> 'R' / 'U'."""
    return re.sub(r"[^A-Za-z]", "", reference_prefix or "").upper()


def classify(name: str) -> str | None:
    """KiCad pin type a pin NAME implies, or None (a signal pin)."""
    # search(), not match(): every alternative is anchored (^ or "_" ... $), and
    # the "(^|_)" ones must also find block-prefixed names like USB_VDD.
    aliases = _aliases(name)
    if any(_NO_CONNECT.search(a) for a in aliases):
        return "no_connect"
    if any(_SUPPLY_OUT.search(a) for a in aliases):
        return "power_out"
    if any(_GROUND.search(a) or _SUPPLY_IN.search(a) for a in aliases):
        return "power_in"
    if any(_PAD.search(a) for a in aliases):
        return "passive"
    return None


def apply(ki_symbol, mode: str, kinds) -> Counter:
    """Set the pin types of a converter KiSymbol in place.

    ``kinds`` is the converter's KiPinType enum. Returns a Counter of the
    resulting KiCad pin type names (for the log).
    """
    if mode not in MODES:
        mode = MODE_SMART
    pins = list(getattr(ki_symbol, "pins", []) or [])
    if mode == MODE_PASSIVE:
        for pin in pins:
            pin.type = kinds.passive
    elif mode == MODE_SMART:
        discrete = prefix_of(getattr(ki_symbol.info, "prefix", "")) in DISCRETE_PREFIXES
        drives: set = set()  # power-out names already used (first one drives)
        for pin in pins:
            if discrete:
                pin.type = kinds.passive
                continue
            implied = classify(pin.name)
            if implied == "power_out":
                key = "".join(_aliases(pin.name))
                pin.type = kinds.passive if key in drives else kinds.power_out
                drives.add(key)
            elif implied is not None:
                pin.type = getattr(kinds, implied)
            elif pin.type in (kinds.unspecified, kinds.power_in):
                # undefined -> bidirectional; EasyEDA "power" on a signal name too
                pin.type = kinds.bidirectional
    return Counter(_kicad_name(p.type.name) for p in pins)


def _kicad_name(enum_name: str) -> str:
    return enum_name[1:] if enum_name.startswith("_") else enum_name


def summary(counts: Counter) -> str:
    label = {"power_in": "power in", "power_out": "power out", "no_connect": "no connect"}
    return ", ".join(f"{n} {label.get(t, t)}" for t, n in counts.most_common())
