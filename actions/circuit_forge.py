# -*- coding: utf-8 -*-  
"""  
actions/circuit_forge.py - deterministic circuit-diagram renderer (helper module, no TOOL dict).

WHY  
    A language model that writes SVG coordinates for a circuit gets the TOPOLOGY wrong: a battery that  
    never touches the bottom node, a galvanometer with gaps at both ends, labels on top of components.  
    So the model no longer draws circuits. It writes a small JSON SPEC of WHAT is connected to WHAT,  
    and this module DRAWS it - every wire is generated from shared coordinates, so it cannot have gaps.

    After drawing, `verify()` reads the finished geometry back (wires + component terminals), rebuilds the  
    electrical netlist from it and compares it with the netlist of the spec. A mismatch is reported as a bug.

SPEC  (all keys optional except `load`)  
    {  
      "title_en": "...", "title_bn": "...",  
      "source": [ELEM, ...],     # left edge, listed top -> bottom (battery, switch, ammeter ...). + terminal on top  
      "top":    [ELEM, ...],     # along the top wire, left -> right (switch, ammeter ...)  
      "load":   NET  
    }  
    ELEM = {"type": "resistor|battery|cell|switch|ammeter|voltmeter|galvanometer|lamp|capacitor|inductor|diode|  
                      rheostat|ac_source", "label": "R1", "value": "10 Ω", "open": false, "flip": false}  
    NET  = ELEM  
         | {"type":"series",   "items":    [NET, ...]}  
         | {"type":"parallel", "branches": [NET, ...]}  
         | {"type":"bridge", "r1":ELEM, "r2":ELEM, "r3":ELEM, "r4":ELEM, "mid":ELEM}  
           (Wheatstone: r1 top-left, r2 top-right, r3 bottom-left, r4 bottom-right, mid between left and right nodes;  
            the supply is connected to the TOP and BOTTOM nodes)  
"""  
from __future__ import annotations

import math  
import re  
from typing import Any, Dict, List, Optional, Tuple

CYAN, AMBER, GREEN, WHITE, GREY, RED = "#38BDF8", "#FBBF24", "#34D399", "#F8FAFC", "#94A3B8", "#F87171"  
FONT = "'Hind Siliguri','Noto Sans Bengali','Nirmala UI','Vrinda','Segoe UI',Arial,sans-serif"  
W, H = 1000, 660  
SVG_NS = "http://www.w3.org/2000/svg"  
Pt = Tuple[float, float]

ELEMENT_TYPES = ("resistor", "battery", "cell", "switch", "ammeter", "voltmeter", "galvanometer", "lamp",  
                 "capacitor", "inductor", "diode", "rheostat", "ac_source")  
_ALIASES = {  
    "res": "resistor", "r": "resistor", "resistance": "resistor", "ohm": "resistor", "rheo": "rheostat",  
    "cap": "capacitor", "c": "capacitor", "condenser": "capacitor", "ind": "inductor", "l": "inductor", "coil": "inductor",  
    "bulb": "lamp", "light": "lamp", "amm": "ammeter", "a": "ammeter", "ampere_meter": "ammeter",  
    "v": "voltmeter", "volt_meter": "voltmeter", "g": "galvanometer", "galvo": "galvanometer",  
    "dc_source": "battery", "source": "battery", "dc": "battery", "emf": "battery", "supply": "battery",  
    "key": "switch", "sw": "switch", "k": "switch", "ac": "ac_source", "generator": "ac_source", "ac_supply": "ac_source",  
    "led": "diode", "wheatstone": "bridge", "wheatstone_bridge": "bridge", "bridge_circuit": "bridge",  
    "serial": "series", "chain": "series", "par": "parallel", "shunt": "parallel",  
}  
_LEN = {"resistor": 100, "battery": 84, "cell": 70, "switch": 76, "ammeter": 76, "voltmeter": 76, "galvanometer": 76,  
        "lamp": 76, "capacitor": 70, "inductor": 100, "diode": 70, "rheostat": 100, "ac_source": 84}  
_HALF = {"resistor": 32, "battery": 14, "cell": 9, "switch": 20, "ammeter": 18, "voltmeter": 18, "galvanometer": 18,  
         "lamp": 17, "capacitor": 5, "inductor": 28, "diode": 11, "rheostat": 32, "ac_source": 19}  
_METER_LETTER = {"ammeter": "A", "voltmeter": "V", "galvanometer": "G"}  
_SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")

# =============================================================================================  
# spec normalisation + validation

# =============================================================================================  
def _typ(t: Any) -> str:  
    t = re.sub(r"[\s\-]+", "_", str(t or "").strip().lower())  
    return _ALIASES.get(t, t)

