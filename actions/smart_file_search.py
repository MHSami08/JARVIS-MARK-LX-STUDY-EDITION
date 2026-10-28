"""
actions/smart_file_search.py — resolve a spoken file name to a real file.

Why this exists
---------------
"Open my physics notes" or "what is in the car picture" used to end in a
question back to the user: *which extension?* *what is the full path?* The user
had already said everything they know. The file is somewhere in Desktop, Downloads, Pictures or
Documents, and its name contains the words they said.

So this tool does the two things the model cannot do for itself: it looks, and
it ranks.

    search_query  ->  one absolute path        (send it)
                  ->  a numbered choice list   (ask which one)
                  ->  "not found" + nearest    (say so politely)

The scan is deliberately bounded: the folders a person actually drops files
into, four levels deep, a file cap and a wall-clock budget. Scanning C:\\ for a
photo is how a helpful assistant becomes a slow one.

The model NEVER needs to know an extension. `.png`, `.jpg`, `.pdf` and friends
are all in scope by default — the query only has to describe the file, not
name its type. `kind` ('image', 'document', ...) is an optional narrowing, not
a requirement.
"""
from __future__ import annotations

import difflib
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # approved study folders are searched (deeply) before the usual folders
    from core.security_policy import is_forbidden_path, list_approved_folders
except Exception:  # pragma: no cover - the search still works standalone
    def is_forbidden_path(_p) -> bool:
        return False

    def list_approved_folders() -> list:
        return []

# ── what we are willing to find ──────────────────────────────────────────────
# Extension-agnostic by default: everything a person would realistically send in
# a chat is fair game, so an image query never has to say ".png".
_IMAGE_EXT = frozenset({
    "png", "jpg", "jpeg", "jpe", "jfif", "webp", "gif", "bmp", "heic", "heif",
    "tif", "tiff", "svg", "avif", "ico",
})
_DOC_EXT = frozenset({
    "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "csv", "xls", "xlsx",
    "ods", "ppt", "pptx", "odp", "epub",
})
_VIDEO_EXT = frozenset({"mp4", "mov", "mkv", "webm", "avi", "m4v", "wmv", "flv", "3gp"})
_AUDIO_EXT = frozenset({"mp3", "wav", "m4a", "aac", "ogg", "opus", "flac", "wma"})
_ARCHIVE_EXT = frozenset({"zip", "rar", "7z", "tar", "gz", "bz2"})

_ALL_EXT = _IMAGE_EXT | _DOC_EXT | _VIDEO_EXT | _AUDIO_EXT | _ARCHIVE_EXT

_KIND_ALIASES: Dict[str, Optional[frozenset]] = {
    "any": None, "all": None, "file": None, "files": None, "auto": None,
    "image": _IMAGE_EXT, "images": _IMAGE_EXT, "picture": _IMAGE_EXT,
    "pictures": _IMAGE_EXT, "photo": _IMAGE_EXT, "photos": _IMAGE_EXT,
    "pic": _IMAGE_EXT, "screenshot": _IMAGE_EXT,
    "document": _DOC_EXT, "documents": _DOC_EXT, "doc": _DOC_EXT,
    "docs": _DOC_EXT, "pdf": frozenset({"pdf"}),
    "video": _VIDEO_EXT, "videos": _VIDEO_EXT, "movie": _VIDEO_EXT,
    "audio": _AUDIO_EXT, "music": _AUDIO_EXT, "sound": _AUDIO_EXT,
    "archive": _ARCHIVE_EXT, "zip": _ARCHIVE_EXT,
    "media": _VIDEO_EXT | _AUDIO_EXT,
}

# ── how far we look, and for how long ───────────────────────────────────────
_PRIMARY_FOLDERS = ("Desktop", "Downloads", "Pictures", "Documents")
_AV_FOLDERS = ("Videos", "Music")          # only worth walking for A/V queries
_MAX_DEPTH = 4
_APPROVED_DEPTH = 12                       # approved folders are recursive
_MAX_FILES = 6000
_TIME_BUDGET_S = 5.0
_TIE_BREAK_MAX = 25                        # how many options are listed back

