# -*- coding: utf-8 -*-
"""
actions/diagram_forge.py - JARVIS's own diagram illustrator (helper module, no TOOL dict).

WHY IT EXISTS
    The voice model is a fast talker. Asking it to hand-write a detailed SVG inside a tool call gives
    3-shape sketches. Claude-style artifacts work because the model WRITES CODE, LOOKS AT THE RESULT
    and FIXES IT. This module gives JARVIS the same loop, off the voice path:

        1. PLAN     a text model lists every structure the HSC/NCTB textbook figure labels (JSON)
        2. DRAW     a text model writes ARTWORK ONLY (anatomy) or a full schematic (circuits, rays...)
        3. COMPOSE  Python adds the labels: columns, leader lines, dots, caption - never overlapping
        4. CHECK    a static linter reads the SVG like a reviewer: broken refs, missing parts, too little
                    detail, overlapping/out-of-canvas text ...  (+ optional vision review, see below)
        5. FIX      the linter's findings go back to the model; up to MAX_ROUNDS-1 repair rounds
        6. CACHE    the finished figure is stored in diagrams/generated/ so asking again is instant

    Splitting "artwork" from "labels" is the key trick: models draw decent pictures but place text
    badly, while a layout routine places text perfectly but cannot draw.

SWITCHES (environment variables, all optional)
    JARVIS_DIAGRAM_FORGE=0       turn the illustrator off (old behaviour)
    JARVIS_ILLUSTRATOR_MODEL=..  try this Gemini model first (default: the SMART ladder, REST models only)
    JARVIS_DIAGRAM_VISION=1      EXPERIMENTAL: also render a PNG with QtSvg and let Gemini look at it

Everything here is import-safe: no Qt, no network at import time.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

GEN_DIR = _BASE / "diagrams" / "generated"
MAX_ROUNDS = 3                 # 1 draw + up to 2 repair rounds
MAX_CACHE_FILES = 150

W, H = 1000, 660               # final canvas
ART_W, ART_H = 540, 560        # artwork contract box (anatomy)
ART_X, ART_Y = 230, 80         # where the artwork box sits on the canvas
COL_W = 182                    # label column width
FONT = "'Hind Siliguri','Noto Sans Bengali','Nirmala UI','Vrinda','Segoe UI',Arial,sans-serif"
CYAN, AMBER, WHITE, GREY = "#38BDF8", "#FBBF24", "#F8FAFC", "#94A3B8"

SVG_NS = "http://www.w3.org/2000/svg"
_SHAPES = {"path", "ellipse", "circle", "rect", "polygon", "polyline", "line"}
_GRADS = {"lineargradient", "radialgradient"}


def enabled() -> bool:
    return os.environ.get("JARVIS_DIAGRAM_FORGE", "1").strip().lower() not in ("0", "false", "no", "off")


# =============================================================================================
# 1. talking to Gemini (REST models only - the Live rung and the Llama fallbacks cannot draw)
# =============================================================================================
def _gem():
    try:
        from core import gemini
        return gemini
    except Exception:
        return None


def _models(g) -> List[str]:
    first = os.environ.get("JARVIS_ILLUSTRATOR_MODEL", "").strip()
    ladder = []
    try:
        ladder = [m for m in g._LADDERS[g.SMART] if m != getattr(g, "LIVE", "live")]
    except Exception:
        ladder = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite"]
    return list(dict.fromkeys(([first] if first else []) + ladder))


def _llm(prompt: str, *, system: str = "", images: Optional[List[bytes]] = None,
         max_tokens: int = 32000, temperature: float = 0.5, timeout_s: int = 150) -> Optional[str]:
    """One generation through every (REST model x API key) until one answers. None if all fail."""
    g = _gem()
    if g is None:
        return None
    try:
        from google.genai import types
    except Exception:
        return None
    cfg = types.GenerateContentConfig(system_instruction=system or None, temperature=temperature,
                                      max_output_tokens=max_tokens)
    contents: List[Any] = []
    for png in images or []:
        contents.append(types.Part.from_bytes(data=png, mime_type="image/png"))
    contents.append(prompt)
    try:
        keys = list(g.api_keys())
    except Exception:
        keys = []
    for model in _models(g):
        for key in keys:
            try:
                if g._cooling(model, key) or g._key_dead(key):
                    continue
            except Exception:
                pass
            try:
                client = g.client(timeout_ms=timeout_s * 1000, key=key)
                resp = client.models.generate_content(model=model, contents=contents, config=cfg)
                out = (getattr(resp, "text", None) or "").strip()
                if out:
                    return out
            except Exception as e:                                   # noqa: BLE001
                msg = str(e)
                try:
                    if g.is_key_error(msg):
                        g._mark_key_dead(key, "the API rejected it")
                    elif g.is_quota_error(msg):
                        g._cool(model, g._COOLDOWN_SECONDS, key)
                    elif g.is_gone_error(msg):
                        g._cool(model, g._GONE_SECONDS)
                    elif g.is_unavailable_error(msg):
                        g._cool(model, g._UNAVAILABLE_SECONDS)
                except Exception:
                    pass
                print(f"[Forge] {model}: {type(e).__name__}: {msg[:120]}")
    return None


# =============================================================================================
# 2. prompts
# =============================================================================================
PLAN_SYSTEM = (
    "You plan figures for Bangladesh NCTB HSC textbooks (Biology, Physics, Chemistry, ICT, Higher Math). "
    "Reply with ONE JSON object and nothing else."
)

PLAN_PROMPT = """Student request: {brief}
Title: {title}
Subject: {subject}