def normalize(node: Any) -> Dict[str, Any]:  
    """Coerce the usual model variations into the canonical grammar. Raises ValueError with a fix hint."""  
    if isinstance(node, list):                               # a bare list means a series  
        return {"type": "series", "items": [normalize(n) for n in node]}  
    if not isinstance(node, dict):  
        raise ValueError(f"expected an object, got {type(node).__name__}")  
    t = _typ(node.get("type") or node.get("kind") or node.get("component"))  
    if t == "series":  
        items = node.get("items") or node.get("children") or node.get("elements") or []  
        if not items:  
            raise ValueError('"series" needs a non-empty "items" list')  
        out = [normalize(n) for n in items]  
        return out[0] if len(out) == 1 else {"type": "series", "items": out}  
    if t == "parallel":  
        br = node.get("branches") or node.get("items") or node.get("children") or []  
        if len(br) < 2:  
            raise ValueError('"parallel" needs a "branches" list with at least 2 branches')  
        return {"type": "parallel", "branches": [normalize(n) for n in br]}  
    if t == "bridge":  
        arms = node.get("arms")  
        get = lambda k, i: node.get(k) or (arms[i] if isinstance(arms, list) and len(arms) > i else None)   # noqa: E731  
        res = {}  
        for k, i in (("r1", 0), ("r2", 1), ("r3", 2), ("r4", 3)):  
            a = get(k, i)  
            if not a:  
                raise ValueError(f'"bridge" is missing "{k}" (needs r1 r2 r3 r4 and mid)')

            a = normalize(a)
            if a["type"] in ("series", "parallel", "bridge"):  
                raise ValueError(f'bridge arm "{k}" must be a single component')  
            res[k] = a  
        mid = node.get("mid") or node.get("galvanometer") or node.get("g") or node.get("middle") or {"type": "galvanometer", "label": "G"}  
        mid = normalize(mid)  
        if mid["type"] in ("series", "parallel", "bridge"):  
            raise ValueError('bridge "mid" must be a single component (usually a galvanometer)')  
        res["mid"] = mid  
        res["type"] = "bridge"  
        return res  
    if t not in ELEMENT_TYPES:  
        raise ValueError(f'unknown component type "{node.get("type")}". Use one of: {", ".join(ELEMENT_TYPES)}, series, parallel, bridge')  
    label = str(node.get("label") or node.get("name") or node.get("id") or "").strip()  
    value = str(node.get("value") or node.get("val") or node.get("rating") or "").strip()  
    return {"type": t, "label": label, "value": value,  
            "open": bool(node.get("open")) and str(node.get("open")).lower() not in ("false", "0", "no"),  
            "flip": bool(node.get("flip")) and str(node.get("flip")).lower() not in ("false", "0", "no")}

def normalize_spec(spec: Any) -> Dict[str, Any]:  
    if not isinstance(spec, dict):  
        raise ValueError("the spec must be a JSON object with a \"load\"")  
    load = spec.get("load") or spec.get("network") or spec.get("circuit")  
    if not load:  
        raise ValueError('the spec needs a "load" (the network between the supply\'s top and bottom wire)')  
    src = spec.get("source") or spec.get("sources") or spec.get("left") or []  
    top = spec.get("top") or []  
    def lst(v):                                              # noqa: E301  
        v = v if isinstance(v, list) else [v]  
        out = [normalize(x) for x in v if x]  
        for e in out:

           if e["type"] in ("series", "parallel", "bridge"):  
                raise ValueError('"source" and "top" may only contain single components (put networks in "load")')  
        return out  
    return {"title_en": str(spec.get("title_en") or ""), "title_bn": str(spec.get("title_bn") or ""),  
            "source": lst(src), "top": lst(top), "load": normalize(load)}

def leaves(net: Dict[str, Any]) -> List[Dict[str, Any]]:  
    t = net["type"]  
    if t == "series":  
        return [x for c in net["items"] for x in leaves(c)]  
    if t == "parallel":  
        return [x for c in net["branches"] for x in leaves(c)]  
    if t == "bridge":  
        return [net["r1"], net["r2"], net["r3"], net["r4"], net["mid"]]  
    return [net]

def all_elements(spec: Dict[str, Any]) -> List[Dict[str, Any]]:  
    return list(spec["source"]) + list(spec["top"]) + leaves(spec["load"])

# =============================================================================================  
# values + electrical sanity maths  
# =============================================================================================  
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")  
_MULT = {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3, "μ": 1e-6, "µ": 1e-6, "u": 1e-6, "": 1.0}

def parse_value(s: str) -> Optional[Tuple[float, str]]:  
    s = (s or "").translate(_BN_DIGITS).replace(",", "")  
    m = re.match(r"\s*([\d.]+)\s*([kKMmμµu]?)\s*(Ω|ohms?|ওহম|V|volts?|ভোল্ট|F|H|A|হেনরি|ফ্যারাড)?", s)

        if not m or not m.group(1).replace(".", "").isdigit():
        return None  
    try:  
        num = float(m.group(1)) * _MULT.get(m.group(2), 1.0)  
    except ValueError:  
        return None  
    u = (m.group(3) or "").lower()  
    unit = {"ω": "ohm", "ohm": "ohm", "ohms": "ohm", "ওহম": "ohm", "v": "volt", "volt": "volt", "volts": "volt", "ভোল্ট": "volt",  
            "f": "farad", "ফ্যারাড": "farad", "h": "henry", "হেনরি": "henry", "a": "amp"}.get(u, "")  
    return num, unit

