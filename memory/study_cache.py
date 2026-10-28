"""
Zero-Cost Local Study Cache & Mistake Bank for JARVIS Study Station.

1. Local Study Cache (`memory/study_cache.json`):
   Stores generated solutions, quizzes, flashcards, and mindmaps keyed by
   (action, subject, normalized_query). Revisiting a topic costs 0 API tokens
   and loads in <10ms.
2. Mistake Bank (`memory/study_mistakes.json`):
   Stores MCQ questions the student answered incorrectly during interactive
   quizzes so they can practice them offline anytime via action='review_mistakes'.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

CACHE_FILE = _BASE / "memory" / "study_cache.json"
MISTAKES_FILE = _BASE / "memory" / "study_mistakes.json"

_lock = Lock()
_MAX_CACHE_ITEMS = 250
_MAX_MISTAKES = 150


def _norm_key(action: str, subject: str = "", query: str = "") -> str:
    s = (subject or "").strip()
    q = (query or "").strip()
    if not q and s:
        q = s
        s = ""
    clean_q = re.sub(r"\s+", " ", q.lower())
    clean_s = s.lower()
    if clean_s:
        raw = f"{action.lower().strip()}|{clean_s}|{clean_q}"
    else:
        raw = f"{action.lower().strip()}|{clean_q}"
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _read_json(path: Path, default):
    if not path.exists():
        return default
    with _lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        try:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[StudyCache] Write failed ({path.name}): {e}")


def get_cached(
    category: str = "",
    query: str = "",
    subject: str = "",
    *args,
    **kwargs,
) -> dict | list | None:
    """Return cached payload dict/list if present, else None (0 tokens)."""
    action = category or kwargs.get("action") or ""
    q = query or kwargs.get("query_key") or ""
    s = subject or kwargs.get("subject") or ""

    if args:
        if len(args) == 1:
            if not q:
                q = str(args[0])
            elif not s:
                s = str(args[0])
        elif len(args) >= 2:
            s = str(args[0])
            q = str(args[1])

    store = _read_json(CACHE_FILE, {})
    if not isinstance(store, dict):
        return None

    # 1. Primary lookup
    key = _norm_key(action, s, q)
    item = store.get(key)

    # 2. Direct key lookup if q is directly in store
    if item is None and q and q in store:
        item = store.get(q)

    # 3. Swapped subject/query lookup
    if item is None and s and q:
        item = store.get(_norm_key(action, q, s))

    # 4. Fallback: Parse composite key (e.g., mindmap_bn_{subject}_{topic})
    if item is None and q:
        q_str = str(q)
        for pfx in [f"{action}_bn_", f"{action}_v3_", f"{action}_", "mindmap_bn_", "quiz_v3_", "solve_", "flashcards_bn_"]:
            if q_str.startswith(pfx):
                remainder = q_str[len(pfx):]
                parts = remainder.split("_", 1)
                if len(parts) == 2:
                    sub_cand, topic_cand = parts[0], parts[1]
                    topic_cand_clean = re.sub(r"_\d+$", "", topic_cand)
                    for t in [topic_cand, topic_cand_clean, f"{topic_cand_clean}_bn_v2", f"{topic_cand}_bn_v2"]:
                        cand_key = _norm_key(action, sub_cand, t)
                        if cand_key in store:
                            item = store[cand_key]
                            break
            if item is not None:
                break

    if not item or not isinstance(item, (dict, list)):
        return None

    # Quiz compatibility: if store contains a wrapper dict with questions/items
    if action == "quiz" and isinstance(item, dict):
        if "questions" in item and isinstance(item["questions"], list):
            return item["questions"]
        if "items" in item and isinstance(item["items"], list):
            return item["items"]

    if isinstance(item, dict) and "items" in item and len(item) <= 3 and "cached_at" in item:
        return item["items"]

    return item


def save_cache(
    action: str = "",
    query: str = "",
    payload: Any = None,
    subject: str = "",
    *args,
    **kwargs,
) -> None:
    """Save generated study payload to disk cache."""
    act = action or kwargs.get("category") or ""
    q = query or kwargs.get("query_key") or ""
    s = subject or kwargs.get("subject") or ""
    p = payload if payload is not None else kwargs.get("payload")

    if args:
        if p is None:
            if len(args) == 1:
                p = args[0]
            elif len(args) == 2:
                q = str(args[0])
                p = args[1]
            elif len(args) >= 3:
                s = str(args[0])
                q = str(args[1])
                p = args[2]

    if not q or p is None:
        return

    key = _norm_key(act, s, q)
    store = _read_json(CACHE_FILE, {})
    if not isinstance(store, dict):
        store = {}

    entry = {
        "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "query_preview": str(q)[:100],
    }
    if isinstance(p, dict):
        entry.update(p)
    elif isinstance(p, list):
        entry["items"] = p
        if act == "quiz":
            entry["questions"] = p
    else:
        entry["data"] = p

    store[key] = entry

    if len(store) > _MAX_CACHE_ITEMS:
        sorted_keys = sorted(store.keys(), key=lambda k: store[k].get("cached_at", "") if isinstance(store[k], dict) else "")
        for k in sorted_keys[: len(store) - _MAX_CACHE_ITEMS]:
            store.pop(k, None)
    _write_json(CACHE_FILE, store)


def add_mistakes(topic: str, mistakes: list[dict]) -> None:
    """Save wrong quiz answers to the Mistake Bank for spaced repetition."""
    if not mistakes:
        return
    bank = _read_json(MISTAKES_FILE, [])
    if not isinstance(bank, list):
        bank = []
    existing_qs = {str(m.get("question", "")).strip().lower() for m in bank}
    now_str = datetime.now().strftime("%Y-%m-%d")

    for m in mistakes:
        q_txt = str(m.get("question") or "").strip()
        opts = m.get("options") or []
        if not q_txt or not opts or q_txt.lower() in existing_qs:
            continue
        ans_idx = m.get("answer_idx")
        if not isinstance(ans_idx, int):
            try:
                ans_idx = opts.index(m.get("answer"))
            except ValueError:
                ans_idx = 0
        bank.append({
            "topic": topic or "General",
            "question": q_txt,
            "options": opts,
            "answer": ans_idx,
            "explain": str(m.get("explain") or ""),
            "added": now_str,
        })
        existing_qs.add(q_txt.lower())

    bank = bank[-_MAX_MISTAKES:]
    _write_json(MISTAKES_FILE, bank)


def get_mistakes(topic_filter: str = "", limit: int = 10) -> list[dict]:
    """Return saved mistake questions for offline practice (0 API cost)."""
    bank = _read_json(MISTAKES_FILE, [])
    if not isinstance(bank, list) or not bank:
        return []
    tf = (topic_filter or "").strip().lower()
    if tf:
        matched = [m for m in bank if tf in str(m.get("topic", "")).lower() or tf in str(m.get("question", "")).lower()]
        if matched:
            bank = matched
    return list(reversed(bank))[: max(1, limit)]