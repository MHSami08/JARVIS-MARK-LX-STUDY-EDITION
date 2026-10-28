"""
core/artifact/design.py - the "look" of generated figures, as code.

Single source of truth for colours / fonts. The illustrator prompt reads TOKENS, and
(from Phase B) the macro compiler reads the same TOKENS, so a theme change is one edit.

PHASE A: `illustrator_system()` asks the model for a complete, hand-labelled <svg>
(there is no macro compiler yet). Phase B swaps in the macro-based prompt (Appendix E).

The system prompt is NOT part of the Live tool declaration: it goes to the one-shot
router only. Router guard `_MAX_SYSTEM_CHARS` is 20 000 -> we hard-assert < 16 000.
"""
from __future__ import annotations

from typing import Dict

TOKENS: Dict[str, str] = {
    "bg": "#07111D",
    "panel": "#0C1C2B",
    "text": "#F8FAFC",
    "text_bn": "#FBBF24",
    "muted": "#94A3B8",
    "accent": "#38BDF8",
    "success": "#34D399",
    "warn": "#F59E0B",
    "font": "'Hind Siliguri','Noto Sans Bengali','Nirmala UI','Vrinda','Segoe UI',Arial,sans-serif",
}

MAX_SYSTEM_CHARS = 16_000
MAX_MODEL_SVG_CHARS = 40_000          # PART 3 size cap for model output
PHASE_A_OUTPUT_BUDGET = 24_000        # what we ask the model to stay under (no macros yet)

# Moved here from the Live tool description (it used to be re-sent with every Live request).
DRAWING_STANDARD = (
    "DRAWING STANDARD (textbook quality like an HSC/NCTB book figure - NEVER a rough sketch of 2-3 shapes):\n"
    "1. viewBox='0 0 1000 660'; the subject covers at least 55% of the canvas.\n"
    "2. Real anatomy with smooth curved <path> outlines (C/Q/S commands), not plain rect/ellipse for organic things. "
    "Add depth with <radialGradient>/<linearGradient> fills, a light highlight, darker edge shading and fine texture lines.\n"
    "3. Include EVERY structure the standard textbook figure shows (8+ labelled parts for biology) and the internal detail, not just the outline.\n"
    "4. Labels go OUTSIDE the drawing in a left or right column, each joined by a thin leader <polyline> "
    "(stroke #38BDF8, width 1.4) that ends ON the part with a small dot. Never write labels on top of the shape; "
    "never let two labels overlap; keep all text inside x 16..984 and y 16..644.\n"
    "5. Label text: English name bold #F8FAFC size 16, Bangla name below in #FBBF24 size 14, optional short Bangla note #94A3B8 size 12. "
    "Never smaller than 12.\n"
    "6. For microscopic detail add a magnified inset (rounded <rect> panel, dashed circle on the source, dashed connector).\n"
    "7. Natural colours for the subject on the dark board (cream mushroom, green chloroplast, red/blue blood). "
    "No pure white or pure black fills for subjects.\n"
    "8. Soft shadows are allowed via <filter> (feGaussianBlur, feOffset, feMerge, feDropShadow).\n"
)

STYLE_GUIDES: Dict[str, str] = {
    "biology": (
        "BIOLOGY: draw only what the standard NCTB textbook figure shows. If unsure a structure exists, omit it. "
        "Keep relative positions and connections correct (gills under the cap; left ventricle wall thicker than right). "
        "Show flow with arrows (red = oxygenated, blue = deoxygenated)."
    ),
    "physics": (
        "PHYSICS: standard symbols and conventional orientation. Arrows show direction (current, force, ray). "
        "Label units. Keep geometry exact when numbers are given. For a circuit put each parallel branch on its "
        "own horizontal line between two vertical rails and never short a source."
    ),
    "chemistry": (
        "CHEMISTRY: standard apparatus symbols and bond notation. Label reagents and states. "
        "Keep atom positions and angles conventional."
    ),
    "math": (
        "MATH: exact geometry. Mark given lengths, angles and points. Use thin construction lines, bold final figure."
    ),
    "flow": (
        "FLOWCHART: rounded rectangles for steps, diamonds for decisions, arrows with markers. "
        "Top-to-bottom or left-to-right, equal spacing, no crossing arrows when avoidable. "
        "Every node is one short Bangla label with the English term in brackets if needed."
    ),
}

_KIND_TO_DOMAIN = {
    "diagram": "biology", "circuit": "physics", "flow": "flow", "widget": "physics", "graph": "math",
}

_BASE_SYSTEM = (
    "You are the illustrator of a study app for Bangladeshi HSC students (NCTB textbooks). You return ONE SVG that "
    "looks like a premium textbook figure on a dark board. Output only the <svg> element: no prose, no markdown fences, no comments.\n\n"
    "CANVAS & OUTPUT CONTRACT\n"
    "- <svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 1000 660\"> and fill it. Keep the whole output under {budget} characters.\n"
    "- Allowed elements: svg g defs path line polyline polygon rect circle ellipse text tspan linearGradient radialGradient stop "
    "clipPath mask pattern marker symbol use filter(feGaussianBlur feOffset feMerge feMergeNode feDropShadow) title desc.\n"
    "- Forbidden: script style image foreignObject a animate on* attributes, external URLs, DOCTYPE/ENTITY. Style with attributes only. "
    "Escape & as &amp;. Never put label text inside a shape narrower than the text.\n"
    "- Background first: <rect width=\"1000\" height=\"660\" rx=\"14\" fill=\"{bg}\"/>.\n"
    "- Put font-family=\"{font}\" on the <svg>.\n"
    "- Colours: text EN {text}, text BN {text_bn}, muted {muted}, leader lines {accent}, panels {panel}.\n\n"
    "METHOD - think like an illustrator, back to front: background, soft ground shadow, main silhouette as curved paths, "
    "internal structures, texture lines, rim light (thin lighter strokes on lit edges), inset for microscopic detail, "
    "labels, title.\n\n"
    "{standard}\n{domain}\n\n"
    "ACCURACY IS MOST IMPORTANT: every part in the plan or brief must be visible and labelled, and each leader line must end "
    "inside or on that part. Draw only what the textbook figure shows.\n"
    "If you are given a list of LINT ISSUES, change only what is needed to fix them and return the complete corrected <svg>."
)


def illustrator_system(kind: str = "diagram") -> str:
    """System prompt for the draw call. Hard-asserts the router payload guard."""
    domain = STYLE_GUIDES.get(_KIND_TO_DOMAIN.get(str(kind or "").lower(), "biology"), "")
    text = _BASE_SYSTEM.format(
        budget=PHASE_A_OUTPUT_BUDGET, standard=DRAWING_STANDARD, domain=domain, **{
            k: TOKENS[k] for k in ("bg", "text", "text_bn", "muted", "accent", "panel", "font")})
    assert len(text) < MAX_SYSTEM_CHARS, f"illustrator prompt too long: {len(text)}"
    return text