def fmt_num(x: float) -> str:  
    if x == 0:  
        return "0"  
    if abs(x) >= 1000 or abs(x) < 0.01:  
        return f"{x:.3g}"  
    return f"{x:.3f}".rstrip("0").rstrip(".")

def _ohms(e: Dict[str, Any]) -> Optional[float]:  
    t = e["type"]  
    if t == "resistor":  
        pv = parse_value(e["value"])  
        return pv[0] if pv and pv[1] in ("ohm", "") else None  
    if t in ("ammeter",):  
        return 0.0  
    if t == "switch":  
        return float("inf") if e.get("open") else 0.0  
    return None

def equivalent(net: Dict[str, Any]) -> Optional[float]:  
    """Series/parallel/balanced-bridge equivalent resistance, or None when it cannot be stated honestly."""  
    t = net["type"]  
    if t == "series":  
        vals = [equivalent(c) for c in net["items"]]  
        return None if any(v is None for v in vals) else sum(vals)  
    if t == "parallel":  
        vals = [equivalent(c) for c in net["branches"]]  
        if any(v is None for v in vals):  
            return None  
        if any(v == 0 for v in vals):  
            return 0.0  
        inv = sum(0.0 if v == float("inf") else 1.0 / v for v in vals)  
        return float("inf") if inv == 0 else 1.0 / inv  
    if t == "bridge":  
        r = [_ohms(net[k]) for k in ("r1", "r2", "r3", "r4")]  
        if any(v is None for v in r):  
            return None  
        if abs(r[0] * r[3] - r[1] * r[2]) <= 1e-9 * max(1.0, r[0] * r[3]):  
            return ((r[0] + r[2]) * (r[1] + r[3])) / (r[0] + r[1] + r[2] + r[3])  
        return None  
    if t == "voltmeter":  
        return float("inf")  
    return _ohms(net)

def notes_bn(spec: Dict[str, Any]) -> str:  
    els = all_elements(spec)  
    lines = []  
    for e in els:  
        name = e["label"] or e["type"]  
        lines.append(f"- **{name}:** {_TYPE_BN.get(e['type'], e['type'])}" + (f" — {e['value']}" if e["value"] else ""))

   load = spec["load"]  
    out = "\n".join(lines)  
    if load["type"] == "bridge":  
        r = [_ohms(load[k]) for k in ("r1", "r2", "r3", "r4")]  
        out += "\n\n**সুষম ব্রিজের শর্ত:** $\\dfrac{R_1}{R_2} = \\dfrac{R_3}{R_4}$ হলে গ্যালভানোমিটারে কোনো প্রবাহ থাকে না।"  
        if all(v is not None for v in r):  
            ok = abs(r[0] * r[3] - r[1] * r[2]) <= 1e-9 * max(1.0, r[0] * r[3])  
            out += f"\n\nদেওয়া মান অনুযায়ী ব্রিজটি **{'সুষম' if ok else 'সুষম নয়'}** (${R}_{1}{R}_{4}=fm{t}_{n}um(r[0]*r[3])$, ${R}_{2}{R}_{3}=fm{t}_{n}um(r[1]*r[2])$)।"  
    req = equivalent(load)  
    if req is not None and req not in (0.0, float("inf")):  
        series_extra = [_ohms(e) for e in spec["source"] + spec["top"] if e["type"] in ("resistor", "ammeter", "switch")]  
        if all(v is not None for v in series_extra):  
            req += sum(series_extra)  
        out += f"\n\n**সমতুল্য রোধ (JARVIS হিসাব করেছে):** $R_{{eq}} = {fmt_num(req)}\\,\\Omega$"  
        volts = [parse_value(e["value"]) for e in els if e["type"] in ("battery", "cell") and e["value"]]  
        volts = [v for v in volts if v and v[1] in ("volt", "")]  
        bats = [e for e in els if e["type"] in ("battery", "cell")]  
        if len(bats) == 1 and len(volts) == 1:  
            out += f"\n\nমোট প্রবাহ: $I = \\dfrac{{V}}{{R_{{eq}}}} = \\dfrac{{{fmt_num(volts[0][0])}}}{{{fmt_num(req)}}} = {fmt_num(volts[0][0]/req)}\\,A$"  
    return out

_TYPE_BN = {"resistor": "রোধ", "battery": "তড়িৎ কোষ/ব্যাটারি", "cell": "তড়িৎ কোষ", "switch": "সুইচ/চাবি", "ammeter": "অ্যামিটার",  
            "voltmeter": "ভোল্টমিটার", "galvanometer": "গ্যালভানোমিটার", "lamp": "বাতি", "capacitor": "ধারক", "inductor": "আবেশক",  
            "diode": "ডায়োড", "rheostat": "রিওস্ট্যাট", "ac_source": "এসি উৎস"}