_SKIP_DIRS = {
    "$recycle.bin", "system volume information", "__pycache__", "node_modules",
    ".git", ".svn", ".cache", "temp", "tmp", "cache", "thumbnails",
    "temporary internet files", "windowsapps",
}

# Words that describe the *kind* of thing, not which thing: "open the car
# picture" must search for "car", not for a file with "picture" in its name.
# Dropped only when something more specific remains, so a file literally named
# "photo.jpg" is still findable.
_QUERY_NOISE = {
    "the", "a", "an", "of", "for", "to", "in", "on", "at", "from", "please",
    "send", "give", "show", "me", "my", "mine", "our", "that", "this", "it",
    "picture", "pictures", "pic", "pics", "photo", "photos", "image", "images",
    "img", "file", "files", "document", "documents", "doc", "scan", "copy",
    "আমার", "এর", "টা", "টার", "টি", "ছবি", "ছবিটা", "ফাইল", "ফাইলটা",
}
_IMAGE_WORDS = {"picture", "pictures", "pic", "pics", "photo", "photos",
                "image", "images", "img", "ছবি", "ছবিটা"}


# ── helpers ─────────────────────────────────────────────────────────────────
def _log(player, text: str) -> None:
    if not player:
        return
    try:
        player.write_log(text)
    except Exception:
        pass


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _root_names(kind_key: str) -> tuple[str, ...]:
    """Folder names to walk. Audio/video queries also get Videos and Music."""
    if kind_key in ("video", "videos", "movie", "audio", "music", "sound", "media"):
        return _PRIMARY_FOLDERS + _AV_FOLDERS
    return _PRIMARY_FOLDERS


def _root_folders(kind_key: str) -> List[Path]:
    """Desktop / Downloads / Pictures / Documents (plus OneDrive-redirected
    copies of them, because on Windows those folders are often moved under
    OneDrive and the plain `~/Pictures` path then does not exist)."""
    home = _home()
    bases = [home]
    try:
        bases.extend(p for p in sorted(home.glob("OneDrive*")) if p.is_dir())
    except OSError:
        pass

    roots: List[Path] = []
    seen: set[str] = set()
    for raw in list_approved_folders():
        # Folders the user approved come first and are searched recursively.
        try:
            candidate = Path(raw)
            if not candidate.is_dir() or is_forbidden_path(candidate):
                continue
            key = str(candidate.resolve()).casefold()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    for base in bases:
        for name in _root_names(kind_key):
            candidate = base / name
            try:
                if not candidate.is_dir():
                    continue
                key = str(candidate.resolve()).casefold()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            roots.append(candidate)
    return roots


def _existing_path(raw: str) -> str:
    """A real path (or a file:// URL) the model already got right: use it as-is."""
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if text.casefold().startswith("file:///"):
        text = text[8:]
    try:
        path = Path(os.path.expandvars(os.path.expanduser(text)))
        if path.exists():
            return str(path.resolve())
    except (OSError, ValueError):
        pass
    return ""


_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _tokens(text: str) -> List[str]:
    """Split a name into lowercase alphanumeric words.

    CamelCase is split first, because filenames often run words together
    ("AnimeCartoonHub") and a user searching "cartoon" must still find it. Keeps
    Bangla, CJK and other non-Latin scripts intact instead of dropping them.
    """
    staged = _CAMEL_RE.sub(" ", str(text or ""))
    out: List[str] = []
    current: List[str] = []
    for ch in staged.casefold():
        if ch.isalnum():
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _human_size(size: int) -> str:
    step = 1024.0
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"


def _folder_label(item: Dict[str, Any]) -> str:
    """'Pictures' when the file sits at the top, 'Pictures/Cars' when not."""
    root = Path(item["root"])
    try:
        rel = Path(item["path"]).parent.relative_to(root)
    except (ValueError, OSError):
        return root.name
    return root.name if str(rel) in ("", ".") else f"{root.name}/{rel.as_posix()}"


