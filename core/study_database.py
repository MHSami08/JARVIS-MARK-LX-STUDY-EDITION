import threading
# -*- coding: utf-8 -*-
"""
100% Local SQLite Study Database (`data/study_vault.db`).

All study progress, notes summaries, flashcards, quiz history, mistake logs
and syllabus context stay strictly on this machine — nothing here ever leaves
it. Owner transparency is mandatory: every write reports the exact absolute
database path to the terminal, and `storage_inventory()` lists every local
storage location JARVIS uses, for the HUD and any user inventory request.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "study_vault.db"

# Full disclosure inventory: every local storage directory this build touches.
# Consumed by the HUD ("storage paths" reporting) and the `study_vault` tool.
STORAGE_PATHS = {
    "study_vault_db": DB_PATH,
    "study_cache": ROOT_DIR / "memory" / "study_cache.json",
    "study_mistakes": ROOT_DIR / "memory" / "study_mistakes.json",
    "mistake_bank": ROOT_DIR / "memory" / "mistake_bank.json",
    "long_term_memory": ROOT_DIR / "memory" / "long_term.json",
    "approved_files_manifest": ROOT_DIR / "data" / "approved_files.json",
    "study_board_html": ROOT_DIR / "runtime" / "study_board.html",
    "manual_screen_capture": ROOT_DIR / "runtime" / "manual_study_capture.png",
    "session_exports": ROOT_DIR / "workspace" / "exports",
    "knowledge_inbox": ROOT_DIR / "data" / "knowledge_inbox",
    "api_config": ROOT_DIR / "config" / "api_keys.json",
}


def storage_inventory() -> dict[str, str]:
    """Absolute-path inventory of every local store, freshly resolved."""
    out: dict[str, str] = {}
    for key, raw in STORAGE_PATHS.items():
        try:
            p = Path(raw).resolve()
            out[key] = str(p)
        except Exception:
            out[key] = str(raw)
    return out


def report_storage_path(action: str, extra: str = "") -> str:
    """Print + return the exact absolute DB path for any save/fetch operation.

    Owner transparency rule: no storage location is ever hidden. Call this
    after every DB mutation; the same string is safe to log into the HUD.
    """
    line = f"[StudyVault] {action}: {DB_PATH.resolve()}" + (f" ({extra})" if extra else "")
    try:
        print(line, file=sys.stdout, flush=True)
    except Exception:
        pass
    return line


_TLS = threading.local()

def _conn() -> sqlite3.Connection:
    conn = getattr(_TLS, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=MEMORY;")
        except Exception:
            pass
        _TLS.conn = conn
    return conn


def init_study_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            subject TEXT NOT NULL,
            topic TEXT NOT NULL,
            duration_min REAL DEFAULT 0,
            summary TEXT
        );

        CREATE TABLE IF NOT EXISTS topic_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            mastery_level INTEGER DEFAULT 1,
            last_reviewed TEXT NOT NULL,
            notes TEXT,
            UNIQUE(subject, chapter)
        );

        CREATE TABLE IF NOT EXISTS saved_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            subject TEXT,
            topic TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS study_context_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at TEXT NOT NULL,
            subject TEXT NOT NULL,
            key_concept TEXT NOT NULL,
            context_detail TEXT NOT NULL
        );
        """)


def log_study_session(subject: str, topic: str, summary: str, duration_min: float = 15.0):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.execute(
            "INSERT INTO study_sessions (created_at, subject, topic, duration_min, summary) VALUES (?, ?, ?, ?, ?)",
            (now, subject, topic, duration_min, summary)
        )
        c.execute(
            """INSERT INTO topic_progress (subject, chapter, mastery_level, last_reviewed, notes)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(subject, chapter) DO UPDATE SET
                   mastery_level = MIN(5, topic_progress.mastery_level + 1),
                   last_reviewed = excluded.last_reviewed,
                   notes = excluded.notes""",
            (subject, topic, now, summary)
        )
    report_storage_path("session logged", f"subject={subject} topic={topic}")


def save_study_artifact(artifact_type: str, subject: str, topic: str, payload: dict):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.execute(
            "INSERT INTO saved_artifacts (created_at, artifact_type, subject, topic, payload_json) VALUES (?, ?, ?, ?, ?)",
            (now, artifact_type, subject or "General", topic or "", json.dumps(payload, ensure_ascii=False))
        )
    report_storage_path("artifact saved", f"type={artifact_type} topic={topic}")


def save_study_context(subject: str, key_concept: str, context_detail: str):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.execute(
            "INSERT INTO study_context_memory (updated_at, subject, key_concept, context_detail) VALUES (?, ?, ?, ?)",
            (now, subject, key_concept, context_detail)
        )
    report_storage_path("context saved", f"subject={subject} concept={key_concept}")