# =============================================================================================  
# abstract netlist straight from the spec (independent of any geometry)  
# =============================================================================================  
def spec_netlist(spec: Dict[str, Any]) -> List[Tuple[int, int, int]]:  
    """[(element_index, node_a, node_b)] in drawing order: source, top, then load leaves."""

   counter = [2]  
    TOP, BOT = 0, 1  
    out: List[Tuple[int, int, int]] = []  
    idx = [0]

    def new() -> int:  
        counter[0] += 1  
        return counter[0]

    def emit(t: int, b: int) -> None:  
        out.append((idx[0], t, b)); idx[0] += 1

    def chain(n: int, t: int, b: int, items) -> None:  
        nodes = [t] + [new() for _ in range(n - 1)] + [b]  
        for i, it in enumerate(items):  
            walk(it, nodes[i], nodes[i + 1])

    def walk(net, t, b) -> None:  
        ty = net["type"]  
        if ty == "series":  
            chain(len(net["items"]), t, b, net["items"])  
        elif ty == "parallel":  
            for br in net["branches"]:  
                walk(br, t, b)  
        elif ty == "bridge":  
            L, R = new(), new()  
            emit(t, L); emit(t, R); emit(L, b); emit(R, b); emit(L, R)  
        else:  
            emit(t, b)

    # source chain (left edge) top->bottom between TOP and BOT; then the top wire items sit between the corner and the load  
    # electrically: left chain from BOT up to TOPLEFT, top chain TOPLEFT -> TOPLOAD, load TOPLOAD -> BOT

   TL = new()                                   # top-left corner node (after the source chain)  
    n_src = len(spec["source"])  
    # drawing order is source(top->bottom): item0 at the top. Electrically BOT -> ... -> TL (bottom to top), so item i is between  
    # node(i+1) (lower) and node(i) (upper).  
    src_nodes = [TL] + [new() for _ in range(max(0, n_src - 1))] + [BOT]  
    for i in range(n_src):  
        emit(src_nodes[i], src_nodes[i + 1])  
    if n_src == 0:  
        TL = BOT                                   # no source drawn: left edge is a plain wire down to the bottom wire  
    top_nodes = [TL] + [new() for _ in range(max(0, len(spec["top"]) - 1))] + [TOP]  
    for i in range(len(spec["top"])):  
        emit(top_nodes[i], top_nodes[i + 1])  
    if not spec["top"]:  
        # TL and TOP are the same wire  
        remap = {TOP: TL}  
        out[:] = [(i, remap.get(a, a), remap.get(b, b)) for i, a, b in out]  
        TOP = TL  
    walk(spec["load"], TOP, BOT)  
    return out

# =============================================================================================  
# drawing  
# =============================================================================================  
class _Scene:  
    def __init__(self) -> None:  
        self.wires: List[List[Pt]] = []  
        self.elems: List[Dict[str, Any]] = []        # {idx, type, P, Q}  
        self.svg_wires: List[str] = []  
        self.svg_syms: List[str] = []  
        self.svg_text: List[str] = []  
        self.dots: List[Pt] = []

       self.pts: List[Pt] = []                      # everything that occupies space (for bbox)  
        self.count = 0

    def wire(self, *pts: Pt) -> None:  
        pts = tuple(p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1])  
        if len(pts) < 2:  
            return  
        self.wires.append(list(pts))  
        self.pts.extend(pts)

    def dot(self, p: Pt) -> None:  
        self.dots.append(p)  
        self.pts.append(p)

def _f(x: float) -> str:  
    return f"{x:.1f}".rstrip("0").rstrip(".")