Return JSON with exactly these keys:
{{
  "kind": "anatomy" | "schematic",
  "title_en": "short English figure title",
  "title_bn": "Bangla figure title as the NCTB textbook would write it",
  "parts": [ {{"en": "English term", "bn": "Bangla term (textbook spelling)", "note_bn": "one short Bangla sentence: what it is / does"}} ],
  "inset": null | {{"title_en": "...", "title_bn": "...", "what": "what the magnified view shows", "parts": [{{"en": "...", "bn": "...", "note_bn": "..."}}]}}
}}

Rules:
- "anatomy" = a labelled picture of an organism, organ, cell, structure, apparatus or molecule model.
  "schematic" = circuits, ray/force/vector diagrams, flowcharts, life cycles, block diagrams, reaction schemes.
- For "anatomy" list EVERY structure the textbook figure labels (typically 8-14 parts, at least 6), main terms in the
  order a student would read them. Use standard scientific English terms and the textbook Bangla terms.
- Add an "inset" ONLY when the textbook figure needs a magnified/section view to show microscopic detail
  (e.g. basidium on a gill, villus cross-section). Otherwise null.
- For "schematic" "parts" may be empty or list the components/labels that must appear.
- Never invent structures that are not in the real figure."""

ANATOMY_SYSTEM = """You are a scientific illustrator drawing textbook figures for Bangladesh NCTB HSC students.
Output ONLY the tagged blocks requested below: no explanations, no markdown fences.

You draw ARTWORK ONLY. A layout engine adds every text label, leader line and the caption afterwards.

ARTWORK CONTRACT
- Coordinate box: x 0..540, y 0..560 (origin top-left). Keep ALL artwork inside it; the subject fills ~80% of the box.
- Write bare SVG child elements only: g, path, ellipse, circle, rect, polygon, polyline, line, defs, linearGradient,
  radialGradient, stop, clipPath, pattern, mask. NO <svg> wrapper, NO <text>, NO <style>, <script>, <image>,
  <foreignObject>. Never paint a background rectangle over the whole box.
