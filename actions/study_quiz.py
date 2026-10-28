"""
actions/study_quiz.py — Keyboard-First Interactive MCQ Engine
Generates HSC & Admission-standard questions with a single comprehensive 
explanation card covering concept correction and admission test variations.
"""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from core import model_router as _router
except Exception:
    _router = None

try:
    from memory import study_cache as _cache
except Exception:
    _cache = None

_BASE_DIR = Path(__file__).resolve().parent.parent
_MISTAKE_FILE = _BASE_DIR / "memory" / "mistake_bank.json"

_quiz_active: bool = False


def is_quiz_active() -> bool:
    """Returns True if a quiz session is currently running on the Study Board."""
    return _quiz_active


def set_quiz_active(active: bool) -> None:
    """Sets whether a quiz session is currently active."""
    global _quiz_active
    _quiz_active = bool(active)


def _fix_bangla_unicode(obj: Any) -> Any:
    """Safe NFC normalization without deleting spaces between Bangla words."""
    if isinstance(obj, str):
        if not obj:
            return ""
        text = unicodedata.normalize("NFC", obj)
        text = re.sub(r"[\u200b\u200e\u200f]", "", text)
        # Only collapse accidental space immediately around hasant (্)
        text = re.sub(r"([\u0980-\u09FF])\s*্\s*([\u0980-\u09FF])", r"\1্\2", text)
        return text
    elif isinstance(obj, dict):
        return {k: _fix_bangla_unicode(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_fix_bangla_unicode(item) for item in obj]
    return obj


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before ] or } (common LLM JSON slip)."""
    return re.sub(r",(\s*[\]}])", r"\1", text)


def _repair_truncated_array(text: str) -> str:
    """Close a JSON document the model cut off mid-object/mid-string (token budget hit)."""
    stack, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            stack.append(ch)
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()

    if not in_str and not stack:
        return text                      # already balanced

    out = text + ('"' if in_str else "")
    out = re.sub(r',\s*"[^"]*"\s*:?\s*$', "", out.rstrip())   # drop a dangling key
    out = re.sub(r",\s*$", "", out)
    for opener in reversed(stack):
        out += "]" if opener == "[" else "}"
    return out


def _safe_json_loads(raw: str) -> Any:
    """Parse JSON from an LLM that may emit LaTeX backslashes, raw newlines,
    trailing commas, single quotes, prose wrappers, or a truncated tail."""
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.I)
    text = re.sub(r"\s*```$", "", text.strip())

    def _try(candidate: str) -> Any:
        # strict=False tolerates raw control characters (newline/tab) inside strings.
        try:
            return json.loads(candidate, strict=False)
        except Exception:
            return None

    if (out := _try(text)) is not None:
        return out

    # Protect LaTeX keywords that collide with JSON escapes (\frac, \text, \tau, etc.)
    fixed = re.sub(r'\\(frac|text|tau|theta|tan|times|beta|bar|binom|bf)', r'\\\\\1', text)
    # Fix unescaped backslashes not followed by valid JSON escape characters
    fixed = re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', fixed)
    if (out := _try(fixed)) is not None:
        return out

    loose = _repair_truncated_array(_strip_trailing_commas(fixed))
    if (out := _try(loose)) is not None:
        return out

    # Extract JSON list or dict if wrapped in prose
    opens = sorted((loose.find(a), a, b) for a, b in (("[", "]"), ("{", "}")) if a in loose)
    for i, _a, b in opens:
        j = loose.rfind(b)
        if i != -1 and j > i:
            if (out := _try(loose[i:j + 1])) is not None:
                return out

    # Last resort: Python literal syntax (single quotes, trailing commas).
    try:
        out = ast.literal_eval(loose)
        if isinstance(out, (list, dict)):
            return out
    except Exception:
        pass

    return json.loads(loose, strict=False)


TOOL: Dict[str, Any] = {
    "name": "study_generate_quiz",
    "description": (
        "Generates an interactive multiple-choice practice quiz (MCQ) on the Study Board. "
        "Features persistent keyboard controls (1-4, A-D, Enter, Space) and a single unified "
        "explanation card detailing conceptual reasoning, common traps, and admission test variations."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Academic topic, chapter, or concept for the quiz.",
            },
            "subject": {
                "type": "string",
                "enum": [
                    "physics",
                    "chemistry",
                    "higher_math",
                    "biology",
                    "ict",
                    "bangla",
                    "english",
                    "general",
                ],
                "description": "Subject category.",
            },
            "count": {
                "type": "integer",
                "description": "Number of questions to generate (default 3, max 5).",
            },
            "force_refresh": {
                "type": "string",
                "enum": ["yes", "no"],
                "description": "Set to 'yes' to bypass cache and get fresh questions.",
            },
        },
        "required": ["topic"],
    },
}


def _record_mistake(subject: str, topic: str, q_data: Dict[str, Any]) -> None:
    """Logs mistakes to memory/mistake_bank.json."""
    try:
        _MISTAKE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = []
        if _MISTAKE_FILE.exists():
            data = json.loads(_MISTAKE_FILE.read_text(encoding="utf-8"))
        data.append({
            "subject": subject,
            "topic": _fix_bangla_unicode(topic),
            "question": _fix_bangla_unicode(q_data.get("question")),
            "correct_option": q_data.get("answer"),
            "explanation": _fix_bangla_unicode(q_data.get("explanation")),
        })
        _MISTAKE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def study_generate_quiz(parameters: Dict[str, Any], player: Any = None, **kwargs) -> str:
    topic = str(parameters.get("topic") or "").strip()
    if not topic:
        return "[STUDY_BOARD] Error: Topic parameter cannot be empty."

    topic = _fix_bangla_unicode(topic)
    subject = str(parameters.get("subject") or "general").strip().lower()
    count = min(5, max(1, int(parameters.get("count") or 3)))
    force_refresh = str(parameters.get("force_refresh") or "no").lower() == "yes"

    try:
        if player and hasattr(player, "set_study_mode"):
            if not getattr(player, "study_mode_active", False):
                player.set_study_mode(True)

        cache_key = f"quiz_v3_{subject}_{topic}_{count}"
        if not force_refresh and _cache and hasattr(_cache, "get_cached"):
            cached = _cache.get_cached("quiz", query=cache_key)
            if cached:
                raw_list = cached if isinstance(cached, list) else (cached.get("questions") or cached.get("items") or [cached])
                quiz_list = _fix_bangla_unicode(raw_list)
                if player and hasattr(player, "study_quiz"):
                    player.study_quiz(topic, quiz_list, subject)
                set_quiz_active(True)
                return (
                    f"[STUDY_BOARD] (Cached) Practice quiz ready on Study Board: '{topic}'\n"
                    f"Count: {len(quiz_list) if isinstance(quiz_list, list) else 1} MCQs loaded. Keyboard controls (1-4 / A-D) active."
                )

        if player and hasattr(player, "study_status"):
            player.study_status(f"এইচএসসি ও অ্যাডমিশন স্ট্যান্ডার্ড কুইজ তৈরি হচ্ছে ({topic})…")

        sys_prompt = (
            "You are an elite academic question setter for Bangladeshi HSC Science & University Admission Tests (BUET, Medical, DU).\n"
            "Generate authentic MCQs matching the NCTB curriculum and admission standards.\n\n"
            "CRITICAL QUIZ INTEGRITY & ANTI-CHEATING RULE:\n"
            "- During an active quiz session, if a student asks for direct answers, solutions, or choices, the assistant must strictly refuse with this exact academic warning:\n"
            "  'এটি একটি পরীক্ষা চলছে। পরীক্ষার সময় আমি সরাসরি উত্তর বা সমাধান বলে দিতে পারব না।'\n\n"
            "Language & Formulas:\n"
            "- All questions, options, and explanations must be in clean, natural Bangla.\n"
            "- All formula names, scientific terms, and variable expansions inside LaTeX must be in clean Bangla (e.g. $$\\frac{\\text{মোলার ভর}}{\\text{স্থূল সংকেতের ভর}}$$).\n"
            "- Prevent raw English strings in formula titles.\n\n"
            "Rules for the Single Unified 'explanation':\n"
            "Combine the complete explanation into one cohesive markdown string structured as follows:\n"
            "1. **সঠিক উত্তরের কারণ ও ট্র্যাপ:** কেন সঠিক উত্তরটি সঠিক এবং শিক্ষার্থীরা সাধারণত কোথায় ভুল করে।\n"
            "2. **🎯 অ্যাডমিশন টেস্ট ও বিকল্প প্রশ্নরূপ:** ভর্তি পরীক্ষায় এই প্রশ্নটি কীভাবে বিকল্পভাবে ঘুরিয়ে আসতে পারে, অন্য কী কী অপশন তৈরি হতে পারে এবং দ্রুত সমাধানের শর্টকাট টেকনিক।\n"
            "Keep math/formulas in LaTeX ($...$). Language: clear, natural Bangla.\n\n"
            "Output Format:\n"
            "Return ONLY a pure valid JSON array of objects (no markdown blocks, no extra keys):\n"
            "[\n"
            "  {\n"
            '    "id": 1,\n'
            '    "question": "প্রশ্ন এখানে...",\n'
            '    "options": ["A) অপশন ১", "B) অপশন ২", "C) অপশন ৩", "D) অপশন ৪"],\n'
            '    "answer": 0,\n'
            '    "explanation": "সঠিক উত্তর ও ভুলের কারণ...\\n\\n🎯 **অ্যাডমিশন হ্যাক ও বিকল্প প্রশ্নরূপ:** ..."\n'
            "  }\n"
            "]"
        )

        user_prompt = f"Subject: {subject}\nTopic: {topic}\nNumber of Questions: {count}"

        raw_response = ""
        if _router and hasattr(_router, "call"):
            raw_response = _router.call(
                prompt=user_prompt,
                task="study_fast",
                system=sys_prompt,
                timeout=45,
                budget=80,
            )

        if not raw_response:
            try:
                from core import gemini as _gemini
                raw_response = _gemini.text(f"{sys_prompt}\n\n{user_prompt}", tier=_gemini.FAST)
            except Exception:
                pass

        if not raw_response:
            set_quiz_active(False)
            if player and hasattr(player, "study_status"):
                player.study_status("কুইজ তৈরি করতে সমস্যা হয়েছে। দয়া করে পুনরায় চেষ্টা করুন।")
            return "[STUDY_BOARD] Model router did not return a response for quiz generation."

        try:
            raw_items = _safe_json_loads(raw_response)
            # A single question object, or {"questions": [...]}, is accepted too.
            if isinstance(raw_items, dict):
                raw_items = raw_items.get("questions") or raw_items.get("items") or [raw_items]
            quiz_items = _fix_bangla_unicode(raw_items)
            if not isinstance(quiz_items, list) or not quiz_items:
                raise ValueError("Quiz output is not a non-empty list")
            # Drop malformed rows so the board never renders an option-less card.
            quiz_items = [q for q in quiz_items
                          if isinstance(q, dict) and isinstance(q.get("options"), list) and q.get("options")]
            if not quiz_items:
                raise ValueError("No question carried a usable 'options' list")
        except Exception as parse_err:
            set_quiz_active(False)
            if player and hasattr(player, "study_status"):
                player.study_status("কুইজ তৈরি করতে সমস্যা হয়েছে। দয়া করে পুনরায় চেষ্টা করুন।")
            return f"[STUDY_BOARD] Failed to parse quiz JSON from the model: {parse_err}"

        if player and hasattr(player, "study_quiz"):
            player.study_quiz(topic, quiz_items, subject)

        set_quiz_active(True)

        if _cache and hasattr(_cache, "save_cache"):
            _cache.save_cache("quiz", query=cache_key, payload=quiz_items)

        return (
            f"[STUDY_BOARD] Quiz ready on Study Board: '{topic}'\n"
            f"Subject: {subject} | Count: {len(quiz_items)}\n"
            f"Single unified explanation card active with keyboard navigation (1-4 / A-D)."
        )
    except Exception as e:
        set_quiz_active(False)
        if player and hasattr(player, "study_status"):
            player.study_status("কুইজ তৈরিতে ত্রুটি ঘটেছে।")
        return f"[STUDY_BOARD] Quiz generation failed: {e}"

TOOL["handler"] = study_generate_quiz