def _esc(s: str) -> str:  
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _sym(e: Dict[str, Any]) -> str:  
    """Local-coordinate symbol: horizontal, centred on 0,0, `_HALF[type]` long each side. Positive terminal at -x."""  
    t = e["type"]  
    st = f'stroke="{AMBER}" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"'  
    sg = f'stroke="{GREEN}" stroke-width="3" fill="none" stroke-linecap="round"'  
    if t == "resistor":  
        return f'<polyline {st} points="-32,0 -25,0 -20,-10 -10,10 0,-10 10,10 20,-10 25,0 32,0"/>'  
    if t == "rheostat":  
        return (f'<polyline {st} points="-32,0 -25,0 -20,-10 -10,10 0,-10 10,10 20,-10 25,0 32,0"/>'

               f'<line x1="-16" y1="22" x2="18" y2="-22" stroke="{AMBER}" stroke-width="2.2"/>'  
                f'<polygon points="18,-22 8,-18 15,-11" fill="{AMBER}"/>')  
    if t == "battery":                                # two cells: long(+) short long short  
        o = ""  
        for x, hgt, w in ((-10.5, 28, 2.8), (-3.5, 15, 5), (3.5, 28, 2.8), (10.5, 15, 5)):  
            o += f'<line x1="{x}" y1="{-hgt/2}" x2="{x}" y2="{hgt/2}" stroke="{GREEN}" stroke-width="{w}" stroke-linecap="butt"/>'  
        return o + f'<path d="M-24,-17 h8 M-20,-21 v8" {sg} stroke-width="1.8"/>'  
    if t == "cell":  
        return (f'<line x1="-4" y1="-14" x2="-4" y2="14" stroke="{GREEN}" stroke-width="2.8"/>'  
                f'<line x1="4" y1="-8" x2="4" y2="8" stroke="{GREEN}" stroke-width="5"/>'  
                f'<path d="M-15,-14 h7 M-11.5,-17.5 v7" {sg} stroke-width="1.8"/>')  
    if t == "switch":  
        if e.get("open"):  
            return (f'<circle cx="-18" cy="0" r="3.6" fill="{AMBER}"/><circle cx="18" cy="0" r="3.6" fill="{AMBER}"/>'  
                    f'<line x1="-18" y1="0" x2="13" y2="-17" {st}/>')  
        return (f'<circle cx="-18" cy="0" r="3.6" fill="{AMBER}"/><circle cx="18" cy="0" r="3.6" fill="{AMBER}"/>'  
                f'<line x1="-18" y1="0" x2="18" y2="0" {st}/>')  
    if t in _METER_LETTER:  
        return f'<circle r="17" fill="#1E293B" stroke="{AMBER}" stroke-width="3"/>'  
    if t == "lamp":  
        return (f'<circle r="16" fill="#1E293B" stroke="{AMBER}" stroke-width="3"/>'  
                f'<path d="M-11,-11 L11,11 M-11,11 L11,-11" {st} stroke-width="2.4"/>')  
    if t == "capacitor":  
        return (f'<line x1="-4.5" y1="-16" x2="-4.5" y2="16" stroke="{AMBER}" stroke-width="3.4"/>'  
                f'<line x1="4.5" y1="-16" x2="4.5" y2="16" stroke="{AMBER}" stroke-width="3.4"/>')  
    if t == "inductor":  
        return f'<path d="M-28,0 a7,8 0 0 1 14,0 a7,8 0 0 1 14,0 a7,8 0 0 1 14,0 a7,8 0 0 1 14,0" {st}/>'  
    if t == "diode":  
        return (f'<polygon points="-9,-12 -9,12 9,0" fill="#1E293B" stroke="{AMBER}" stroke-width="3" stroke-linejoin="round"/>'  
                f'<line x1="9" y1="-12" x2="9" y2="12" {st}/>')  
    if t == "ac_source":  
        return (f'<circle r="19" fill="#1E293B" stroke="{GREEN}" stroke-width="3"/>'

               f'<path d="M-11,0 q5.5,-13 11,0 t11,0" {sg} stroke-width="2.4"/>')  
    return ""

def _label_lines(e: Dict[str, Any]) -> List[Tuple[str, str, float, bool]]:  
    """[(text, colour, size, bold)] shown beside the component."""  
    lines = []  
    lab = e.get("label") or ""  
    lab = re.sub(r"([A-Za-z])(\d+)$", lambda m: m.group(1) + m.group(2).translate(_SUB), lab)  
    if lab and lab != _METER_LETTER.get(e["type"]):  
        lines.append((lab, WHITE, 17, True))  
    if e.get("value"):  
        lines.append((e["value"], AMBER, 15, False))  
    return lines

def _tw(text: str, size: float, bold: bool) -> float:  
    w = 0.0  
    for ch in text:  
        if "\u0980" <= ch <= "\u09ff":  
            w += size * (0.30 if "\u09be" <= ch <= "\u09cd" else 0.62)  
        elif ch.isspace():  
            w += size * 0.3  
        else:  
            w += size * (0.60 if bold else 0.54)  
    return w

def _lbl_w(e: Dict[str, Any]) -> float:  
    ls = _label_lines(e)  
    return max([_tw(t, s, b) for t, _, s, b in ls] or [0.0])