def _item(path: Path, root: Optional[Path] = None) -> Dict[str, Any]:
    try:
        st = path.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    return {
        "name": path.name,
        "path": str(path),
        "ext": path.suffix.casefold().lstrip("."),
        "size": size,
        "size_text": _human_size(size),
        "mtime": mtime,
        "root": str(root or path.parent),
    }


# ── scan ────────────────────────────────────────────────────────────────────
def _approved_keys() -> set[str]:
    out: set[str] = set()
    for raw in list_approved_folders():
        try:
            out.add(str(Path(raw).resolve()).casefold())
        except OSError:
            continue
    return out


def _scan(roots: List[Path], allowed_ext: Optional[frozenset],
          deadline: float, any_kind: bool = False) -> tuple[List[Dict[str, Any]], bool]:
    """Walk the roots; stop at the file cap or the deadline.

    Approved folders are walked ``_APPROVED_DEPTH`` levels deep and, for an
    untyped query, match EVERY file type (notes, code, csv ...). The standard
    folders keep the shallow, media/document-only scan. Forbidden paths are
    skipped everywhere. Returns (files, truncated) so the caller can say the
    list was cut short instead of implying the disk holds nothing else.
    """
    files: List[Dict[str, Any]] = []
    truncated = False
    approved = _approved_keys()
    visited: set[str] = set()
    stack: List[tuple[Path, int, Path, bool]] = []
    for root in roots:
        try:
            is_app = str(root.resolve()).casefold() in approved
        except OSError:
            is_app = False
        stack.append((root, 0, root, is_app))
    # Pop from the left so approved roots (listed first) are scanned first.
    stack.reverse()
    while stack:
        if len(files) >= _MAX_FILES or time.monotonic() >= deadline:
            truncated = True
            break
        folder, depth, root, is_app = stack.pop()
        try:
            real = str(folder.resolve()).casefold()
        except OSError:
            continue
        if real in visited:
            continue
        visited.add(real)
        limit_depth = _APPROVED_DEPTH if is_app else _MAX_DEPTH
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if len(files) >= _MAX_FILES or time.monotonic() >= deadline:
                        truncated = True
                        break
                    name = entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if (depth < limit_depth
                                    and name[:1] not in (".", "$")
                                    and name.casefold() not in _SKIP_DIRS
                                    and not is_forbidden_path(entry.path)):
                                stack.append((Path(entry.path), depth + 1, root, is_app))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if is_forbidden_path(entry.path):
                            continue
                    except OSError:
                        continue
                    ext = os.path.splitext(name)[1].casefold().lstrip(".")
                    if (allowed_ext is not None and ext not in allowed_ext
                            and not (is_app and any_kind)):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                        size, mtime = st.st_size, st.st_mtime
                    except OSError:
                        size, mtime = 0, 0.0
                    files.append({
                        "name": name, "path": entry.path, "ext": ext,
                        "size": size, "size_text": _human_size(size),
                        "mtime": mtime, "root": str(root),
                    })
        except OSError:
            continue
    return files, truncated


# ── ranking ─────────────────────────────────────────────────────────────────
# A single match is only auto-sent when the name really is the thing asked for.
# Below this confidence the match is reported as "confirm this one": sending the
# wrong file is worse than one short question.
_CONFIDENT = 62.0


def _span_hit(stem_tokens: List[str], q_compact: str) -> bool:
    """True when the query starts a word and then crosses into the next one.

    "redcar" -> "red_car" belongs here. "car" -> "cartoon" does not: it only
    starts a word, which is weaker evidence (see the word-prefix tier).
    """
    joined = "".join(stem_tokens)
    offset = 0
    for tok in stem_tokens:
        if len(q_compact) > len(tok) and joined.startswith(q_compact, offset):
            return True
        offset += len(tok)
    return False


