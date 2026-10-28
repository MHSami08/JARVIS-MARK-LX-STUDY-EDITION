# -*- coding: utf-8 -*-
"""
Zero-Trust Study File Reader (`actions/file_reader.py`)

JARVIS cannot scan folders or read any file without explicit approval via
`core.security_policy`. Approving a folder once grants recursive access to the
files inside it, while sensitive system paths stay blocked per file.
"""

from pathlib import Path
import json
from core.security_policy import (
    request_verified_file_access,
    request_verified_folder_access,
    request_verified_write_access,
    list_approved_files,
    list_approved_folders,
    list_approved_tree,
    list_directory_tree,
    find_approved_folder,
    is_forbidden_path,
)

TOOL_SCHEMA = {
    "name": "file_reader",
    "description": (
        "Read, list, find or modify study files ONLY inside what the user approved. "
        "An approved FOLDER grants recursive read, list and write access to every file and "
        "subfolder inside it with no further popups, so never ask the user for per-file "
        "paths: use list_folder to see what is there, then read_file. The user never has to "
        "give an exact path or extension: file_path and folder_path accept a plain name "
        "('physics notes', 'StudyNotes') and are resolved by fuzzy search; if several files "
        "match, the result lists them and you must ask which one. Sensitive system paths "
        "(.env, .git, Windows, keys) stay blocked."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["read_file", "list_approved", "list_folder", "find_file",
                         "write_file", "append_file"],
                "description": (
                    "read_file: read a file (name or path). list_approved: every approved "
                    "folder plus all files beneath them (recursive). list_folder: recursively "
                    "list one folder; leave folder_path empty to list all approved folders. "
                    "find_file: fuzzy-search a file by a few words. "
                    "write_file: replace a file's text. append_file: add text to it."
                )
            },
            "file_path": {
                "type": "STRING",
                "description": "Path OR just the words the user used for the file ('car', 'chapter 3')."
            },
            "folder_path": {
                "type": "STRING",
                "description": "Folder path or name of an approved folder. Empty = all approved folders."
            },
            "query": {
                "type": "STRING",
                "description": "Words to search for when action is find_file."
            },
            "content": {
                "type": "STRING",
                "description": "Text to write or append when action is write_file/append_file."
            }
        },
        "required": ["action"]
    }
}

_MAX_FOLDER_ENTRIES = 500
_MAX_WRITE_BYTES = 2_000_000
_TEXTLESS_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic",
                  ".mp3", ".wav", ".mp4", ".mkv", ".zip", ".rar", ".7z", ".exe"}


def _fuzzy(query: str, kind: str = "any") -> dict:
    """Run smart_file_search; never raises."""
    try:
        from actions import smart_file_search as sfs
        res = sfs.resolve(query, kind=kind, limit=25)
        return {"res": res, "text": sfs.describe(res)}
    except Exception as e:  # pragma: no cover - defensive
        return {"res": {"status": "error"}, "text": f"search failed: {e}"}


def _resolve_file(file_path: str):
    """Return (path, None) or (None, result_dict) when the name is ambiguous."""
    raw = str(file_path or "").strip().strip('"').strip("'")
    if not raw:
        return None, {"status": "error", "reason": "file_path (or a name to search) is required."}
    try:
        cand = Path(raw).expanduser()
        if cand.exists():
            return str(cand), None
    except (OSError, ValueError):
        pass
    found = _fuzzy(raw)
    res = found["res"]
    if res.get("status") == "ok":
        return res["path"], None
    return None, {
        "status": "needs_choice" if res.get("status") in ("multiple", "confirm") else "not_found",
        "message": found["text"],
        "matches": [{"name": m["name"], "path": m["path"], "folder": m.get("folder", "")}
                    for m in (res.get("matches") or [])],
    }


def _resolve_folder(folder_path: str):
    """Exact path, else the approved folder whose name contains the words."""
    raw = str(folder_path or "").strip().strip('"').strip("'")
    if not raw:
        return None
    try:
        if Path(raw).expanduser().is_dir():
            return str(Path(raw).expanduser())
    except (OSError, ValueError):
        pass
    want = raw.casefold().replace("\\", "/").rstrip("/")
    hits = []
    for folder in list_approved_folders():
        norm = folder.casefold().replace("\\", "/").rstrip("/")
        if norm.endswith("/" + want) or want in norm.rsplit("/", 1)[-1]:
            hits.append(folder)
    if len(hits) == 1:
        return hits[0]
    # A sub-folder of an approved folder, by name ("phy" inside StudyNotes).
    if not hits:
        for folder in list_approved_folders():
            try:
                for d in Path(folder).rglob("*"):
                    if d.is_dir() and want in d.name.casefold() and not is_forbidden_path(d):
                        return str(d)
            except OSError:
                continue
    return hits[0] if hits else None