def draw_element(sc: _Scene, e: Dict[str, Any], P: Pt, Q: Pt, *, label_dir: Tuple[float, float] = (1.0, 0.0),  
                 label_off: Optional[float] = None) -> None:  
    """Draw component `e` between terminals P and Q (any direction) with leads and a label."""  
    t = e["type"]  
    if e.get("flip"):  
        P, Q = Q, P  
    dx, dy = Q[0] - P[0], Q[1] - P[1]  
    dist = math.hypot(dx, dy) or 1.0  
    ux, uy = dx / dist, dy / dist  
    M = ((P[0] + Q[0]) / 2, (P[1] + Q[1]) / 2)  
    hl = _HALF[t]  
    a = (M[0] - ux * hl, M[1] - uy * hl)  
    b = (M[0] + ux * hl, M[1] + uy * hl)  
    sc.svg_wires.append(f'<polyline points="{_f(P[0])},{_f(P[1])} {_f(a[0])},{_f(a[1])}" fill="none" stroke="{CYAN}" stroke-width="3" stroke-linecap="round"/>')  
    sc.svg_wires.append(f'<polyline points="{_f(b[0])},{_f(b[1])} {_f(Q[0])},{_f(Q[1])}" fill="none" stroke="{CYAN}" stroke-width="3" stroke-linecap="round"/>')  
    ang = math.degrees(math.atan2(dy, dx))  
    sc.svg_syms.append(f'<g transform="translate({_f(M[0])} {_f(M[1])}) rotate({_f(ang)})">{_sym(e)}</g>')  
    if t in _METER_LETTER:  
        sc.svg_text.append(f'<text x="{_f(M[0])}" y="{_f(M[1] + 6)}" text-anchor="middle" font-size="18" font-weight="700" fill="{AMBER}">{_METER_LETTER[t]}</text>')  
    sc.elems.append({"type": t, "P": P if not e.get("flip") else Q, "Q": Q if not e.get("flip") else P, "M": M})  
    sc.pts += [P, Q]  
    # label  
    ls = _label_lines(e)  
    if not ls:  
        return  
    r = 20 if t in _METER_LETTER or t in ("lamp", "ac_source") else 16  
    off = label_off if label_off is not None else r + 12  
    nx, ny = label_dir  
    cx, cy = M[0] + nx * off, M[1] + ny * off  
    h = 20 * len(ls)  
    # anchor by the horizontal direction of the normal

   if nx > 0.35:  
        anchor, tx = "start", cx  
    elif nx < -0.35:  
        anchor, tx = "end", cx  
    else:  
        anchor, tx = "middle", cx  
    y0 = cy - h / 2 + 15 if abs(ny) < 0.6 else (cy - h + 15 if ny < 0 else cy + 15)  
    for i, (txt, col, size, bold) in enumerate(ls):  
        yy = y0 + i * 20  
        sc.svg_text.append(f'<text x="{_f(tx)}" y="{_f(yy)}" text-anchor="{anchor}" font-size="{size}" '  
                           f'{"font-weight=\"700\" " if bold else ""}fill="{col}">{_esc(txt)}</text>')  
        w = _tw(txt, size, bold)  
        x0 = tx if anchor == "start" else tx - w if anchor == "end" else tx - w / 2  
        sc.pts += [(x0, yy - size), (x0 + w, yy + 4)]

# ---- measuring ------------------------------------------------------------------------------  
PAD = 12                                           # wire between stacked items  
DX, DY = 150, 135                                  # bridge half-diagonals

def measure(net: Dict[str, Any]) -> Tuple[float, float]:  
    t = net["type"]  
    if t == "series":  
        ms = [measure(c) for c in net["items"]]  
        return max(m[0] for m in ms), sum(m[1] for m in ms) + PAD * (len(ms) - 1)  
    if t == "parallel":  
        ms = [measure(c) for c in net["branches"]]  
        cell = max(m[0] for m in ms)  
        return cell * len(ms) + 14 * (len(ms) - 1), max(m[1] for m in ms) + 36  
    if t == "bridge":  
        return 2 * DX + 2 * (max(_lbl_w(net[k]) for k in ("r1", "r2", "r3", "r4")) + 40), 2 * DY

   return max(120.0, 2 * (34 + _lbl_w(net) + 10)), float(_LEN[t])

def place(sc: _Scene, net: Dict[str, Any], cx: float, y: float) -> float:  
    """Draw `net` with its top terminal at (cx, y). Returns the y of its bottom terminal."""  
    t = net["type"]  
    if t == "series":  
        yy = y  
        for i, c in enumerate(net["items"]):  
            yb = place(sc, c, cx, yy)  
            if i < len(net["items"]) - 1:  
                sc.wire((cx, yb), (cx, yb + PAD))  
                yy = yb + PAD  
            else:  
                yy = yb  
        return yy  
    if t == "parallel":  
        br = net["branches"]  
        ms = [measure(c) for c in br]  
        cell = max(m[0] for m in ms)  
        hmax = max(m[1] for m in ms)  
        n = len(br)  
        centers = [cx + (i - (n - 1) / 2) * (cell + 14) for i in range(n)]  
        ytop, ybot = y + 18, y + 18 + hmax  
        sc.wire((cx, y), (cx, ytop))  
        sc.wire((centers[0], ytop), (centers[-1], ytop))  
        sc.wire((cx, ybot), (cx, ybot + 18))  
        sc.wire((centers[0], ybot), (centers[-1], ybot))  
        for i, (c, ccx) in enumerate(zip(br, centers)):  
            yb = place(sc, c, ccx, ytop)  
            sc.wire((ccx, yb), (ccx, ybot))  
            if 0 < i < n - 1 or (abs(ccx - cx) < 0.5 and n % 2 == 1):

               sc.dot((ccx, ytop)); sc.dot((ccx, ybot))  
            elif abs(ccx - cx) > 0.5 and 0 < i < n - 1:  
                pass  
        if n % 2 == 1:                                     # the centre branch shares x with the terminal stub: T junction  
            sc.dot((cx, ytop)); sc.dot((cx, ybot))  
        return ybot + 18  
    if t == "bridge":  
        T, B = (cx, y), (cx, y + 2 * DY)  
        L, R = (cx - DX, y + DY), (cx + DX, y + DY)  
        centre = (cx, y + DY)  
        for k, (p, q) in (("r1", (T, L)), ("r2", (T, R)), ("r3", (L, B)), ("r4", (R, B))):  
            mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2  
            vx, vy = mx - centre[0], my - centre[1]  
            nrm = math.hypot(vx, vy) or 1.0  
            draw_element(sc, net[k], p, q, label_dir=(vx / nrm, vy / nrm), label_off=30)  
        draw_element(sc, net["mid"], L, R, label_dir=(0.0, 1.0), label_off=34)  
        for p in (T, L, R, B):  
            sc.dot(p)  
        return y + 2 * DY  
    draw_element(sc, net, (cx, y), (cx, y + _LEN[t]), label_dir=(1.0, 0.0))  
    return y + _LEN[t]

