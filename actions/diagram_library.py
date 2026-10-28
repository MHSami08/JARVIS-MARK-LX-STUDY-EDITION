# -*- coding: utf-8 -*-
"""
actions/diagram_library.py - library of hand-finished, textbook-grade diagrams.

This is a HELPER module (no TOOL dict, so the action loader ignores it).
`study_visualizer` uses it: when a student asks for a diagram that exists here, the
finished artwork is shown instead of letting the voice model improvise a rough sketch.

Where the artwork lives:  <project>/diagrams/index.json  +  <project>/diagrams/<id>.svg

Adding a diagram needs no code:
  1. drop  diagrams/<id>.svg   (one complete <svg viewBox="0 0 1000 660"> ... </svg>)
  2. add an entry to diagrams/index.json:
        "<id>": {"title_en": "...", "title_bn": "...", "subject": "Biology",
                 "keywords": ["english word", "বাংলা শব্দ"], "notes_bn": "markdown, optional"}
The list is re-read on every request, so no restart is needed for matching (restart once
so the model also sees the new id in the tool description).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

DIAGRAM_DIR = _BASE / "diagrams"
_INDEX = DIAGRAM_DIR / "index.json"


def _load_index() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def catalog() -> List[Dict[str, Any]]:
    """[{id, title_en, title_bn, subject, keywords, notes_bn}] for every diagram whose .svg exists."""
    out = []
    for tid, meta in _load_index().items():
        if not isinstance(meta, dict) or not (DIAGRAM_DIR / f"{tid}.svg").is_file():
            continue
        out.append({
            "id": tid,
            "title_en": str(meta.get("title_en") or tid),
            "title_bn": str(meta.get("title_bn") or ""),
            "subject": str(meta.get("subject") or ""),
            "keywords": [str(k) for k in (meta.get("keywords") or [])],
            "notes_bn": str(meta.get("notes_bn") or ""),
        })
    return out


def get(template_id: str) -> Optional[Dict[str, Any]]:
    """The catalog entry plus its 'svg' text, or None."""
    tid = str(template_id or "").strip()
    for entry in catalog():
        if entry["id"] == tid:
            try:
                svg = (DIAGRAM_DIR / f"{tid}.svg").read_text(encoding="utf-8")
            except Exception:
                return None
            if "<svg" not in svg:
                return None
            return dict(entry, svg=svg)
    return None


def match(*texts: Any) -> Optional[str]:
    """Id of the first diagram whose keyword appears in any of the given texts (title, topic ...)."""
    hay = " ".join(str(t or "") for t in texts).lower()
    if not hay.strip():
        return None
    for entry in catalog():
        for kw in entry["keywords"]:
            if kw and kw.lower() in hay:
                return entry["id"]
    return None