def _score(name: str, ext: str, q_tokens: List[str], q_compact: str,
           wanted_ext: str) -> float:
    """Higher is a better match. 0 means 'not this file'.

    Evidence, strongest first: the whole name is the query, the name starts with
    it, it is a whole word of the name, it spans two words, every query word is a
    word of the name, a word merely starts with it, then typo tolerance. Recency
    only breaks ties.
    """
    stem_tokens = _tokens(Path(name).stem)
    if not stem_tokens or not q_compact:
        return 0.0
    stem_compact = "".join(stem_tokens)
    score = 0.0

    if stem_compact == q_compact:
        score = 100.0
    elif stem_compact.startswith(q_compact):
        score = 92.0
    elif q_compact in stem_tokens:
        score = 88.0
    elif _span_hit(stem_tokens, q_compact):
        score = 84.0 - min(len(stem_compact) - len(q_compact), 20) * 0.15
    elif q_tokens and all(tok in stem_tokens for tok in q_tokens):
        score = max(60.0, 76.0 - (len(stem_tokens) - len(q_tokens)) * 3.0)
    elif (len(q_compact) >= 3
          and any(word.startswith(q_compact) for word in stem_tokens)):
        # A word merely starts with the query ("car" -> "cartoon"): weaker than
        # a whole word, so it is offered for confirmation rather than sent.
        score = 58.0
    else:
        ratio = difflib.SequenceMatcher(None, q_compact, stem_compact).ratio()
        # Length guard matters: "redcar" is a 0.78 subsequence of
        # "triptocarshow", which is not a typo of anything — it is a different
        # file that happens to share letters.
        if ratio >= 0.78 and len(stem_compact) <= len(q_compact) * 1.6 + 3:
            score = 40.0 + ratio * 20.0
        elif q_tokens:
            # Every query word must be close to some word in the name: "reprot"
            # -> "report", but "red car" must not match "trip to car show" just
            # because it shares the word "car".
            hits = [max((difflib.SequenceMatcher(None, tok, word).ratio()
                         for word in stem_tokens), default=0.0)
                    for tok in q_tokens]
            if hits and min(hits) >= 0.8:
                score = 48.0 + (sum(hits) / len(hits)) * 10.0

    if score and wanted_ext and ext == wanted_ext:
        score += 5.0        # the user did name the type after all
    return score