def draw_circuit(spec: Dict[str, Any]) -> Tuple[_Scene, Dict[str, Any]]:  
    sc = _Scene()  
    load = spec["load"]  
    src, top = spec["source"], spec["top"]  
    lw, lh = measure(load)  
    n_s = len(src)  
    left_need = n_s * 100 + 40 if n_s else 0  
    Hloop = max(lh + 40, left_need, 200)  
    gap = 70 + max([_lbl_w(e) for e in src] or [0.0]) * 0 + 40

   top_need = sum(150 for _ in top) + 40  
    xl = 0.0  
    cx = max(xl + gap + lw / 2, xl + top_need + 40)  
    yt, yb = 0.0, Hloop  
    ly = yt + (Hloop - lh) / 2                                   # load's top terminal y  
    # left edge  
    left_pts: List[Pt] = []  
    if n_s:  
        seg = Hloop / n_s  
        cur = (xl, yt)  
        for i, e in enumerate(src):  
            c = yt + (i + 0.5) * seg  
            L = _LEN[e["type"]]  
            P, Q = (xl, c - L / 2), (xl, c + L / 2)  
            sc.wire(cur, P)  
            draw_element(sc, e, P, Q, label_dir=(-1.0, 0.0))  
            cur = Q  
        sc.wire(cur, (xl, yb))  
    else:  
        sc.wire((xl, yt), (xl, yb))  
    # top wire (items spaced evenly between the corner and the load stub)  
    if top:  
        seg = (cx - xl) / len(top)  
        cur = (xl, yt)  
        for i, e in enumerate(top):  
            c = xl + (i + 0.5) * seg  
            L = _LEN[e["type"]]  
            P, Q = (c - L / 2, yt), (c + L / 2, yt)  
            sc.wire(cur, P)  
            draw_element(sc, e, P, Q, label_dir=(0.0, -1.0), label_off=34)  
            cur = Q  
        sc.wire(cur, (cx, yt))

   else:  
        sc.wire((xl, yt), (cx, yt))  
    sc.wire((xl, yb), (cx, yb))  
    # load + stubs  
    yb_load = place(sc, load, cx, ly)  
    sc.wire((cx, yt), (cx, ly))  
    sc.wire((cx, yb_load), (cx, yb))  
    # current arrow on the top wire (conventional current leaves the + terminal at the top of the source)  
    ax = xl + (cx - xl) * (0.28 if top else 0.5)  
    sc.svg_syms.append(f'<polygon points="{_f(ax+9)},{_f(yt)} {_f(ax-5)},{_f(yt-7)} {_f(ax-5)},{_f(yt+7)}" fill="{CYAN}"/>')  
    sc.svg_text.append(f'<text x="{_f(ax)}" y="{_f(yt-14)}" text-anchor="middle" font-size="16" font-weight="700" fill="{CYAN}">I</text>')  
    sc.pts += [(ax, yt - 30)]  
    return sc, {"xl": xl, "cx": cx, "yt": yt, "yb": yb, "ly": ly, "yb_load": yb_load}