def get_study_progress_summary(limit: int = 10) -> dict:
    """Everything the Study Mentor needs injected locally — no raw files, no network.

    Keys: topics (chapter_list), recent_context, recent_sessions.
    """
    with _conn() as c:
        sessions = [dict(r) for r in c.execute(
            "SELECT subject, topic, duration_min, summary, created_at FROM study_sessions ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()]
        topics = [dict(r) for r in c.execute(
            "SELECT subject, chapter, mastery_level, last_reviewed, notes FROM topic_progress ORDER BY last_reviewed DESC LIMIT ?",
            (limit,)
        ).fetchall()]
        contexts = [dict(r) for r in c.execute(
            "SELECT subject, key_concept, context_detail, updated_at FROM study_context_memory ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()]
    return {"topics": topics, "recent_context": contexts, "recent_sessions": sessions}


init_study_db()


# ── Permanent Textbook Knowledge Vault (Auto-Indexed .md / .json / Docs) ─────
def _init_knowledge_tables():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS textbook_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source_file TEXT NOT NULL,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            topic TEXT NOT NULL,
            content_type TEXT DEFAULT 'theory',
            content_md TEXT NOT NULL,
            keywords TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_tb_subject ON textbook_chunks(subject);
        CREATE INDEX IF NOT EXISTS idx_tb_chapter ON textbook_chunks(chapter);
        CREATE INDEX IF NOT EXISTS idx_tb_topic ON textbook_chunks(topic);
        """)

_init_knowledge_tables()


def save_textbook_chunk(
    subject: str,
    chapter: str,
    topic: str,
    content_md: str,
    content_type: str = "theory",
    keywords: str = "",
    source_file: str = "",
    metadata: dict | None = None,
) -> int:
    """Insert or update a textbook knowledge chunk in data/study_vault.db."""
    content_md = (content_md or "").strip()
    if not content_md:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    subject = (subject or "General").strip()
    chapter = (chapter or "General").strip()
    topic = (topic or "Concept").strip()
    meta_str = json.dumps(metadata or {}, ensure_ascii=False)

    with _conn() as c:
        # Avoid duplicate chunks from the same file & topic
        existing = c.execute(
            "SELECT id FROM textbook_chunks WHERE source_file = ? AND topic = ? AND substr(content_md, 1, 120) = ?",
            (str(source_file), topic, content_md[:120]),
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE textbook_chunks
                   SET created_at=?, subject=?, chapter=?, content_type=?, content_md=?, keywords=?, metadata_json=?
                   WHERE id=?""",
                (now, subject, chapter, content_type, content_md, keywords, meta_str, existing["id"]),
            )
            row_id = int(existing["id"])
        else:
            cur = c.execute(
                """INSERT INTO textbook_chunks
                   (created_at, source_file, subject, chapter, topic, content_type, content_md, keywords, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, str(source_file), subject, chapter, topic, content_type, content_md, keywords, meta_str),
            )
            row_id = int(cur.lastrowid or 0)

    report_storage_path("textbook chunk saved", f"{subject} > {chapter} > {topic}")
    return row_id


def search_textbook_chunks(query: str, subject: str = "", limit: int = 6) -> list[dict]:
    """Search the permanent textbook vault by Bengali/English keywords, topic, chapter, or content."""
    q = (query or "").strip()
    tokens = [t.strip() for t in q.replace(",", " ").split() if len(t.strip()) >= 2]
    with _conn() as c:
        rows = c.execute(
            "SELECT id, created_at, source_file, subject, chapter, topic, content_type, content_md, keywords "
            "FROM textbook_chunks ORDER BY id DESC LIMIT 400"
        ).fetchall()

    scored: list[tuple[int, dict]] = []
    subj_filter = (subject or "").strip().lower()
    q_low = q.lower()

    for r in rows:
        d = dict(r)
        if subj_filter and subj_filter not in d["subject"].lower():
            continue
        hay_title = f"{d['subject']} {d['chapter']} {d['topic']} {d['keywords']}".lower()
        hay_body = d["content_md"].lower()
        score = 0
        if not q_low:
            score = 1
        else:
            if q_low in hay_title:
                score += 12
            if q_low in hay_body:
                score += 6
            for tok in tokens:
                tl = tok.lower()
                if tl in hay_title:
                    score += 5
                if tl in hay_body:
                    score += 2
        if score > 0:
            scored.append((score, d))

    scored.sort(key=lambda x: (x[0], x[1]["id"]), reverse=True)
    report_storage_path("textbook vault searched", f"query='{q}' matches={min(len(scored), limit)}")
    return [item[1] for item in scored[:limit]]


def get_textbook_catalog() -> list[dict]:
    """Return a summary of all subjects and chapters stored in the vault."""
    with _conn() as c:
        rows = c.execute(
            """SELECT subject, chapter, COUNT(*) as chunk_count, MAX(created_at) as last_updated
               FROM textbook_chunks
               GROUP BY subject, chapter
               ORDER BY subject, chapter"""
        ).fetchall()
    return [dict(r) for r in rows]