def resolve(query: str, kind: str = "any", limit: int = 8) -> Dict[str, Any]:
    """Find files whose names match `query`.

    Returns a dict the caller renders (see `describe`) or uses directly:
        status : "ok" | "confirm" | "multiple" | "not_found"
        path   : absolute path when status == "ok"
        matches: ranked list of {name, path, ext, size_text, mtime, folder}
        total  : how many files scored above zero (matches may be truncated)
        confidence: rank of the best match; "ok" needs a confident one
    """
    started = time.monotonic()
    raw = str(query or "").strip().strip('"').strip("'")
    out: Dict[str, Any] = {
        "query": raw, "kind": kind, "status": "not_found", "path": "",
        "matches": [], "suggestions": [], "total": 0, "roots": [],
        "searched": [], "elapsed_ms": 0, "truncated": False, "confidence": 0.0,
    }

    if not raw:
        out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return out

    direct = _existing_path(raw)
    if direct:
        item = _item(Path(direct))
        item["folder"] = _folder_label(item)
        out.update(status="ok", path=direct, total=1, matches=[item],
                   searched=[item["folder"]], confidence=100.0,
                   elapsed_ms=int((time.monotonic() - started) * 1000))
        return out

    kind_key = str(kind or "any").strip().casefold()
    allowed_ext = _KIND_ALIASES.get(kind_key, _ALL_EXT)

    q_tokens = _tokens(raw)
    wanted_ext = ""
    if q_tokens and q_tokens[-1] in _ALL_EXT:
        # "car.png" / "car png": the extension is a hint, never a requirement.
        wanted_ext = q_tokens[-1]
        q_tokens = q_tokens[:-1]
    # "the car picture" -> ["car"]: drop filler only when something remains.
    core_tokens = [t for t in q_tokens if t not in _QUERY_NOISE]
    if core_tokens:
        q_tokens = core_tokens
    elif q_tokens:
        # Nothing but filler ("send a photo"): treat it as "newest of that type"
        # and let a picture word imply images.
        if kind_key == "any" and any(t in _IMAGE_WORDS for t in q_tokens):
            allowed_ext = _IMAGE_EXT
        q_tokens = []
    q_compact = "".join(q_tokens)

    roots = _root_folders(kind_key)
    out["roots"] = [str(r) for r in roots]
    out["searched"] = [r.name for r in roots]
    out["approved_roots"] = sorted(_approved_keys())
    if not roots:
        out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return out

    deadline = started + _TIME_BUDGET_S
    files, truncated = _scan(roots, allowed_ext, deadline,
                             any_kind=(kind_key in ("any", "all", "file", "files", "auto")
                                       or kind_key not in _KIND_ALIASES))
    out["truncated"] = truncated

    if not q_compact:
        # Only a type was given ("send a photo"): newest files of that type.
        pool = [f for f in files if not wanted_ext or f["ext"] == wanted_ext]
        ranked = [_item(Path(f["path"]), Path(f["root"])) for f in pool]
        ranked.sort(key=lambda it: it["mtime"], reverse=True)
        top_score = 50.0
    else:
        scored = []
        for f in files:
            s = _score(f["name"], f["ext"], q_tokens, q_compact, wanted_ext)
            if s > 0:
                scored.append((s, f))
        scored.sort(key=lambda pair: (-pair[0], -pair[1]["mtime"],
                                      pair[1]["name"].casefold()))
        ranked = [_item(Path(f["path"]), Path(f["root"])) for _s, f in scored]
        top_score = scored[0][0] if scored else 0.0

    for item in ranked:
        item["folder"] = _folder_label(item)

    # De-duplicate (same file reachable from two roots, e.g. OneDrive link).
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for item in ranked:
        key = item["path"].casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    out["total"] = len(unique)
    out["matches"] = unique[:max(1, limit)]
    out["confidence"] = round(top_score, 1)
    if out["total"] == 1:
        # Only a real identification is sent without asking. A lone fuzzy hit
        # ("car" -> "cartoon") is offered for confirmation instead.
        if top_score >= _CONFIDENT:
            out["status"] = "ok"
            out["path"] = unique[0]["path"]
        else:
            out["status"] = "confirm"
    elif out["total"] > 1:
        out["status"] = "multiple"
    else:
        out["suggestions"] = _near_misses(files, q_compact)

    out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return out


def _near_misses(files: List[Dict[str, Any]], q_compact: str) -> List[str]:
    """Closest names when nothing matched, so the reply can be useful instead of
    just 'no'. Cheap: one SequenceMatcher pass over the scanned stems."""
    if not q_compact or not files:
        return []
    stems = {}
    for f in files:
        stems.setdefault("".join(_tokens(Path(f["name"]).stem)), f["name"])
    close = difflib.get_close_matches(q_compact, list(stems), n=4, cutoff=0.55)
    return [stems[c] for c in close]