def _extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".pdf", ".docx", ".doc", ".pptx", ".ppt"):
        try:
            from actions.file_processor import extract_document_text
            text = extract_document_text(path, max_chars=60000)
            if text:
                return text
        except Exception:
            pass
        if ext == ".pdf":
            import pypdf  # may raise: reported by the caller
            reader = pypdf.PdfReader(str(path))
            return "\n".join((pg.extract_text() or "") for pg in reader.pages[:25])
    return path.read_text(encoding="utf-8", errors="replace")


def execute(action: str = "list_approved", file_path: str = "",
            folder_path: str = "", content: str = "", query: str = "", **kwargs) -> dict:
    if action == "list_approved":
        tree = list_approved_tree(limit=_MAX_FOLDER_ENTRIES)
        files = list_approved_files()
        return {
            "status": "ok",
            "approved_files": files,
            "approved_folders": tree["approved_folders"],
            "entries": tree["entries"],
            "count": tree["count"],
            "truncated": tree["truncated"],
            "note": ("Folder grants are recursive: every file listed here can be "
                     "read or modified without asking again."),
        }

    if action == "find_file":
        found = _fuzzy(query or file_path)
        res = found["res"]
        return {"status": res.get("status", "error"), "message": found["text"],
                "path": res.get("path", ""),
                "matches": [{"name": m["name"], "path": m["path"], "folder": m.get("folder", "")}
                            for m in (res.get("matches") or [])]}

    if action == "list_folder":
        try:
            if not str(folder_path or "").strip():
                if not list_approved_folders():
                    return {"status": "blocked",
                            "reason": "No folder is approved yet. Ask the user to click "
                                      "'🔒 Approve Study Files' and choose a folder."}
                return execute("list_approved")
            target = _resolve_folder(folder_path)
            if not target:
                return {"status": "not_found",
                        "reason": f"No approved folder matches '{folder_path}'."}
            verified_folder = request_verified_folder_access(target)
            listing = list_directory_tree(str(verified_folder), limit=_MAX_FOLDER_ENTRIES,
                                          require_approved=False)
            return {
                "status": "ok",
                "folder": str(verified_folder),
                "entries": listing["entries"],
                "count": listing["count"],
                "truncated": listing["truncated"],
            }
        except Exception as e:
            return {"status": "blocked", "reason": str(e)}

    if action in ("write_file", "append_file"):
        try:
            payload = "" if content is None else str(content)
            data = payload.encode("utf-8")
            if len(data) > _MAX_WRITE_BYTES:
                return {
                    "status": "blocked",
                    "reason": f"Refusing to write {len(data):,} bytes (limit {_MAX_WRITE_BYTES:,}).",
                }
            resolved, ask = _resolve_file(file_path) if not _is_new_path(file_path) \
                else (file_path, None)
            if ask is not None:
                return ask
            target = request_verified_write_access(resolved)
            mode = "a" if action == "append_file" else "w"
            with open(target, mode, encoding="utf-8") as fh:
                fh.write(payload)
            return {
                "status": "ok",
                "file": str(target),
                "bytes_written": len(data),
                "mode": "append" if mode == "a" else "replace",
            }
        except Exception as e:
            return {"status": "blocked", "reason": str(e)}

    if action != "read_file":
        return {"status": "error", "reason": f"Unknown action: {action}"}

    try:
        resolved, ask = _resolve_file(file_path)
        if ask is not None:
            return ask
        verified_path: Path = request_verified_file_access(resolved)
        if verified_path.suffix.lower() in _TEXTLESS_EXTS:
            return {"status": "ok", "file": str(verified_path), "content_preview": "",
                    "truncated": False,
                    "note": "Binary/media file: no text to read. For an image, ask the "
                            "user to press F9 / '📸 Capture Screen' with it on screen."}
        try:
            text = _extract_text(verified_path)
        except Exception as e:
            return {"status": "error", "reason": f"Read error: {e}"}
        return {
            "status": "ok",
            "file": str(verified_path),
            "approved_folder": find_approved_folder(str(verified_path)),
            "content_preview": text[:12000],
            "truncated": len(text) > 12000
        }
    except Exception as e:
        return {"status": "blocked", "reason": str(e)}


def _is_new_path(file_path: str) -> bool:
    """A write target that does not exist yet but has a real parent folder."""
    try:
        p = Path(str(file_path or "").strip().strip('"')).expanduser()
        return bool(p.name) and p.is_absolute() and not p.exists() and p.parent.is_dir()
    except (OSError, ValueError):
        return False


def file_reader(parameters: dict, response=None, player=None,
                session_memory=None) -> str:
    """Tool handler used by core.action_loader."""
    params = parameters or {}
    result = execute(
        action=str(params.get("action") or "list_approved"),
        file_path=str(params.get("file_path") or ""),
        folder_path=str(params.get("folder_path") or ""),
        content=params.get("content") or "",
        query=str(params.get("query") or ""),
    )
    return json.dumps(result, ensure_ascii=False)


TOOL = {
    "name": TOOL_SCHEMA["name"],
    "description": TOOL_SCHEMA["description"],
    "parameters": TOOL_SCHEMA["parameters"],
    "handler": file_reader,
}
