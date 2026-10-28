# -*- coding: utf-8 -*-
"""
actions/study_kb_lookup.py — Smart Textbook Knowledge Base Ingestor & Lookup Tool.

Supports automatic parsing and permanent SQLite indexing (`data/study_vault.db`) of:
  - Markdown (`.md`) & Text (`.txt`, `.rst`)
  - Structured JSON (`.json`) — flexible schemas (chapters, chunks, flashcards, MCQs)
  - Documents (`.pdf`, `.docx`, `.pptx`) via local extractor
  - Textbook Page Images (`.png`, `.jpg`, `.jpeg`, `.webp`) via Gemini Vision OCR
  - Entire Folders (recursively scans and indexes all supported files inside)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from core.study_database import (
    DB_PATH,
    get_textbook_catalog,
    save_study_artifact,
    save_textbook_chunk,
    search_textbook_chunks,
)
from core.security_policy import approve_file_permanently, approve_folder_permanently

ROOT_DIR = Path(__file__).resolve().parent.parent
INBOX_DIR = ROOT_DIR / "data" / "knowledge_inbox"
INBOX_DIR.mkdir(parents=True, exist_ok=True)
try:
    approve_folder_permanently(INBOX_DIR)
except Exception:
    pass

_SUBJECT_KEYWORDS = {
    "Physics": ["পদার্থ", "physics", "তড়িৎ", "বলবিদ্যা", "ভেক্টর", "গতি", "নিউটন", "ভোল্ট", "রোধ", "সার্কিট", "ধারক", "চৌম্বক", "আলোক", "তাপগতিবিদ্যা", "অর্ধপরিবাহী"],
    "Higher Math": ["গণিত", "math", "ক্যালকুলাস", "calculus", "ম্যাট্রিক্স", "নির্ণায়ক", "ত্রিকোণমিতি", "সরলরেখা", "বৃত্ত", "কনিক", "অন্তরীকরণ", "যোগজীকরণ", "সম্ভাবনা"],
    "Chemistry": ["রসায়ন", "chemistry", "মৌল", "যৌগ", "বিক্রিয়া", "জৈব", "অম্ল", "ক্ষার", "পর্যায় সারণি", "জারণ", "বিজারণ", "তড়িৎ রসায়ন"],
    "Biology": ["জীববিজ্ঞান", "biology", "কোষ", "উদ্ভিদ", "প্রাণী", "জিনতত্ত্ব", "ডিএনএ", "dna", "রক্ত", "হৃদপিণ্ড", "শ্বসন", "সালোকসংশ্লেষণ", "টিস্যু", "অণুজীব"],
    "ICT": ["আইসিটি", "ict", "প্রোগ্রামিং", "সংখ্যা পদ্ধতি", "বুলিয়ান", "লজিক গেট", "ডেটাবেস", "sql", "html", "নেটওয়ার্ক", "অ্যালগরিদম", "সি প্রোগ্রামিং"],
    "Bengali": ["বাংলা", "সাহিত্য", "ব্যাকরণ", "কবিতা", "গদ্য", "উপন্যাস", "নাটক", "সমাস", "সন্ধি"],
    "English": ["english", "grammar", "vocabulary", "passage", "preposition", "narration", "transformation"],
}


def _detect_subject(text: str, fallback: str = "General Study") -> str:
    low = (text or "").lower()
    best_subj = fallback
    best_hits = 0
    for subj, words in _SUBJECT_KEYWORDS.items():
        hits = sum(1 for w in words if w in low)
        if hits > best_hits:
            best_hits = hits
            best_subj = subj
    return best_subj


def _detect_content_type(text: str) -> str:
    low = (text or "").lower()
    if any(k in low for k in ["mcq", "বহুনির্বাচনি", "সঠিক উত্তর", "options"]):
        return "mcq"
    if any(k in low for k in ["$$", "\\frac", "সূত্র", "formula", "সমীকরণ"]):
        return "formula_and_theory"
    if any(k in low for k in ["উদাহরণ", "example", "সমাধান", "প্রমাণ কর"]):
        return "example"
    return "theory"


def _ingest_markdown_text(raw_text: str, source_path: Path, default_subject: str = "") -> int:
    """Split Markdown or plain text by headings (#, ##, ###) and index each section."""
    text = (raw_text or "").strip()
    if not text:
        return 0

    subject = default_subject or _detect_subject(f"{source_path.stem}\n{text[:1500]}")
    chapter = source_path.stem.replace("_", " ").replace("-", " ").strip()

    first_h1 = re.search(r"^#\s+(.+)$", text, flags=re.M)
    if first_h1:
        h1_txt = first_h1.group(1).strip()
        if " - " in h1_txt or "—" in h1_txt:
            parts = re.split(r"\s*[-—:]\s*", h1_txt, maxsplit=1)
            subject = _detect_subject(parts[0], fallback=parts[0].strip())
            chapter = parts[1].strip()
        else:
            chapter = h1_txt

    sections: list[tuple[str, str]] = []
    cur_topic = chapter
    cur_lines: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^(#{2,3})\s+(.+)$", line.strip())
        if m:
            body = "\n".join(cur_lines).strip()
            if body:
                sections.append((cur_topic, body))
            cur_topic = m.group(2).strip()
            cur_lines = []
        else:
            cur_lines.append(line)

    tail_body = "\n".join(cur_lines).strip()
    if tail_body:
        sections.append((cur_topic, tail_body))

    count = 0
    for topic_title, body in sections:
        for i in range(0, len(body), 2800):
            chunk_txt = body[i : i + 2800].strip()
            if len(chunk_txt) < 15:
                continue
            part_topic = topic_title if i == 0 else f"{topic_title} (Part {i // 2800 + 1})"
            ctype = _detect_content_type(chunk_txt)
            save_textbook_chunk(
                subject=subject,
                chapter=chapter,
                topic=part_topic,
                content_md=chunk_txt,
                content_type=ctype,
                keywords=f"{subject}, {chapter}, {topic_title}",
                source_file=str(source_path),
            )
            count += 1
    return count


def _ingest_json_file(source_path: Path) -> int:
    try:
        data = json.loads(source_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        print(f"[KnowledgeIngest] JSON parse error in {source_path}: {e}")
        return 0

    count = 0
    default_subj = _detect_subject(source_path.stem)
    default_chap = source_path.stem.replace("_", " ").strip()

    def _handle_item(item: dict, subj: str, chap: str):
        nonlocal count
        s = str(item.get("subject") or subj or default_subj).strip()
        c = str(item.get("chapter") or chap or default_chap).strip()
        t = str(
            item.get("topic")
            or item.get("title")
            or item.get("heading")
            or item.get("question")
            or item.get("front")
            or c
        ).strip()
        body = (
            item.get("content")
            or item.get("content_md")
            or item.get("markdown")
            or item.get("text")
            or item.get("explanation")
            or item.get("back")
            or ""
        )
        if not body and ("options" in item or "answer" in item):
            opts = item.get("options") or []
            ans = item.get("answer")
            exp = item.get("explain") or item.get("explanation") or ""
            body = f"**প্রশ্ন:** {t}\n**অপশন:** {', '.join(map(str, opts))}\n**উত্তর:** {ans}\n**ব্যাখ্যা:** {exp}"
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, indent=2)
        body = str(body).strip()
        if not body:
            return
        kw = item.get("keywords") or item.get("tags") or ""
        if isinstance(kw, list):
            kw = ", ".join(map(str, kw))
        ctype = str(item.get("content_type") or item.get("type") or _detect_content_type(body))
        save_textbook_chunk(
            subject=s,
            chapter=c,
            topic=t,
            content_md=body,
            content_type=ctype,
            keywords=str(kw),
            source_file=str(source_path),
            metadata=item,
        )
        count += 1

    if isinstance(data, list):
        for elem in data:
            if isinstance(elem, dict):
                _handle_item(elem, default_subj, default_chap)
    elif isinstance(data, dict):
        top_subj = str(data.get("subject") or default_subj)
        top_chap = str(data.get("chapter") or data.get("title") or default_chap)
        if "chapters" in data and isinstance(data["chapters"], list):
            for ch in data["chapters"]:
                if isinstance(ch, dict):
                    c_name = str(ch.get("chapter") or ch.get("title") or top_chap)
                    for sec in ch.get("topics") or ch.get("sections") or ch.get("items") or [ch]:
                        if isinstance(sec, dict):
                            _handle_item(sec, top_subj, c_name)
        elif any(k in data for k in ("items", "chunks", "topics", "cards", "questions")):
            for k in ("items", "chunks", "topics", "cards", "questions"):
                if isinstance(data.get(k), list):
                    for elem in data[k]:
                        if isinstance(elem, dict):
                            _handle_item(elem, top_subj, top_chap)
        else:
            _handle_item(data, top_subj, top_chap)

    return count


def _ingest_image_via_vision(source_path: Path) -> int:
    try:
        from google import genai
        from google.genai import types
        from core import gemini as _gem

        api_key = _gem.api_key()
        if not api_key:
            return 0
        img_bytes = source_path.read_bytes()
        ext = source_path.suffix.lower().lstrip(".")
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        client = genai.Client(api_key=api_key)
        prompt = (
            "You are digitizing a Bangladeshi HSC/University textbook page into clean Markdown for a study database.\n"
            "1. First line MUST be `# Subject - Chapter Title`.\n"
            "2. Use `## Topic Heading` for each section or math problem.\n"
            "3. Preserve all Bengali text accurately and write all mathematical/physics equations in LaTeX ($...$ or $$...$$).\n"
            "Output ONLY the clean Markdown."
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[types.Part.from_bytes(data=img_bytes, mime_type=mime), prompt],
        )
        md_text = (resp.text or "").strip()
        if md_text:
            companion = INBOX_DIR / f"{source_path.stem}_ocr.md"
            companion.write_text(md_text, encoding="utf-8")
            return _ingest_markdown_text(md_text, source_path)
    except Exception as e:
        print(f"[KnowledgeIngest] Vision OCR skipped for {source_path.name}: {e}")
    return 0


def ingest_files_or_folders(paths: list[str] | str) -> dict:
    if isinstance(paths, (str, Path)):
        raw_list = [str(paths)]
    else:
        raw_list = [str(p) for p in (paths or []) if p]

    supported_exts = {".md", ".json", ".txt", ".rst", ".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".webp"}
    files_to_process: list[Path] = []

    for raw in raw_list:
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_dir():
            try:
                approve_folder_permanently(p)
            except Exception:
                pass
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".git", ".venv")]
                for fn in files:
                    fp = Path(root) / fn
                    if fp.suffix.lower() in supported_exts and not fn.startswith("."):
                        files_to_process.append(fp)
        elif p.is_file() and p.suffix.lower() in supported_exts:
            try:
                approve_file_permanently(p)
            except Exception:
                pass
            files_to_process.append(p)

    total_chunks = 0
    processed_files: list[str] = []

    for fp in files_to_process:
        ext = fp.suffix.lower()
        added = 0
        try:
            if ext == ".json":
                added = _ingest_json_file(fp)
            elif ext in (".md", ".txt", ".rst"):
                txt = fp.read_text(encoding="utf-8", errors="replace")
                added = _ingest_markdown_text(txt, fp)
            elif ext in (".pdf", ".docx", ".pptx"):
                from actions.file_processor import extract_documents
                agg_text, _ = extract_documents([str(fp)])
                if agg_text:
                    added = _ingest_markdown_text(agg_text, fp)
            elif ext in (".png", ".jpg", ".jpeg", ".webp"):
                added = _ingest_image_via_vision(fp)
        except Exception as e:
            print(f"[KnowledgeIngest] Error indexing {fp.name}: {e}")

        if added > 0:
            total_chunks += added
            processed_files.append(fp.name)

    return {
        "files_indexed": len(processed_files),
        "file_names": processed_files,
        "chunks_saved": total_chunks,
        "db_path": str(DB_PATH.resolve()),
    }


def execute(parameters: dict | None = None, player=None, **kwargs) -> str:
    params = dict(parameters or kwargs or {})
    action = str(params.get("action") or "search").strip().lower()
    query = str(params.get("query") or "").strip()
    subject = str(params.get("subject") or "").strip()
    file_or_folder_path = str(params.get("path") or "").strip()

    if action == "ingest" and file_or_folder_path:
        res = ingest_files_or_folders([file_or_folder_path])
        return (
            f"[TEXTBOOK_VAULT_INGESTED] Indexed {res['files_indexed']} file(s) "
            f"({res['chunks_saved']} chunks) into {res['db_path']}."
        )

    if action == "catalog":
        cat = get_textbook_catalog()
        if not cat:
            return (
                f"[TEXTBOOK_VAULT] No textbook chapters indexed yet in {DB_PATH.resolve()}. "
                "The user can drop .md, .json, .pdf, or textbook images into the File Upload box or `data/knowledge_inbox/`."
            )
        lines = [f"[TEXTBOOK_VAULT_CATALOG — stored in {DB_PATH.resolve()}]"]
        for c in cat:
            lines.append(f"- {c['subject']} > {c['chapter']} ({c['chunk_count']} chunks, updated {c['last_updated']})")
        return "\n".join(lines)

    matches = search_textbook_chunks(query=query, subject=subject, limit=6)
    if not matches:
        return (
            f"[TEXTBOOK_VAULT_SEARCH] No exact textbook match found for '{query}' in {DB_PATH.resolve()}. "
            "You may answer using standard curriculum knowledge, and mention that this topic wasn't in the uploaded notes yet."
        )

    out = [f"[TEXTBOOK_VAULT_MATCHES — from {DB_PATH.resolve()}]"]
    for i, m in enumerate(matches, 1):
        out.append(
            f"\n--- MATCH {i}: [{m['subject']} > {m['chapter']} > {m['topic']}] (Source: {Path(m['source_file']).name}) ---\n"
            f"{m['content_md']}"
        )
    return "\n".join(out)


TOOL = {
    "name": "study_kb_lookup",
    "description": (
        "Search, list, or ingest the student's personal textbook & notes database (`data/study_vault.db`). "
        "CRITICAL RULE: Before explaining any academic topic, solving a problem, or making a quiz, call "
        "`study_kb_lookup` with action='search' and your topic query so you teach strictly from the student's "
        "own uploaded textbook/Markdown/JSON notes!"
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "search (default: find textbook chunks) | catalog (list all stored chapters) | ingest (index a file/folder path)",
            },
            "query": {
                "type": "STRING",
                "description": "Topic, keyword, or formula to search for (in Bengali or English, e.g., 'কির্টফের সূত্র', 'Kirchhoff', 'ম্যাট্রিক্স')",
            },
            "subject": {
                "type": "STRING",
                "description": "Optional subject filter (Physics, Higher Math, Chemistry, Biology, ICT)",
            },
            "path": {
                "type": "STRING",
                "description": "File or folder path to ingest when action='ingest'",
            },
        },
        "required": ["action"],
    },
    "handler": execute,
}

run = execute