# =============================================================================================  
# verification: read the geometry back and rebuild the netlist  
# =============================================================================================  
def verify(spec: Dict[str, Any], sc: _Scene) -> List[str]:  
    """Problems found by comparing the drawn geometry with the spec ([] = the drawing is electrically correct)."""  
    q = lambda p: (round(p[0] * 2) / 2, round(p[1] * 2) / 2)          # noqa: E731  
    parent: Dict[Pt, Pt] = {}

    def find(a: Pt) -> Pt:  
        parent.setdefault(a, a)  
        while parent[a] != a:  
            parent[a] = parent[parent[a]]  
            a = parent[a]  
        return a

    def union(a: Pt, b: Pt) -> None:  
        parent[find(a)] = find(b)

    for w in sc.wires:  
        for a, b in zip(w, w[1:]):  
            union(q(a), q(b))  
    for d in sc.dots:  
        find(q(d))  
    issues: List[str] = []  
    # wire ends that touch nothing (dangling) - count how many wire/element endpoints meet at every point  
    deg: Dict[Pt, int] = {}  
    for w in sc.wires:  
        for p in (w[0], w[-1]):  
            deg[q(p)] = deg.get(q(p), 0) + 1  
    for e in sc.elems:  
        for p in (e["P"], e["Q"]):  
            deg[q(p)] = deg.get(q(p), 0) + 1  
    # interior contact: a point lying inside another wire segment counts as connected (T junctions)  
    def on_wire(p: Pt) -> bool:  
        for w in sc.wires:  
            for a, b in zip(w, w[1:]):  
                if min(a[0], b[0]) - 0.6 <= p[0] <= max(a[0], b[0]) + 0.6 and min(a[1], b[1]) - 0.6 <= p[1] <= max(a[1], b[1]) + 0.6:  
                    if abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) <= 0.8 * (math.hypot(b[0] - a[0], b[1] - a[1]) or 1):  
                        return True  
        return False  
    for p, n in deg.items():  
        if n == 1 and not on_wire(p):  
            issues.append(f"dangling end at ({p[0]:.0f},{p[1]:.0f})")  
    # T junctions: merge the nets of every endpoint that lies on another wire's interior  
    for p in list(deg):  
        for w in sc.wires:  
            for a, b in zip(w, w[1:]):  
                if q(a) != p and q(b) != p and on_wire(p) and (min(a[0], b[0]) - .6 <= p[0] <= max(a[0], b[0]) + .6 and min(a[1], b[1]) - .6 <= p[1] <= max(a[1], b[1]) + .6):  
                    if abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) <= 0.8 * (math.hypot(b[0] - a[0], b[1] - a[1]) or 1):

                       union(p, q(a))  
    # netlist from geometry  
    geo = [(find(q(e["P"])), find(q(e["Q"]))) for e in sc.elems]  
    ref = spec_netlist(spec)  
    # drawing order of sc.elems is: source (top->bottom), top, then load in placement order. spec_netlist follows the same order.  
    if len(geo) != len(ref):  
        return issues + [f"element count differs: drawn {len(geo)}, spec {len(ref)}"]  
    fwd: Dict[int, Pt] = {}  
    bwd: Dict[Pt, int] = {}  
    for (i, a, b), (ga, gb) in zip(ref, geo):  
        for ab, gg in ((a, ga), (b, gb)):  
            if fwd.setdefault(ab, gg) != gg or bwd.setdefault(gg, ab) != ab:  
                issues.append(f"element #{i} is wired to the wrong net")  
                break  
    return issues

# =============================================================================================  
# render to a finished SVG  
# =============================================================================================  
def render(spec_in: Any, *, uid: str = "c1") -> Dict[str, Any]:  
    """spec (raw model JSON) -> {"svg", "notes_md", "spec", "problems"}. Raises ValueError for an invalid spec."""  
    spec = normalize_spec(spec_in)  
    sc, info = draw_circuit(spec)  
    problems = verify(spec, sc)  
    xs = [p[0] for p in sc.pts]; ys = [p[1] for p in sc.pts]  
    bw, bh = max(xs) - min(xs) + 40, max(ys) - min(ys) + 40  
    s = min(1.0, 900.0 / bw, 500.0 / bh)  
    tx = (W - bw * s) / 2 - (min(xs) - 20) * s  
    ty = 80 + (560 - bh * s) / 2 - (min(ys) - 20) * s  
    o = [f'<svg viewBox="0 0 {W} {H}" xmlns="{SVG_NS}" font-family="{FONT}">',  
         f"<title>{_esc(spec['title_en'] or 'Circuit')}</title>"]

   if spec["title_bn"]:  
        o.append(f'<text x="24" y="34" font-size="17" font-weight="700" fill="{WHITE}">{_esc(spec["title_bn"])}</text>')  
    if spec["title_en"]:  
        o.append(f'<text x="24" y="54" font-size="12.5" fill="{GREY}">{_esc(spec["title_en"])}</text>')  
    o.append(f'<g transform="translate({_f(tx)} {_f(ty)}) scale({s:.3f})">')  
    for w in sc.wires:  
        o.append(f'<polyline points="{" ".join(f"{_f(x)},{_f(y)}" for x, y in w)}" fill="none" stroke="{CYAN}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')  
    o += sc.svg_wires + sc.svg_syms  
    for d in sc.dots:  
        o.append(f'<circle cx="{_f(d[0])}" cy="{_f(d[1])}" r="5" fill="{GREEN}"/>')  
    o += sc.svg_text  
    o.append("</g></svg>")  
    return {"svg": "\n".join(o), "notes_md": notes_bn(spec), "spec": spec, "problems": problems, "scale": s}  