# ── rendering for the model ─────────────────────────────────────────────────
def describe(res: Dict[str, Any]) -> str:
    """One compact string: the path, a choice list, or an honest 'not found'."""
    query = res.get("query") or ""
    searched = ", ".join(res.get("searched") or []) or "Desktop, Downloads, Pictures, Documents"
    status = res.get("status")

    if not query:
        return ('smart_file_search needs search_query — the words the user used '
                'for the file (e.g. "car", "red car", "eye"). Never ask the user '
                'for a path or an extension. [status: "failed", step: "input"]')

    if status == "ok":
        item = res["matches"][0]
        return (
            f'Found 1 file for "{query}": {item["path"]} '
            f'({item["size_text"]}, in {item["folder"]}).\n'
            f'Read it with file_reader(action=\'read_file\', file_path=<that path>). '
            f'Do not ask the user to confirm the path or the extension. '
            f'[status: ok, matches: 1]'
        )

    if status == "confirm":
        item = res["matches"][0]
        return (
            f'One file looks close to "{query}": {item["name"]} '
            f'({item["size_text"]}, in {item["folder"]}) -> {item["path"]}\n'
            f'The name is not an exact match, so do not open it silently: tell '
            f'the user which file you found and read it only if they agree. '
            f'[status: confirm, matches: 1]'
        )

    if status == "multiple":
        total = int(res.get("total") or 0)
        shown = len(res.get("matches") or [])
        lines = [
            f'Found {total} files for "{query}". Do not guess: ask the user which '
            f'one in one short sentence, then use the chosen absolute path with '
            f'file_reader(action=\'read_file\').'
        ]
        for i, item in enumerate(res.get("matches") or [], 1):
            lines.append(f'{i}. {item["name"]} (in {item["folder"]})  ->  {item["path"]}')
        if total > shown:
            lines.append(f'{total - shown} more matched — narrow the search_query '
                         f'if these are not the right ones.')
        lines.append(f'[status: multiple, matches: {total}, action: ask_user]')
        return "\n".join(lines)

    lines = [f'No file matching "{query}" in {searched}.']
    if res.get("truncated"):
        lines.append("(The folder scan hit its time/size cap, so a very deep "
                     "match may not have been seen.)")
    if res.get("suggestions"):
        lines.append("Closest names found: " + ", ".join(res["suggestions"]) + ".")
    lines.append(
        'Do not ask for an extension or an absolute path. Tell the user politely '
        'that you could not find it, and ask for one more word or which folder it '
        'might be in. [status: not_found]'
    )
    return "\n".join(lines)


def smart_file_search(parameters: dict, response=None, player=None,
                      session_memory=None) -> str:
    params = parameters or {}
    query = (params.get("search_query") or params.get("query")
             or params.get("name") or params.get("file_name") or "")
    kind = str(params.get("kind") or "any")
    try:
        limit = max(1, min(int(params.get("limit") or 8), _TIE_BREAK_MAX))
    except Exception:
        limit = 8

    res = resolve(str(query), kind=kind, limit=limit)
    _log(player, f'[Search] "{query}" via {kind}: {res["status"]} '
                 f'({res["total"]} match(es), {res["elapsed_ms"]} ms)')
    return describe(res)


TOOL = {
    "name": "smart_file_search",
    "description": (
        "Resolves a file name the user spoke into a real absolute path. Call "
        "this before sending any file, and whenever the user refers to a file "
        "by a description ('my car picture', 'the eye photo', 'that PDF').\n"
        "- Searches Desktop, Downloads, Pictures and Documents (plus their "
        "OneDrive copies) for common image, document, video, audio and archive "
        "types. Extension-agnostic: never ask the user for .png/.jpg/.pdf.\n"
        "- Fuzzy: case-insensitive substring, word and typo matching, so "
        "search_query='car' finds 'my_red_car_2023.jpg'.\n"
        "- One confident match: returns the absolute path — send it immediately.\n"
        "- One uncertain match (status: confirm): the name is only close, so say "
        "which file you found and send it only if the user agrees.\n"
        "- Several matches: returns a numbered list of names and paths — ask "
        "the user which one, then send that path.\n"
        "- None: returns not-found plus the closest names — say so politely and "
        "ask for one more word. Never demand a full path or an extension."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "search_query": {
                "type": "STRING",
                "description": ("What the user called the file: 'car', 'red car', "
                                "'eye', 'physics notes'. Do NOT add an extension "
                                "and do NOT guess a folder."),
            },
            "kind": {
                "type": "STRING",
                "enum": ["any", "image", "document", "video", "audio", "archive"],
                "description": ("Optional narrowing. Default 'any' already "
                                "covers every common type."),
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum options to list when several match. Default 8.",
            },
        },
        "required": ["search_query"],
    },
    "handler": smart_file_search,
}
