# -*- coding: utf-8 -*-
"""
actions/study_visualizer.py - Renders circuits, biology/physics figures, architecture diagrams and 2D graphs
directly onto the embedded JARVIS Study Station Board (`runtime/study_board.html`).

HOW A DIAGRAM IS PRODUCED (v3 - JARVIS draws on its own)
  1. diagrams/ library      hand-finished textbook figures (actions/diagram_library.py)   -> instant
  2. diagrams/generated/    figures JARVIS drew earlier and kept (actions/diagram_forge.py)   -> instant
  3. the illustrator        actions/diagram_forge.py: plan -> draw -> lay out labels -> self-check -> repair.
                            Runs in a BACKGROUND thread so the voice never freezes; the board shows live
                            progress and the model gets a system notice when the figure is ready.
  4. graphs                 coord_spec_json / plot_spec_json are drawn directly by the board.
  Fallbacks: if the illustrator is off or fails, `svg_markup` (if the model wrote one) is shown instead.

Pipeline: Gemini tool call -> execute() -> player.study_solution(..., reveal="all")
          -> ui._study_post -> StudyCanvasWidget._post("render") -> board ops.render()
          -> blocks(): ```svg / ```coord / ```plot fences.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # the vault is a nice-to-have: its failure must never hide the tool itself
    from core.study_database import save_study_artifact
except Exception:  # pragma: no cover
    save_study_artifact = None

_JOB_LOCK = threading.Lock()
_JOB_ID = 0


# ----------------------------------------------------------------------------------------------
# helper modules (loaded lazily, never raise)
# ----------------------------------------------------------------------------------------------
def _load_helper(name: str):
    try:
        import importlib
        return importlib.import_module(f"actions.{name}")
    except Exception:
        pass
    try:
        import importlib.util
        path = Path(__file__).with_name(f"{name}.py")
        spec = importlib.util.spec_from_file_location(f"actions.{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _library():
    return _load_helper("diagram_library")


def _forge():
    mod = _load_helper("diagram_forge")
    return mod if mod is not None and mod.enabled() else None


# ----------------------------------------------------------------------------------------------
# small utilities
# ----------------------------------------------------------------------------------------------
def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on", "হ্যাঁ")


def _clean(value: Any) -> str:
    """Strip a wrapping ```lang fence the model sometimes adds, and any stray ```
    that would close our own fence early (and let content inject new fences)."""
    s = str(value or "").strip()
    s = re.sub(r"^```[a-zA-Z0-9_-]*[ \t]*\r?\n?", "", s)
    s = re.sub(r"\r?\n?```\s*$", "", s)
    return s.replace("```", "").strip()


def _json_object(raw: Any) -> str:
    s = re.sub(r",(\s*[\]}])", r"\1", _clean(raw))
    data = json.loads(s)
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object")
    return json.dumps(data, ensure_ascii=False)


def _plain(md: str) -> str:
    return re.sub(r"[*_`>]|^\s*-\s+", "", md or "", flags=re.M).strip()


def _tell(speak, text: str) -> None:
    """Hand the voice model a system notice (best effort)."""
    if callable(speak):
        try:
            speak(text)
        except Exception as e:
            print(f"[StudyVisualizer] could not notify the voice model: {e}")


def _status(player, text: str) -> None:
    try:
        if player is not None and hasattr(player, "study_status"):
            player.study_status(text)
    except Exception as e:
        print(f"[StudyVisualizer] status update failed: {e}")


def _ensure_study_mode(player) -> None:
    try:
        if hasattr(player, "set_study_mode") and not getattr(player, "study_mode_active", False):
            player.set_study_mode(True, notify_voice=False)
    except Exception as e:
        print(f"[StudyVisualizer] study mode switch failed: {e}")


def _push(player, title: str, markdown: str, subject: str, visual_type: str) -> Optional[str]:
    """Save to the vault and show on the board. Returns an error text or None."""
    if save_study_artifact is not None:
        try:
            save_study_artifact(artifact_type=f"visual_{visual_type}", subject=subject, topic=title,
                                payload={"visual_type": visual_type, "title": title, "markdown": markdown})
        except Exception as e:
            print(f"[StudyVisualizer] Vault log warning: {e}")
    if player is None or not hasattr(player, "study_solution"):
        return "the Study Board is not available right now"
    try:
        _ensure_study_mode(player)
        player.study_solution(title, markdown, subject, reveal="all")
    except Exception as e:
        print(f"[StudyVisualizer] UI board push error: {e}")
        return f"pushing the diagram to the board failed ({e})"
    return None


def _assemble(title: str, blocks: List[Dict[str, str]], notes_md: str, explanation_bn: str) -> str:
    parts = [f"## চিত্র ও ডায়াগ্রাম: {title}"]
    for i, b in enumerate(blocks):
        if i:
            parts.append(f"## {b.get('caption') or 'বর্ধিত অংশ'}")
        parts.append(f"```svg\n{_clean(b['svg'])}\n```")
    if notes_md:
        parts.append(f"## চিত্রের অংশগুলো\n{notes_md}")
    if explanation_bn:
        parts.append(f"## বিস্তারিত ব্যাখ্যা ও গাণিতিক বিশ্লেষণ\n{explanation_bn}")
    return "\n\n".join(parts)


def _facts(plan: Dict[str, Any], notes_md: str) -> str:
    names = [f"{p.get('bn') or ''} ({p.get('en')})" for p in (plan.get("parts") or [])][:12]
    return "; ".join(names) if names else _plain(notes_md)[:700]


def _current_job() -> int:
    with _JOB_LOCK:
        return _JOB_ID


# ----------------------------------------------------------------------------------------------
# the background illustrator job
# ----------------------------------------------------------------------------------------------
def _run_job(job_id: int, forge, player, speak, *, title: str, subject: str, brief: str,
             visual_type: str, explanation_bn: str, fallback_svg: str) -> None:
    def alive() -> bool:
        return job_id == _current_job()

    def progress(msg: str) -> None:
        if alive():
            _status(player, f"🎨 {title}\n\nJARVIS নিজে চিত্রটি আঁকছে — একটু অপেক্ষা করো।\n\n{msg}")

    try:
        res = forge.forge(brief, title, subject, progress=progress, cancelled=lambda: not alive())
        if not alive():
            return
        if res.get("ok"):
            md = _assemble(title, res["blocks"], res.get("notes_md", ""), explanation_bn)
            err = _push(player, title, md, subject, visual_type)
            if err is None:
                if not forge.needs_fix(res.get("issues") or []):
                    forge.cache_store(title, subject, res)
                _tell(speak, (
                    f"[SYSTEM NOTICE - this is not the student speaking] The diagram '{title}' that you started is now "
                    "displayed on the Study Board. In Bengali, tell the student in 3-4 short sentences that it is ready "
                    "and walk through the main labelled parts. Parts: "
                    f"{_facts(res.get('plan') or {}, res.get('notes_md', ''))}. "
                    "Do not mention files or paths and do not call any tool."))
                return
            res = {"ok": False, "error": err}
        reason = str(res.get("error") or "unknown error")[:240]
        print(f"[StudyVisualizer] illustrator failed: {reason}")
        if fallback_svg:
            md = _assemble(title, [{"svg": fallback_svg}], "", explanation_bn)
            if _push(player, title, md, subject, visual_type) is None:
                _tell(speak, f"[SYSTEM NOTICE - not the student] The detailed illustrator failed, so a simpler version of "
                             f"'{title}' was put on the board. Tell the student in one Bengali sentence that it is a simple "
                             "version and they can say 'আবার আঁকো' to retry.")
                return
        _status(player, f"⚠ '{title}' চিত্রটি বানানো যায়নি।\n\nকারণ: {reason}\n\nআবার বলো (\"আবার আঁকো\"), অথবা একটু পরে চেষ্টা করো।")
        _tell(speak, f"[SYSTEM NOTICE - not the student] Drawing '{title}' failed ({reason}). Tell the student in one short "
                     "Bengali sentence that it could not be drawn this time and offer to try again.")
    except Exception as e:                                           # noqa: BLE001
        print(f"[StudyVisualizer] job crashed: {type(e).__name__}: {e}")
        _status(player, f"⚠ '{title}' আঁকতে গিয়ে সমস্যা হয়েছে: {e}")


# ----------------------------------------------------------------------------------------------
# the tool
# ----------------------------------------------------------------------------------------------
def execute(
    parameters: dict | None = None,
    player=None,
    speak=None,
    **kwargs,
) -> str:
    global _JOB_ID
    params = dict(parameters or {})
    visual_type = _one_line(params.get("visual_type")) or "circuit_or_diagram_svg"
    title = _one_line(params.get("title")) or "Visual Diagram"
    subject = _one_line(params.get("subject")) or "Physics & Math"
    explanation_bn = str(params.get("explanation_bn") or "").strip()
    brief = str(params.get("diagram_brief") or "").strip()
    redraw = _truthy(params.get("redraw"))
    wants_graph = bool(str(params.get("coord_spec_json") or "").strip()
                       or str(params.get("plot_spec_json") or "").strip()
                       or visual_type in ("coordinate_grid", "plotly_chart"))
    svg_markup = _clean(params.get("svg_markup"))
    if svg_markup:
        s, e = svg_markup.find("<svg"), svg_markup.rfind("</svg>")
        svg_markup = svg_markup[s:e + 6] if s != -1 and e > s else ""

    # ---- 1. hand-finished library ---------------------------------------------------------
    template = None
    asked_id = _one_line(params.get("template_id"))
    lib = _library()
    if lib is not None:
        if asked_id:
            template = lib.get(asked_id)
        if template is None and not wants_graph and not redraw:
            guess = lib.match(title, asked_id)
            if guess:
                template = lib.get(guess)
    if asked_id and template is None and not svg_markup and not brief and not wants_graph:
        known = ", ".join(e["id"] for e in (lib.catalog() if lib else [])) or "none"
        return (f"[STUDY_BOARD] Error: template_id '{asked_id}' does not exist (available: {known}). NOTHING was drawn. "
                "Call study_visualizer again without template_id and pass diagram_brief instead; do not tell the student "
                "it is displayed.")

    if template is not None:
        blocks = [{"svg": template["svg"]}]
        md = _assemble(title, blocks, template.get("notes_bn", ""), explanation_bn)
        if template.get("subject") and subject in ("Physics & Math", ""):
            subject = template["subject"]
        err = _push(player, title, md, subject, visual_type)
        if err:
            return f"[STUDY_BOARD] Error: {err}. The diagram was NOT displayed. Tell the student briefly."
        return (f"[STUDY_BOARD] The finished textbook diagram '{template['title_en']}' is now displayed on the Study Station "
                "Board with labelled parts. Explain it in Bengali in 3-5 sentences: walk through the main labelled parts in "
                f"order and say what each does. Facts you may use: {_plain(template.get('notes_bn', ''))[:900]} "
                "Do NOT mention any file or path.")

    # ---- 2. JARVIS's own illustrator (cache first) ----------------------------------------
    forge = None if wants_graph else _forge()
    if forge is not None:
        if redraw:
            forge.cache_forget(title)
        else:
            cached = forge.cache_lookup(title)
            if cached:
                md = _assemble(title, cached["blocks"], cached.get("notes_md", ""), explanation_bn)
                err = _push(player, title, md, subject, visual_type)
                if err is None:
                    return (f"[STUDY_BOARD] The diagram '{title}' (drawn earlier by JARVIS) is now displayed on the Study "
                            "Station Board. Explain it clearly in Bengali in 3-4 sentences using these facts: "
                            f"{_facts(cached.get('plan') or {}, cached.get('notes_md', ''))}. "
                            "If the student says it looks wrong, call study_visualizer again with redraw=true. "
                            "Do NOT mention any file or path.")
        with _JOB_LOCK:
            _JOB_ID += 1
            job_id = _JOB_ID
        _ensure_study_mode(player)
        _status(player, f"🎨 {title}\n\nJARVIS নিজে চিত্রটি আঁকছে — একটু অপেক্ষা করো।")
        job_brief = brief or f"{title}. {explanation_bn[:300]}".strip()
        threading.Thread(
            target=_run_job, args=(job_id, forge, player, speak), daemon=True, name="jarvis-illustrator",
            kwargs=dict(title=title, subject=subject, brief=job_brief, visual_type=visual_type,
                        explanation_bn=explanation_bn, fallback_svg=svg_markup),
        ).start()
        return (f"[STUDY_BOARD] Drawing started: JARVIS's illustrator is now drawing '{title}' on the Study Board in the "
                "background (about 20-60 seconds; progress is shown on the board). Tell the student in ONE short Bengali "
                "sentence that you are drawing it and it will appear on the board shortly. Do NOT describe the diagram yet "
                "and do NOT call study_visualizer again for it - you will receive a system notice when it is ready.")

    # ---- 3. direct drawing: model-written svg and graph specs -----------------------------
    md_parts = [f"## চিত্র ও ডায়াগ্রাম: {title}"]
    drawn = 0
    problems: List[str] = []
    if svg_markup:
        md_parts.append(f"```svg\n{svg_markup}\n```")
        drawn += 1
    elif str(params.get("svg_markup") or "").strip():
        problems.append("svg_markup must be ONE complete <svg ...>...</svg> element")
    for key, fence in (("coord_spec_json", "coord"), ("plot_spec_json", "plot")):
        if not str(params.get(key) or "").strip():
            continue
        try:
            md_parts.append(f"```{fence}\n{_json_object(params.get(key))}\n```")
            drawn += 1
        except Exception as e:
            problems.append(f"{key} is not a valid JSON object ({e})")
    if not drawn:
        why = "; ".join(problems) or "nothing to draw - pass diagram_brief (figures), coord_spec_json or plot_spec_json (graphs)"
        return (f"[STUDY_BOARD] Error: {why}. NOTHING was drawn on the board. Call study_visualizer again now with corrected "
                "arguments, and do not tell the student it is displayed.")
    if explanation_bn:
        md_parts.append(f"## বিস্তারিত ব্যাখ্যা ও গাণিতিক বিশ্লেষণ\n{explanation_bn}")
    err = _push(player, title, "\n\n".join(md_parts), subject, visual_type)
    if err:
        return f"[STUDY_BOARD] Error: {err}. It was NOT displayed. Tell the student briefly that something went wrong."
    note = f" (Skipped: {'; '.join(problems)}.)" if problems else ""
    return (f"[STUDY_BOARD] The diagram '{title}' ({visual_type}) is now displayed on the Study Station Board! "
            "Explain it clearly in Bengali in 2-4 sentences. Do NOT mention any file or path." + note)


# ----------------------------------------------------------------------------------------------
# Tool declaration (read by Gemini to route and to write the arguments)
# ----------------------------------------------------------------------------------------------
def _build_description() -> str:
    lib = _library()
    entries = lib.catalog() if lib is not None else []
    ready = ""
    if entries:
        listing = "; ".join(f"{e['id']} = {e['title_en']}" for e in entries)
        ready = f"READY-MADE FIGURES (pass only template_id): {listing}. "
    return (
        "Show a diagram, figure, circuit, schematic or graph on the Study Station Board. "
        "CRITICAL: whenever the student asks to see/draw/explain with ANY diagram, figure, circuit, structure, cycle, "
        "flowchart or graph, call this tool IMMEDIATELY in the same turn - never just promise to draw it. "
        + ready +
        "FOR EVERYTHING ELSE (biology structures, cells, organs, apparatus, circuits, ray/force diagrams, flowcharts, "
        "life cycles ...) pass `diagram_brief`: one or two sentences saying what to draw plus the parts the HSC/NCTB "
        "textbook figure labels, in English (e.g. 'Human heart, internal view: four chambers, valves, aorta, vena cava, "
        "pulmonary vessels'). JARVIS's own illustrator then plans, draws, labels, checks and repairs the figure in the "
        "background (20-60 s). Do NOT write svg_markup yourself and keep the call short. "
        "For function graphs/coordinate geometry pass `coord_spec_json`; for data charts `plot_spec_json`. "
        "If the student says an earlier figure is wrong, ugly or incomplete, call again with `redraw`=true."
    )


TOOL: Dict[str, Any] = {
    "name": "study_visualizer",
    "description": _build_description(),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "visual_type": {"type": "STRING", "description": "circuit_or_diagram_svg | coordinate_grid | plotly_chart"},
            "title": {"type": "STRING", "description": "Short title of the figure (e.g. 'Human Heart - Internal Structure', 'Parallel Resistor Circuit')"},
            "subject": {"type": "STRING", "description": "Subject tag (e.g. 'Physics', 'Higher Math', 'ICT', 'Biology')"},
            "diagram_brief": {
                "type": "STRING",
                "description": ("What to draw and which parts must be labelled, in English, 1-3 sentences. Include numbers/values "
                                "for circuits (e.g. '3 ohm, 4 ohm, 5 ohm in parallel across a 10 V battery, with ammeter')."),
            },
            "template_id": {"type": "STRING", "description": "Id from the READY-MADE FIGURES list in the tool description, if the topic matches."},
            "redraw": {"type": "BOOLEAN", "description": "true when the student wants an earlier figure of the same topic drawn again from scratch."},
            "coord_spec_json": {
                "type": "STRING",
                "description": (
                    "Optional JSON string for 2D Cartesian coordinate grid & geometry. Schema: "
                    '{"x_range":[-10,10],"y_range":[-10,10],'
                    '"functions":[{"expr":"x*x - 4","label":"y = x^2 - 4","color":"#38BDF8"}],'
                    '"points":[[0,0,"O(0,0)"],[3,4,"P(3,4)"]], '
                    '"segments":[[[0,0],[3,4],"r = 5"]]}. '
                    "expr may use x, + - * / ^, ( ), pi, e and sin cos tan sqrt abs exp ln log(base 10)."
                ),
            },
            "plot_spec_json": {
                "type": "STRING",
                "description": (
                    "Optional JSON string for a Plotly bar/line/scatter chart. Schema: "
                    '{"title":"...","x":[1,2,3],"y":[4,5,6],"type":"scatter","xlabel":"...","ylabel":"..."} '
                    'or {"labels":["A","B"],"values":[3,5],"type":"bar"}.'
                ),
            },
            "svg_markup": {
                "type": "STRING",
                "description": "Normally leave EMPTY - the illustrator draws. Only a complete <svg viewBox='0 0 1000 660'>...</svg> if you must draw it yourself.",
            },
            "explanation_bn": {
                "type": "STRING",
                "description": ("Optional for figures (JARVIS writes the part list itself). For graphs/maths: step-by-step Bengali "
                                "explanation with LaTeX formulas ($...$ or $$...$$) in English."),
            },
        },
        "required": ["visual_type", "title"],
    },
    "handler": execute,
}

# Compatibility exports for any loader variant
run = execute