- Gradient/clip/pattern ids: short and unique (g1, g2, ...). Every url(#id) MUST be defined in a <defs> you write.
- Realism: curved organic outlines with <path> (C/Q commands), not plain rectangles for living things. Use 3-6
  gradients for volume (light top-left, darker edge), one soft highlight, a shadow ellipse under free-standing
  objects, fine texture strokes (veins, fibres, speckles, pores) at low opacity, and a 1.5-2px outline in a darker
  shade of the fill. Vary shapes - no copy-paste grids of identical items.
- Colours must look natural for the specimen and read well on a dark navy board: light/mid-tone fills, visible outlines.
- Show EVERY structure in the plan's parts: each clearly visible, large enough to point at (>= 14px), plus the internal
  detail the textbook figure shows (cut-away / section where the book uses one).

LABEL TABLE
Write one line per part: English | Bangla | x | y | side
(x,y) is a point exactly ON that visible part, inside the 0..540 x 0..560 box (the leader line ends there).
Use the plan's part names verbatim. Keep points at least 25px apart. "side" is optional: left or right (default: nearest).

OUTPUT FORMAT (exact tags)
<artwork>
...elements...
</artwork>
<labels>
English | বাংলা | x | y | side
</labels>
(only when the plan has an inset, ALSO write <inset_artwork>...</inset_artwork> and <inset_labels>...</inset_labels>
 with the same rules, again in a fresh 0..540 x 0..560 box)

STYLE EXAMPLE (format and technique only - do not copy the subject)
<artwork>
<defs><radialGradient id="g1" cx="38%" cy="25%" r="80%"><stop offset="0" stop-color="#FFF6E5"/><stop offset="0.6" stop-color="#E0C79B"/><stop offset="1" stop-color="#9A7A52"/></radialGradient></defs>
<ellipse cx="270" cy="520" rx="120" ry="14" fill="#000" opacity="0.35"/>
<path d="M80,250 C80,130 170,70 270,70 C370,70 460,130 460,250 Z" fill="url(#g1)" stroke="#6F5233" stroke-width="2"/>
<path d="M150,150 q20,-6 40,0" fill="none" stroke="#9A7A52" stroke-width="1.2" opacity="0.5"/>
</artwork>
<labels>
Cap | ছাতা | 380 | 120 | right
</labels>"""

SCHEMATIC_SYSTEM = """You are a physics/chemistry/ICT diagram illustrator for Bangladesh NCTB HSC students.
Output ONE complete SVG and nothing else (no markdown fences, no explanation).

CANVAS
<svg viewBox="0 0 1000 660" xmlns="http://www.w3.org/2000/svg" font-family="'Hind Siliguri','Noto Sans Bengali','Nirmala UI','Vrinda',sans-serif">
Fill the canvas sensibly (margins >= 30px). Dark navy board background - never paint a full-canvas background rect.

ALLOWED ELEMENTS: g, path, line, polyline, polygon, rect, circle, ellipse, text, tspan, defs, marker, linearGradient,
radialGradient, stop, clipPath. NO style/script/image/foreignObject. Every url(#id) must be defined.

COLOURS: wires/lines #38BDF8 (stroke-width 3), components #FBBF24, source/voltage #34D399, text #F8FAFC,
secondary text #94A3B8, danger/negative #F87171.

TEXT: font-size >= 15, bold for names. Put every label 20-26px away from the thing it names, NEVER on top of a wire
or another label. Bangla text must stay short.

CIRCUITS: standard symbols - resistor = zig-zag polyline (6 peaks) or a rectangle, battery = one long and one short
parallel plate, switch = angled line with two dots, lamp = circle with a cross, ammeter/voltmeter = circle with A/V.
Junctions get a filled dot. Draw current/ray arrows with <marker> arrowheads. PARALLEL resistors: one resistor per
horizontal branch between two shared vertical rails (never short a resistor with a wire). SERIES: all in one loop.
Write values next to components (e.g. 3 Ω, 10 V). Check that the topology matches the request before answering.

OTHER DIAGRAMS (rays, forces, flowcharts, cycles): consistent arrow style, evenly spaced boxes/nodes, labels centred
inside boxes, title not needed (the board shows it)."""

FIX_PROMPT = """Here is your previous output for "{title}":

{previous}

A reviewer found these problems:
{issues}

Return the COMPLETE corrected output in exactly the same format (all tags again). Fix the listed problems and keep
everything that was fine. Do not add commentary."""

VISION_PROMPT = """You are reviewing a textbook-style diagram titled "{title}" (Bangladesh HSC). The image was rendered by a
simple renderer: ignore missing blur/shadow filters and missing arrowhead markers. Judge only: Is the subject clearly
recognisable and detailed enough for a textbook? Are labels readable, not overlapping each other or the drawing? Do
leader lines end on the correct parts? Is anything cut off or empty? Reply with JSON only:
{{"ok": true|false, "problems": ["short, specific, fixable problem", ...]}}"""


# =============================================================================================
# 3. parsing model output
# =============================================================================================
def _strip_fences(s: str) -> str:
    s = re.sub(r"```[a-zA-Z0-9_-]*\s*", "", s or "")
    return s.replace("```", "")


def extract_json(raw: str) -> Optional[dict]:
    raw = _strip_fences(raw or "")
    if "{" not in raw:
        return None
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    raw = re.sub(r",(\s*[\]}])", r"\1", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _block(raw: str, tag: str) -> Optional[str]:
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", raw or "", re.S | re.I)
    return m.group(1).strip() if m else None


def parse_labels(block: Optional[str]) -> List[Dict[str, Any]]:
    out = []
    for line in (block or "").splitlines():
        line = line.strip().strip("-*• ")
        if line.count("|") < 3:
            continue
        cells = [c.strip() for c in line.split("|")]
        try:
            x, y = float(cells[2]), float(cells[3])
        except Exception:
            continue
        side = cells[4].lower() if len(cells) > 4 and cells[4].lower() in ("left", "right") else ""
        out.append({"en": cells[0], "bn": cells[1], "x": x, "y": y, "side": side})
    return out


def parse_anatomy(raw: str) -> Dict[str, Any]:
    raw = _strip_fences(raw)
    art = _block(raw, "artwork")
    if art is not None:
        art = re.sub(r"^\s*<svg\b[^>]*>|</svg>\s*$", "", art, flags=re.I).strip()
    ins = _block(raw, "inset_artwork")
    if ins is not None:
        ins = re.sub(r"^\s*<svg\b[^>]*>|</svg>\s*$", "", ins, flags=re.I).strip()
    return {"artwork": art, "labels": parse_labels(_block(raw, "labels")),
            "inset": ins, "inset_labels": parse_labels(_block(raw, "inset_labels"))}


def parse_schematic(raw: str) -> Optional[str]:
    raw = _strip_fences(raw)
    s, e = raw.find("<svg"), raw.rfind("</svg>")
    return raw[s:e + 6].strip() if s != -1 and e > s else None


# =============================================================================================
# 4. cleaning + XML helpers
# =============================================================================================
_BAD_ELEMS = re.compile(r"<(script|style|image|foreignObject|iframe|object|embed|animate\w*|set)\b[^>]*?(/>|>.*?</\1\s*>)", re.S | re.I)
_ON_ATTR = re.compile(r"\s+on\w+\s*=\s*(\"[^\"]*\"|'[^']*')", re.I)


def clean_markup(s: str) -> str:
    s = _BAD_ELEMS.sub("", s or "")
    s = _ON_ATTR.sub("", s)
    s = re.sub(r"&(?!#?\w+;)", "&amp;", s)            # bare ampersands break XML
    return s


def _strip_text(s: str) -> str:
    return re.sub(r"<text\b[^>]*?(/>|>.*?</text\s*>)", "", s, flags=re.S | re.I)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse(inner_or_svg: str, wrap: bool) -> ET.Element:
    src = inner_or_svg if not wrap else f'<svg xmlns="{SVG_NS}">{inner_or_svg}</svg>'
    if not wrap and "xmlns=" not in src[:300]:
        src = src.replace("<svg", f'<svg xmlns="{SVG_NS}"', 1)
    src = re.sub(r"\sxlink:href=", " href=", src)
    return ET.fromstring(src)


def _prefix_ids(s: str, p: str) -> str:
    ids = set(re.findall(r"""\bid=["']([^"']+)["']""", s))
    for i in sorted(ids, key=len, reverse=True):
        s = re.sub(rf"""(\bid=["']){re.escape(i)}(["'])""", rf"\g<1>{p}-{i}\g<2>", s)
        s = s.replace(f"url(#{i})", f"url(#{p}-{i})").replace(f"url('#{i}')", f"url(#{p}-{i})")
        s = s.replace(f'href="#{i}"', f'href="#{p}-{i}"')
    return s


# =============================================================================================
# 5. label layout + composition (the part that makes labels look right)
# =============================================================================================
def _is_bn(ch: str) -> bool:
    return "\u0980" <= ch <= "\u09ff"


def est_width(text: str, fs: float, bold: bool = False) -> float:
    w = 0.0
    for ch in text:
        if _is_bn(ch):
            w += fs * (0.30 if "\u09be" <= ch <= "\u09cd" else 0.62)      # vowel signs/hasanta are narrow
        elif ch.isspace():
            w += fs * 0.30
        else:
            w += fs * (0.60 if bold else 0.54)
    return w


def _fit(text: str, base: float, maxw: float, bold: bool, lo: float = 11.5) -> float:
    fs = base
    while fs > lo and est_width(text, fs, bold) > maxw:
        fs -= 0.5
    return fs


def _spread(desired: List[float], lo: float, hi: float, gap: float) -> List[float]:
    """Place label baselines as close as possible to `desired` while keeping `gap` between neighbours."""
    n = len(desired)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: desired[i])
    ys = [min(max(desired[i], lo), hi) for i in order]
    for i in range(1, n):
        ys[i] = max(ys[i], ys[i - 1] + gap)
    if ys[-1] > hi:
        ys[-1] = hi
        for i in range(n - 2, -1, -1):
            ys[i] = min(ys[i], ys[i + 1] - gap)
    if ys[0] < lo:                                    # more labels than room: even spacing
        step = (hi - lo) / max(1, n - 1)
        ys = [lo + i * step for i in range(n)]
    res = [0.0] * n
    for rank, i in enumerate(order):
        res[i] = ys[rank]
    return res


def compose_anatomy(artwork: str, labels: List[Dict[str, Any]], *, title_en: str, title_bn: str,
                    uid: str, tag: str = "") -> str:
    art = _prefix_ids(_strip_text(clean_markup(artwork)), uid)
    left, right = [], []
    for lb in labels:
        x = min(max(lb["x"], 4), ART_W - 4)
        y = min(max(lb["y"], 4), ART_H - 4)
        side = lb.get("side") or ("left" if x < ART_W / 2 else "right")
        (left if side == "left" else right).append(dict(lb, x=x, y=y))
    cap = (H - 90) // 48 + 1                                  # labels that fit in one column
    while len(left) > cap:
        right.append(left.pop(max(range(len(left)), key=lambda i: left[i]["x"])))
    while len(right) > cap:
        left.append(right.pop(min(range(len(right)), key=lambda i: right[i]["x"])))

    o: List[str] = []
    o.append(f'<svg viewBox="0 0 {W} {H}" xmlns="{SVG_NS}" font-family="{FONT}">')
    o.append(f"<title>{_esc(title_en)}</title>")
    o.append(f'<defs><clipPath id="{uid}-clip"><rect x="0" y="0" width="{ART_W}" height="{ART_H}"/></clipPath></defs>')
    o.append(f'<text x="24" y="34" font-size="17" font-weight="700" fill="{WHITE}">{_esc(title_bn)}{_esc(" " + tag) if tag else ""}</text>')
    o.append(f'<text x="24" y="54" font-size="12.5" fill="{GREY}">{_esc(title_en)}</text>')
    o.append(f'<g transform="translate({ART_X} {ART_Y})" clip-path="url(#{uid}-clip)">{art}</g>')

    for side, items in (("left", left), ("right", right)):
        ys = _spread([it["y"] + ART_Y + 4 for it in items], 92, H - 34, 48)
        for it, yb in zip(items, ys):
            tx, ty = it["x"] + ART_X, it["y"] + ART_Y
            if side == "right":
                elbow, end, anchor, textx = ART_X + ART_W + 4, ART_X + ART_W + 24, "start", ART_X + ART_W + 32
            else:
                elbow, end, anchor, textx = ART_X - 4, ART_X - 24, "end", ART_X - 32
            ly = yb - 5
            o.append(f'<polyline points="{tx:.0f},{ty:.0f} {elbow},{ly:.0f} {end},{ly:.0f}" fill="none" stroke="{CYAN}" stroke-width="1.4" stroke-linejoin="round"/>')
            o.append(f'<circle cx="{tx:.0f}" cy="{ty:.0f}" r="3.6" fill="{CYAN}" stroke="#06101C" stroke-width="1"/>')
            fe = _fit(it["en"], 16.5, COL_W, True)
            o.append(f'<text x="{textx}" y="{yb:.0f}" text-anchor="{anchor}" font-size="{fe:g}" font-weight="700" fill="{WHITE}">{_esc(it["en"])}</text>')
            if it.get("bn"):
                fb = _fit(it["bn"], 14.5, COL_W, True)
                o.append(f'<text x="{textx}" y="{yb + 19:.0f}" text-anchor="{anchor}" font-size="{fb:g}" font-weight="600" fill="{AMBER}">{_esc(it["bn"])}</text>')
    o.append("</svg>")
    return "\n".join(o)


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


# =============================================================================================
# 6. the reviewer: static lint
# =============================================================================================
def _walk(el: ET.Element, dx: float = 0.0, dy: float = 0.0, skip: bool = False):
    tr = el.get("transform") or ""
    if tr:
        m = re.fullmatch(r"\s*translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)\s*", tr)
        if m:
            dx, dy = dx + float(m.group(1)), dy + float(m.group(2))
        else:
            skip = True
    yield el, dx, dy, skip
    for ch in el:
        yield from _walk(ch, dx, dy, skip)


def _num(v: Optional[str], default: float) -> float:
    try:
        return float(re.sub(r"[^\d.\-]", "", v)) if v else default
    except Exception:
        return default


def text_boxes(root: ET.Element) -> List[Dict[str, Any]]:
    boxes = []
    for el, dx, dy, skip in _walk(root):
        if _local(el.tag) != "text" or skip:
            continue
        txt = " ".join("".join(el.itertext()).split())
        if not txt:
            continue
        fs = _num(el.get("font-size"), 16)
        x, y = _num(el.get("x"), 0) + dx, _num(el.get("y"), 0) + dy
        w = est_width(txt, fs, (el.get("font-weight") or "") in ("bold", "600", "700", "800"))
        anchor = el.get("text-anchor") or "start"
        x0 = x - w if anchor == "end" else x - w / 2 if anchor == "middle" else x
        boxes.append({"text": txt, "x0": x0, "x1": x0 + w, "y0": y - fs * 0.95, "y1": y + fs * 0.28, "fs": fs})
    return boxes


def _overlap_issues(boxes: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    out = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            iw = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
            ih = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
            if iw > 2 and ih > 2:
                small = min((a["x1"] - a["x0"]) * (a["y1"] - a["y0"]), (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]))
                if small > 0 and iw * ih / small > 0.30:
                    out.append(f'ERROR text "{a["text"][:28]}" overlaps text "{b["text"][:28]}" (around x={max(a["x0"], b["x0"]):.0f}, y={max(a["y0"], b["y0"]):.0f}); move one of them')
                    if len(out) >= limit:
                        return out
    return out


def _ref_issues(root: ET.Element) -> List[str]:
    ids, refs, issues = set(), set(), []
    for el in root.iter():
        if el.get("id"):
            if el.get("id") in ids:
                issues.append(f'ERROR duplicate id "{el.get("id")}"')
            ids.add(el.get("id"))
        for v in el.attrib.values():
            for m in re.finditer(r"url\(\s*['\"]?#([^)'\"\s]+)", v):
                refs.add(m.group(1))
    for r in sorted(refs - ids):
        issues.append(f'ERROR url(#{r}) is used but no element has id="{r}" (the fill/stroke would vanish) - define it in <defs>')
    return issues


def _count(root: ET.Element) -> Dict[str, int]:
    c = {"shapes": 0, "paths": 0, "grads": 0, "texts": 0, "colors": 0}
    cols = set()
    for el in root.iter():
        t = _local(el.tag)
        if t in _SHAPES:
            c["shapes"] += 1
        if t == "path":
            c["paths"] += 1
        if t in _GRADS:
            c["grads"] += 1
        if t == "text":
            c["texts"] += 1
        for k in ("fill", "stroke", "stop-color"):
            v = (el.get(k) or "").strip().lower()
            if v.startswith("#") or v.startswith("rgb"):
                cols.add(v)
    c["colors"] = len(cols)
    return c


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0980-\u09ff]", "", (s or "").lower())


def lint_anatomy(artwork: Optional[str], labels: List[Dict[str, Any]], parts: List[Dict[str, Any]],
                 *, inset: bool = False) -> List[str]:
    issues: List[str] = []
    if not artwork:
        return ["ERROR no <artwork> block found - output the tagged blocks exactly as specified"]
    try:
        root = _parse(clean_markup(artwork), wrap=True)
    except ET.ParseError as e:
        return [f"ERROR the artwork is not valid XML ({e}); close every tag, quote every attribute, escape & as &amp;"]
    c = _count(root)
    need_shapes, need_paths = (14, 4) if inset else (30, 8)
    if c["shapes"] < need_shapes:
        issues.append(f"ERROR too little detail: {c['shapes']} shapes, need at least {need_shapes}. Add the internal structures, texture strokes, highlights and shading the textbook figure shows")
    if c["paths"] < need_paths:
        issues.append(f"ERROR only {c['paths']} curved <path> outlines (need >= {need_paths}); draw organic outlines, not just ellipses/rects")
    if c["grads"] < (1 if inset else 2):
        issues.append("WARN add gradients (radialGradient/linearGradient) for volume and realism")
    if c["colors"] < 4:
        issues.append(f"WARN only {c['colors']} distinct colours; use a natural palette with light, mid and dark tones")
    issues += _ref_issues(root)

    min_labels = 3 if inset else 6
    if len(labels) < min_labels:
        issues.append(f"ERROR only {len(labels)} label lines; need at least {min_labels} (format: English | Bangla | x | y | side)")
    names = [(_norm(lb["en"]), _norm(lb["bn"])) for lb in labels]
    for p in parts:
        pe, pb = _norm(p.get("en", "")), _norm(p.get("bn", ""))
        if not any((pe and (pe in e or e in pe) and e) or (pb and (pb in b or b in pb) and b) for e, b in names):
            issues.append(f'ERROR part "{p.get("en")}" from the plan has no line in the label table (draw it and label it)')
    for lb in labels:
        if not (0 <= lb["x"] <= ART_W and 0 <= lb["y"] <= ART_H):
            issues.append(f'ERROR label "{lb["en"]}" points at ({lb["x"]:.0f},{lb["y"]:.0f}) outside the 0..{ART_W} x 0..{ART_H} box')
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            if abs(labels[i]["x"] - labels[j]["x"]) < 18 and abs(labels[i]["y"] - labels[j]["y"]) < 18:
                issues.append(f'WARN labels "{labels[i]["en"]}" and "{labels[j]["en"]}" point at almost the same spot; they must name different parts')
    return issues[:14]


def lint_schematic(svg: Optional[str]) -> List[str]:
    if not svg:
        return ["ERROR no complete <svg>...</svg> found"]
    try:
        root = _parse(clean_markup(svg), wrap=False)
    except ET.ParseError as e:
        return [f"ERROR the SVG is not valid XML ({e}); close every tag, quote every attribute, escape & as &amp;"]
    issues: List[str] = []
    c = _count(root)
    if c["shapes"] < 12:
        issues.append(f"ERROR too little drawn: {c['shapes']} shapes (need >= 12). Draw every component and wire/arrow explicitly")
    if c["texts"] < 3:
        issues.append(f"ERROR only {c['texts']} text labels; name the components/values/axes")
    issues += _ref_issues(root)
    boxes = text_boxes(root)
    issues += _overlap_issues(boxes)
    vb = (root.get("viewBox") or f"0 0 {W} {H}").replace(",", " ").split()
    try:
        vw, vh = float(vb[2]), float(vb[3])
    except Exception:
        vw, vh = W, H
    for b in boxes:
        if b["x0"] < -4 or b["x1"] > vw + 4 or b["y0"] < -4 or b["y1"] > vh + 4:
            issues.append(f'ERROR text "{b["text"][:30]}" runs outside the canvas; move or shorten it')
        if b["fs"] < 13:
            issues.append(f'WARN text "{b["text"][:24]}" is only {b["fs"]:g}px; use >= 15px')
    return issues[:14]


def needs_fix(issues: List[str]) -> bool:
    return any(i.startswith("ERROR") for i in issues) or sum(i.startswith("WARN") for i in issues) >= 3


# =============================================================================================
# 7. optional vision review (experimental, off by default)
# =============================================================================================
def render_png(svg: str, width: int = 1100) -> Optional[bytes]:
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
        from PyQt6.QtGui import QColor, QImage, QPainter
        from PyQt6.QtSvg import QSvgRenderer
        r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if not r.isValid():
            return None
        vb = r.viewBoxF()
        h = max(1, int(width * vb.height() / max(1.0, vb.width())))
        img = QImage(width, h, QImage.Format.Format_ARGB32)
        img.fill(QColor("#0b1626"))
        p = QPainter(img)
        r.render(p, QRectF(0, 0, width, h))
        p.end()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())
    except Exception:
        return None


def vision_review(svg: str, title: str) -> List[str]:
    if os.environ.get("JARVIS_DIAGRAM_VISION", "0").strip().lower() not in ("1", "true", "yes", "on"):
        return []
    png = render_png(svg)
    if not png:
        return []
    raw = _llm(VISION_PROMPT.format(title=title), images=[png], max_tokens=1200, temperature=0.2, timeout_s=60)
    data = extract_json(raw or "") or {}
    if data.get("ok") is False:
        return [f"ERROR (visual review) {p}" for p in (data.get("problems") or [])[:5] if isinstance(p, str)]
    return []


# =============================================================================================
# 8. orchestrator
# =============================================================================================
_SCHEMATIC_WORDS = ("circuit", "resistor", "battery", "ohm", "kirchhoff", "capacitor", "inductor", "lens", "ray ",
                    "mirror", "vector", "force", "flowchart", "flow chart", "algorithm", "cycle", "block diagram",
                    "gate", "logic", "সার্কিট", "রোধ", "ব্যাটারি", "লেন্স", "বল", "ফ্লোচার্ট", "লজিক")


def _guess_kind(text: str) -> str:
    t = (text or "").lower()
    return "schematic" if any(w in t for w in _SCHEMATIC_WORDS) else "anatomy"


def _plan(brief: str, title: str, subject: str) -> Dict[str, Any]:
    raw = _llm(PLAN_PROMPT.format(brief=brief, title=title, subject=subject), system=PLAN_SYSTEM,
               max_tokens=4000, temperature=0.2, timeout_s=60)
    plan = extract_json(raw or "") or {}
    kind = plan.get("kind") if plan.get("kind") in ("anatomy", "schematic") else _guess_kind(f"{title} {brief}")
    parts = [p for p in (plan.get("parts") or []) if isinstance(p, dict) and p.get("en")]
    inset = plan.get("inset") if isinstance(plan.get("inset"), dict) else None
    if inset is not None:
        inset["parts"] = [p for p in (inset.get("parts") or []) if isinstance(p, dict) and p.get("en")]
    return {"kind": kind, "title_en": str(plan.get("title_en") or title), "title_bn": str(plan.get("title_bn") or title),
            "parts": parts, "inset": inset, "from_model": bool(plan)}


def _parts_json(plan: Dict[str, Any]) -> str:
    slim = {"parts": [{"en": p.get("en"), "bn": p.get("bn")} for p in plan["parts"]]}
    if plan.get("inset"):
        slim["inset"] = {"what": plan["inset"].get("what"), "title_en": plan["inset"].get("title_en"),
                         "parts": [{"en": p.get("en"), "bn": p.get("bn")} for p in plan["inset"]["parts"]]}
    return json.dumps(slim, ensure_ascii=False)


def _fmt_anatomy(parsed: Dict[str, Any]) -> str:
    out = f"<artwork>\n{parsed.get('artwork') or ''}\n</artwork>\n<labels>\n" + "\n".join(
        f"{l['en']} | {l['bn']} | {l['x']:.0f} | {l['y']:.0f} | {l.get('side') or ''}" for l in parsed["labels"]) + "\n</labels>"
    if parsed.get("inset"):
        out += f"\n<inset_artwork>\n{parsed['inset']}\n</inset_artwork>\n<inset_labels>\n" + "\n".join(
            f"{l['en']} | {l['bn']} | {l['x']:.0f} | {l['y']:.0f} | {l.get('side') or ''}" for l in parsed["inset_labels"]) + "\n</inset_labels>"
    return out


def forge(brief: str, title: str, subject: str = "", *,
          progress: Optional[Callable[[str], None]] = None,
          cancelled: Callable[[], bool] = lambda: False) -> Dict[str, Any]:
    """Plan -> draw -> compose -> check -> fix. Never raises. Result keys:
       ok, blocks[{caption, svg}], notes_md, plan, issues (what was still flagged), rounds, error"""
    say = progress or (lambda _m: None)
    res: Dict[str, Any] = {"ok": False, "blocks": [], "notes_md": "", "plan": {}, "issues": [], "rounds": 0, "error": ""}
    try:
        say("১/৪  কী কী অংশ আঁকতে হবে, পরিকল্পনা করছি…")
        plan = _plan(brief, title, subject)
        res["plan"] = plan
        if cancelled():
            res["error"] = "cancelled"
            return res
        uid = "f" + hashlib.md5(f"{title}{time.time()}".encode()).hexdigest()[:5]

        if plan["kind"] == "anatomy":
            system = ANATOMY_SYSTEM
            ask = (f'Draw the textbook figure "{plan["title_en"]}" for this request: {brief}\n'
                   f"Plan (draw and label ALL of these parts): {_parts_json(plan)}")
            parsed: Dict[str, Any] = {}
            say("২/৪  ছবি আঁকছি…")
            raw = _llm(ask, system=system)
            issues: List[str] = []
            for rnd in range(1, MAX_ROUNDS + 1):
                res["rounds"] = rnd
                if not raw:
                    res["error"] = "the drawing model did not answer (quota or network)"
                    return res
                parsed = parse_anatomy(raw)
                issues = lint_anatomy(parsed["artwork"], parsed["labels"], plan["parts"])
                if plan.get("inset") and parsed.get("inset") is not None:
                    issues += [f"(inset) {i}" for i in lint_anatomy(parsed["inset"], parsed["inset_labels"],
                                                                     plan["inset"].get("parts") or [], inset=True)]
                elif plan.get("inset"):
                    issues.append("ERROR the plan needs an inset: add <inset_artwork> and <inset_labels>")
                if not needs_fix(issues) or rnd == MAX_ROUNDS or cancelled():
                    break
                say(f"৩/৪  নিজের আঁকা পরীক্ষা করে {sum(i.startswith('ERROR') for i in issues)}টি সমস্যা পেয়েছি, ঠিক করছি… (দফা {rnd})")
                raw = _llm(FIX_PROMPT.format(title=plan["title_en"], previous=_fmt_anatomy(parsed),
                                             issues="\n".join(f"- {i}" for i in issues)), system=system)
                if not raw:                                         # repair failed: keep what we have
                    break
            if not parsed.get("artwork") or any(i.startswith("ERROR no <artwork>") or "not valid XML" in i for i in issues):
                res["error"] = "the drawing could not be repaired: " + "; ".join(issues[:2])
                return res
            say("৪/৪  লেবেল বসাচ্ছি…")
            main = compose_anatomy(parsed["artwork"], parsed["labels"], title_en=plan["title_en"],
                                   title_bn=plan["title_bn"], uid=uid)
            res["blocks"].append({"caption": plan["title_bn"], "svg": main})
            if plan.get("inset") and parsed.get("inset"):
                ins_issues = lint_anatomy(parsed["inset"], parsed["inset_labels"], [], inset=True)
                if not any("not valid XML" in i or "no <artwork>" in i for i in ins_issues):
                    ip = plan["inset"]
                    res["blocks"].append({"caption": ip.get("title_bn") or "বর্ধিত অংশ", "svg": compose_anatomy(
                        parsed["inset"], parsed["inset_labels"], title_en=ip.get("title_en") or "Magnified view",
                        title_bn=ip.get("title_bn") or "বর্ধিত অংশ", uid=uid + "i", tag="(বর্ধিত)")})
            res["issues"] = issues
        else:
            system = SCHEMATIC_SYSTEM
            ask = f'Draw: "{plan["title_en"]}". Request: {brief}\nMust include (if relevant): {_parts_json(plan)}'
            say("২/৪  ডায়াগ্রাম আঁকছি…")
            raw = _llm(ask, system=system)
            svg, issues = None, []
            for rnd in range(1, MAX_ROUNDS + 1):
                res["rounds"] = rnd
                if not raw:
                    res["error"] = "the drawing model did not answer (quota or network)"
                    return res
                svg = parse_schematic(raw)
                issues = lint_schematic(svg)
                if not needs_fix(issues):
                    issues += vision_review(svg, plan["title_en"]) if svg else []
                if not needs_fix(issues) or rnd == MAX_ROUNDS or cancelled():
                    break
                say(f"৩/৪  নিজের আঁকা পরীক্ষা করে {sum(i.startswith('ERROR') for i in issues)}টি সমস্যা পেয়েছি, ঠিক করছি… (দফা {rnd})")
                raw = _llm(FIX_PROMPT.format(title=plan["title_en"], previous=svg or raw,
                                             issues="\n".join(f"- {i}" for i in issues)), system=system)
                if not raw:
                    break
            if not svg or any("not valid XML" in i for i in issues):
                res["error"] = "the diagram could not be repaired: " + "; ".join(issues[:2])
                return res
            svg = _prefix_ids(clean_markup(svg), uid)
            if "font-family" not in svg[:400]:
                svg = svg.replace("<svg", f'<svg font-family="{FONT}"', 1)
            res["blocks"].append({"caption": plan["title_bn"], "svg": svg})
            res["issues"] = issues

        res["notes_md"] = notes_markdown(plan)
        res["ok"] = bool(res["blocks"])
    except Exception as e:                                           # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def notes_markdown(plan: Dict[str, Any]) -> str:
    lines = []
    for p in plan.get("parts") or []:
        bn, en, note = p.get("bn") or "", p.get("en") or "", p.get("note_bn") or ""
        lines.append(f"- **{bn or en} ({en}):** {note}" if note else f"- **{bn or en} ({en})**")
    ins = plan.get("inset") or {}
    for p in ins.get("parts") or []:
        bn, en, note = p.get("bn") or "", p.get("en") or "", p.get("note_bn") or ""
        lines.append(f"- **{bn or en} ({en}):** {note}" if note else f"- **{bn or en} ({en})**")
    if not lines:
        return ""
    return "\n".join(lines) + "\n\nবি.দ্র.: চিত্রটি JARVIS নিজে এঁকেছে, তাই অংশের নাম ও গঠন NCTB বইয়ের চিত্রের সঙ্গে একবার মিলিয়ে নিও।"


# =============================================================================================
# 9. cache of finished figures  (diagrams/generated/*.json)
# =============================================================================================
_STOP = {"diagram", "figure", "fig", "of", "the", "a", "an", "and", "structure", "drawing", "picture", "image",
         "চিত্র", "ডায়াগ্রাম", "এর", "ও", "আঁকো", "দেখাও", "ছবি", "গঠন"}


def cache_key(title: str) -> str:
    toks = [t for t in re.findall(r"[a-z0-9]+|[\u0980-\u09ff]+", (title or "").lower()) if t not in _STOP]
    return "-".join(sorted(set(toks)))[:80]


def cache_lookup(title: str) -> Optional[Dict[str, Any]]:
    key = cache_key(title)
    if not key or not GEN_DIR.is_dir():
        return None
    want = set(key.split("-"))
    best, best_score = None, 0.0
    for f in GEN_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        have = set(str(d.get("key", "")).split("-"))
        if not have or not want:
            continue
        score = len(want & have) / len(want | have)
        if score > best_score:
            best, best_score = d, score
    return best if best_score >= 0.75 and best and best.get("blocks") else None


def cache_store(title: str, subject: str, res: Dict[str, Any]) -> None:
    try:
        key = cache_key(title)
        if not key:
            return
        GEN_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(GEN_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:max(0, len(files) - MAX_CACHE_FILES + 1)]:
            old.unlink(missing_ok=True)
        name = hashlib.md5(key.encode()).hexdigest()[:10] + ".json"
        (GEN_DIR / name).write_text(json.dumps({
            "key": key, "title": title, "subject": subject, "created": time.strftime("%Y-%m-%d %H:%M"),
            "blocks": res["blocks"], "notes_md": res.get("notes_md", ""),
            "plan": {k: res["plan"].get(k) for k in ("title_en", "title_bn", "parts", "kind")}}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:                                           # noqa: BLE001
        print(f"[Forge] cache write skipped: {e}")


def cache_forget(title: str) -> None:
    key = cache_key(title)
    try:
        (GEN_DIR / (hashlib.md5(key.encode()).hexdigest()[:10] + ".json")).unlink(missing_ok=True)
    except Exception:
        pass
