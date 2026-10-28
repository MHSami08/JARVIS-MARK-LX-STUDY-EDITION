"""
core/artifact/sanitize.py - allow-list SVG sanitizer (Python side).

Model output is untrusted. This module is the FIRST of two gates: the board
(`runtime/study_board.html`, `sanitizeSvg`) scrubs again in JS. The two allow-lists
must stay identical (master prompt, Appendix H).

Rules
  * `<!DOCTYPE` / `<!ENTITY` are rejected before parsing (entity bombs, XXE).
  * Only allow-listed SVG elements survive; everything else is removed with its subtree.
  * Every `on*` attribute is dropped. `href` / `xlink:href` only when it starts with `#`.
  * Any value holding `javascript:`, `data:text/html` or `<script` is dropped.
  * `url(...)` only as `url(#id)`.
  * `style` holding `@import`, `expression(` or `behavior:` is dropped.
  * Root: viewBox guaranteed, width/height removed, role="img".

Stdlib only (xml.etree.ElementTree). Never raises: returns ("", [reason]) on rejection.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

DEFAULT_VIEWBOX = "0 0 1000 660"
MAX_INPUT_CHARS = 300_000       # same ceiling as the board (SVG_MAX_CHARS)

# Keep in sync with SVG_TAGS in runtime/study_board.html (lower-case compare).
ALLOWED_TAGS = frozenset(
    "svg g defs symbol use marker path line polyline polygon rect circle ellipse "
    "text tspan textpath title desc lineargradient radialgradient stop clippath mask "
    "pattern filter fegaussianblur feoffset femerge femergenode fedropshadow "
    "fecolormatrix fecomposite feflood feturbulence fedisplacementmap feblend".split()
)

# Elements whose text / tail content is real content. Others get whitespace-only text.
_TEXT_TAGS = frozenset({"text", "tspan", "textpath", "title", "desc"})

_BAD_VALUE = re.compile(r"javascript:|data:text/html|<\s*script", re.I)
_BAD_STYLE = re.compile(r"@import|expression\s*\(|behavior\s*:", re.I)
_URL_REF = re.compile(r"url\s*\(\s*(['\"]?)\s*([^)'\"]*)", re.I)
_DOCTYPE = re.compile(r"<!\s*(doctype|entity)", re.I)
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
_NAMED_ENT = re.compile(r"&([a-zA-Z][a-zA-Z0-9]*);")
_PREFIXED_NAME = re.compile(r"^\{(.*)\}(.*)$")

_ET_REGISTERED = False


def _register_namespaces() -> None:
    global _ET_REGISTERED
    if not _ET_REGISTERED:
        ET.register_namespace("", SVG_NS)
        ET.register_namespace("xlink", XLINK_NS)
        _ET_REGISTERED = True


def _split(name: str) -> Tuple[str, str]:
    m = _PREFIXED_NAME.match(name)
    return (m.group(1), m.group(2)) if m else ("", name)


def _attr_ok(local: str, value: str) -> bool:
    low = local.lower()
    if low.startswith("on"):
        return False
    if low == "href" and not value.lstrip().startswith("#"):
        return False
    if _BAD_VALUE.search(value):
        return False
    if "url(" in value.lower().replace(" ", ""):
        for m in _URL_REF.finditer(value):
            if not m.group(2).strip().startswith("#"):
                return False
    if low == "style" and _BAD_STYLE.search(value):
        return False
    return True


def _prepare(text: str) -> str:
    """Slice out the <svg>...</svg> element and make it parseable XML."""
    start = text.find("<svg")
    end = text.rfind("</svg>")
    if start == -1 or end < start:
        return ""
    s = text[start:end + 6]
    s = _BARE_AMP.sub("&amp;", s)

    def _ent(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name in ("amp", "lt", "gt", "quot", "apos"):
            return m.group(0)
        return "&#160;" if name == "nbsp" else "&amp;" + name + ";"

    s = _NAMED_ENT.sub(_ent, s)
    head_end = s.find(">")
    head = s[:head_end] if head_end != -1 else s
    extra = ""
    if "xmlns=" not in head:
        extra += f' xmlns="{SVG_NS}"'
    if "xlink:" in s and "xmlns:xlink" not in head:
        extra += f' xmlns:xlink="{XLINK_NS}"'
    if extra:
        s = "<svg" + extra + s[4:]
    return s


def _clean_element(src: ET.Element, depth: int, counter: List[int], warns: List[str]) -> Optional[ET.Element]:
    ns, local = _split(src.tag)
    low = local.lower()
    if ns not in ("", SVG_NS) or low not in ALLOWED_TAGS:
        warns.append(f"dropped <{local}>")
        return None
    if depth > 40:
        warns.append("dropped over-deep subtree")
        return None
    counter[0] += 1
    out = ET.Element(f"{{{SVG_NS}}}{local}")
    for k, v in src.attrib.items():
        ans, alocal = _split(k)
        if ans == XLINK_NS and alocal == "href":
            if _attr_ok("href", v):
                out.set(f"{{{XLINK_NS}}}href", v)
            continue
        if ans == XML_NS and alocal == "space":
            out.set(f"{{{XML_NS}}}space", v)
            continue
        if ans not in ("", SVG_NS):
            continue
        if _attr_ok(alocal, v):
            out.set(alocal, v)
    if low in _TEXT_TAGS:
        out.text = src.text
    elif src.text and src.text.strip():
        pass  # stray text inside a non-text element is dropped
    for child in list(src):
        kept = _clean_element(child, depth + 1, counter, warns)
        if kept is not None:
            out.append(kept)
        if child.tail and low in _TEXT_TAGS and kept is not None:
            kept.tail = child.tail
    return out


def sanitize_svg_ex(text: str, *, max_chars: int = MAX_INPUT_CHARS,
                    max_elements: int = 4000,
                    generated: Optional[str] = None) -> Tuple[str, List[str]]:
    """(clean_svg, warnings). clean_svg == "" means rejected (warnings say why).

    `generated` ("ai" | "draft") stamps data-jv-generated on the root so the board can
    show the "AI-generated schematic" chip. Curated figures pass generated=None.
    """
    warns: List[str] = []
    try:
        raw = str(text or "")
        if not raw.strip():
            return "", ["empty input"]
        if len(raw) > max_chars:
            return "", [f"input larger than {max_chars} chars"]
        if _DOCTYPE.search(raw):
            return "", ["DOCTYPE/ENTITY is not allowed"]
        prepared = _prepare(raw)
        if not prepared:
            return "", ["no complete <svg>...</svg> element"]
        _register_namespaces()
        try:
            root = ET.fromstring(prepared)
        except ET.ParseError as e:
            return "", [f"not well-formed XML ({e})"]
        counter = [0]
        clean = _clean_element(root, 0, counter, warns)
        if clean is None or _split(clean.tag)[1].lower() != "svg":
            return "", ["root element is not <svg>"]
        if counter[0] > max_elements:
            return "", [f"more than {max_elements} elements"]
        vb = clean.get("viewBox")
        if not vb:
            w, h = clean.get("width", ""), clean.get("height", "")
            try:
                fw, fh = float(re.sub(r"[^0-9.]", "", w)), float(re.sub(r"[^0-9.]", "", h))
                vb = f"0 0 {fw:g} {fh:g}" if fw > 0 and fh > 0 else DEFAULT_VIEWBOX
            except ValueError:
                vb = DEFAULT_VIEWBOX
            clean.set("viewBox", vb)
        clean.attrib.pop("width", None)
        clean.attrib.pop("height", None)
        clean.set("role", "img")
        if generated:
            clean.set("data-jv-generated", str(generated))
        out = ET.tostring(clean, encoding="unicode")
        return out, warns
    except Exception as e:  # never raise into the job runner
        return "", [f"sanitizer error: {type(e).__name__}"]


def sanitize_svg(text: str, **kw) -> str:
    """Clean SVG text, or "" when it cannot be made safe."""
    return sanitize_svg_ex(text, **kw)[0]
