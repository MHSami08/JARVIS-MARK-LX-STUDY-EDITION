# -*- coding: utf-8 -*-
"""
Security Policy Enforcement for JARVIS Secure Study Edition:
1. Total Camera Disabler (blocks cv2.VideoCapture & any camera APIs).
2. Button/Hotkey-Only Screen Capture Guard (blocks voice/LLM autonomous screen reads).
3. Zero-Trust File Access Gatekeeper with explicit user permission prompts & whitelist.
   Files and whole folders can be approved; a folder grant is recursive.
"""

import os
import sys
import json
import time
import secrets
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
APPROVED_MANIFEST = DATA_DIR / "approved_files.json"

# Full disclosure inventory: every local storage location this build touches.
# The owner (administrator) has the absolute right to see where every byte,
# database, manifest and cache lives — nothing is hidden from them.
STORAGE_PATHS = {
    "study_vault_db": ROOT_DIR / "data" / "study_vault.db",
    "study_cache": ROOT_DIR / "memory" / "study_cache.json",
    "study_mistakes": ROOT_DIR / "memory" / "study_mistakes.json",
    "mistake_bank": ROOT_DIR / "memory" / "mistake_bank.json",
    "long_term_memory": ROOT_DIR / "memory" / "long_term.json",
    "approved_files_manifest": APPROVED_MANIFEST,
    "study_board_html": ROOT_DIR / "runtime" / "study_board.html",
    "manual_screen_capture": ROOT_DIR / "runtime" / "manual_study_capture.png",
    "session_exports": ROOT_DIR / "workspace" / "exports",
    "api_config": ROOT_DIR / "config" / "api_keys.json",
}


def storage_inventory() -> dict:
    """Absolute-path inventory of every local store (transparency reporting)."""
    out = {}
    for key, raw in STORAGE_PATHS.items():
        try:
            out[key] = str(Path(raw).resolve())
        except Exception:
            out[key] = str(raw)
    return out

# ---------------------------------------------------------------------------
# 1. TOTAL CAMERA HARDWARE BLOCK
# ---------------------------------------------------------------------------
class CameraAccessBlockedError(PermissionError):
    pass

def _blocked_video_capture(*args, **kwargs):
    raise CameraAccessBlockedError("SECURITY POLICY: Camera access is permanently disabled in JARVIS Study Edition.")

def enforce_camera_killswitch():
    """Monkey-patches cv2.VideoCapture if OpenCV is loaded anywhere."""
    try:
        import cv2
        cv2.VideoCapture = _blocked_video_capture
    except ImportError:
        pass

enforce_camera_killswitch()

# ---------------------------------------------------------------------------
# 2. BUTTON / SHORTCUT-ONLY SCREEN CAPTURE GUARD
# ---------------------------------------------------------------------------
_manual_screen_token = None
_token_expiry = 0.0

def issue_manual_screen_token(ttl_seconds: float = 8.0) -> str:
    """
    Called ONLY by the physical HUD Button click or explicit keyboard shortcut (F9).
    Never callable by LLM tool dispatch or voice commands.
    """
    global _manual_screen_token, _token_expiry
    _manual_screen_token = secrets.token_hex(16)
    _token_expiry = time.time() + ttl_seconds
    return _manual_screen_token

def consume_manual_screen_token(token: str) -> bool:
    global _manual_screen_token, _token_expiry
    if not token or _manual_screen_token is None:
        return False
    if time.time() > _token_expiry:
        _manual_screen_token = None
        return False
    if secrets.compare_digest(str(token), str(_manual_screen_token)):
        _manual_screen_token = None
        return True
    return False

def capture_screen_manual_only(token: str, save_path: str = None) -> str:
    """
    Captures the screen ONLY if a valid one-time token from the HUD button/hotkey is passed.
    """
    if not consume_manual_screen_token(token):
        raise PermissionError(
            "BLOCKED: Screen capture is restricted to manual HUD Button or Hotkey (F9) press only."
        )
    if save_path is None:
        runtime_dir = ROOT_DIR / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        save_path = str(runtime_dir / "manual_study_capture.png")

    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img.save(save_path)
        return save_path
    except Exception:
        import mss
        import mss.tools
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            sct_img = sct.grab(monitor)
            mss.tools.to_png(sct_img.rgb, sct_img.size, output=save_path)
        return save_path

# ---------------------------------------------------------------------------
# 3. ZERO-TRUST FILE GATEKEEPER
# ---------------------------------------------------------------------------
FORBIDDEN_PATTERNS = (
    ".env", "api_keys.json", "id_rsa", ".ssh", ".git",
    "AppData", "Windows", "System32", "Program Files",
    "Login Data", "Cookies", "Web Data"
)

# Matching is done per path COMPONENT, not as a substring of the whole path.
# The old substring test blocked harmless names (".github", "Windows_Notes",
# "Cookies_recipe.txt") while a component test blocks exactly the sensitive
# folders/files and nothing that merely contains the word.
_FORBIDDEN_DIR_NAMES = frozenset({
    ".git", ".ssh", ".aws", ".gnupg", "appdata", "windows", "system32",
    "syswow64", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information",
})
_FORBIDDEN_FILE_NAMES = frozenset({
    "login data", "cookies", "web data", "history", "local state",
    ".git-credentials", ".netrc", ".npmrc", ".pypirc", "known_hosts",
})
_FORBIDDEN_FILE_PREFIXES = ("api_keys", ".env", "id_rsa", "id_ed25519", "id_ecdsa")
_FORBIDDEN_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".kdbx")


def is_forbidden_path(file_path) -> bool:
    """True for system dirs, secrets, key material and browser profile files."""
    parts = [p.strip().lower() for p in
             str(file_path).replace("\\", "/").split("/") if p.strip()]
    if not parts:
        return False
    # Allow the generated study board internally, but nothing else under
    # runtime/: a blanket exception would let .env/.git slip through there.
    try:
        _board = str((ROOT_DIR / "runtime" / "study_board.html").resolve())
        if str(file_path).replace("\\", "/").lower() == _board.replace("\\", "/").lower():
            return False
    except Exception:
        pass
    for part in parts[:-1]:
        if part in _FORBIDDEN_DIR_NAMES:
            return True
    last = parts[-1]
    if last in _FORBIDDEN_DIR_NAMES or last in _FORBIDDEN_FILE_NAMES:
        return True
    if last.startswith(_FORBIDDEN_FILE_PREFIXES) or last.endswith(_FORBIDDEN_SUFFIXES):
        return True
    return False

def _load_manifest() -> dict:
    if not APPROVED_MANIFEST.exists():
        return {"approved_files": {}, "approved_folders": {}}
    try:
        data = json.loads(APPROVED_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {"approved_files": {}, "approved_folders": {}}
    if not isinstance(data, dict):
        return {"approved_files": {}, "approved_folders": {}}
    # Backward compatible with manifests written before folder support.
    data.setdefault("approved_files", {})
    data.setdefault("approved_folders", {})
    return data

def _save_manifest(data: dict):
    APPROVED_MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def _norm_path(p) -> str:
    """Case-insensitive, separator-normalised path string for prefix checks."""
    return os.path.normcase(os.path.normpath(str(p))).replace("\\", "/")

def _is_within(child_path, approved_folder) -> bool:
    """True when `child_path` is the folder itself or lives below it.

    Prefix matching uses a separator boundary, so an approved
    ``D:/StudyNotes`` never grants access to ``D:/StudyNotes-private``.
    """
    child = _norm_path(child_path)
    parent = _norm_path(approved_folder).rstrip("/")
    return child == parent or child.startswith(parent + "/")

def list_approved_files(include_folders: bool = True) -> list[str]:
    """Return every approved path: files first, then approved folders."""
    data = _load_manifest()
    entries = sorted(data.get("approved_files", {}).keys())
    if include_folders:
        entries += sorted(data.get("approved_folders", {}).keys())
    return entries

def list_approved_folders() -> list[str]:
    data = _load_manifest()
    return sorted(data.get("approved_folders", {}).keys())

DEFAULT_LIST_LIMIT = 500


def _walk_folder(folder: Path, recursive: bool = True,
                 limit: int = DEFAULT_LIST_LIMIT,
                 include_folders: bool = True) -> dict:
    """Depth-first walk of one folder, skipping forbidden and looping paths.

    Returns ``{"folder", "entries", "files", "folders", "count", "truncated"}``.
    Each entry is ``{"path", "name", "type", "size", "ext", "relative"}``.
    Symlinked directories are never descended, so a junction cannot bounce the
    walk back into an ancestor. No permission decision is made here: callers
    must have established access first (see list_directory_tree).
    """
    folder = Path(folder).expanduser().resolve()
    root = folder
    entries: list[dict] = []
    truncated = False
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(),
                              key=lambda p: (p.is_file(), p.name.lower()))
        except (PermissionError, OSError):
            continue
        for child in children:
            if len(entries) >= limit:
                truncated = True
                stack.clear()
                break
            if is_forbidden_path(child):
                continue
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                continue
            if is_dir and not include_folders:
                pass
            else:
                try:
                    relative = child.relative_to(root).as_posix()
                except (ValueError, OSError):
                    relative = child.name
                entries.append({
                    "path": str(child),
                    "name": child.name,
                    "type": "folder" if is_dir else "file",
                    "size": size,
                    "ext": "" if is_dir else child.suffix.lower().lstrip("."),
                    "relative": relative,
                })
            if recursive and is_dir and not child.is_symlink():
                stack.append(child)
    files = [e for e in entries if e["type"] == "file"]
    folders = [e for e in entries if e["type"] == "folder"]
    return {
        "folder": str(folder),
        "entries": entries,
        "files": files,
        "folders": folders,
        "count": len(entries),
        "truncated": truncated,
    }


def list_directory_tree(folder_path: str, recursive: bool = True,
                        limit: int = DEFAULT_LIST_LIMIT,
                        include_folders: bool = True,
                        require_approved: bool = True) -> dict:
    """List an approved folder; recursive by default.

    An approved parent folder covers every child file and subfolder, so this is
    the helper JARVIS uses to look inside an approved tree instead of asking
    the user for one absolute path per file. With ``require_approved`` (the
    default) an unapproved folder raises PermissionError; pass False only
    after the caller has run request_verified_folder_access itself.
    """
    if not folder_path:
        raise ValueError("Empty folder path provided.")
    resolved = Path(folder_path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Folder does not exist: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Path is not a folder: {resolved}")
    if is_forbidden_path(resolved):
        raise PermissionError(
            f"SECURITY BLOCK: Access to '{resolved.name}' is permanently restricted.")
    if require_approved:
        if str(resolved) not in _load_manifest().get("approved_folders", {}) \
                and find_approved_folder(str(resolved)) is None:
            raise PermissionError(
                f"Folder is not approved for JARVIS access: {resolved}. "
                "Approve it with the HUD '🔒 Approve Study Files' button first.")
    result = _walk_folder(resolved, recursive=recursive, limit=limit,
                          include_folders=include_folders)
    result["approved_folder"] = find_approved_folder(str(resolved))
    result["recursive"] = bool(recursive)
    return result


def list_approved_tree(limit: int = DEFAULT_LIST_LIMIT,
                       recursive: bool = True) -> dict:
    """List every approved folder plus every file already covered by a grant."""
    folders = list_approved_folders()
    files = sorted(_load_manifest().get("approved_files", {}).keys())
    entries: list[dict] = []
    per_folder: dict[str, int] = {}
    truncated = False
    for folder in folders:
        try:
            listing = _walk_folder(Path(folder), recursive=recursive,
                                   limit=max(1, limit - len(entries)))
        except Exception:
            continue
        per_folder[folder] = listing["count"]
        entries.extend(listing["entries"])
        if listing["truncated"] or len(entries) >= limit:
            truncated = True
            break
    for path in files:
        if len(entries) >= limit:
            truncated = True
            break
        if is_forbidden_path(Path(path)):
            continue
        if any(_is_within(path, folder) for folder in folders):
            continue  # already present through its folder grant
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        entries.append({
            "path": path,
            "name": Path(path).name,
            "type": "file",
            "size": size,
            "ext": Path(path).suffix.lower().lstrip("."),
            "relative": Path(path).name,
        })
    return {
        "approved_folders": folders,
        "per_folder": per_folder,
        "entries": entries,
        "count": len(entries),
        "truncated": truncated,
    }


def find_approved_folder(file_path: str) -> str | None:
    """Return the approved folder that covers this path, if any."""
    for folder in list_approved_folders():
        try:
            if _is_within(file_path, folder):
                return folder
        except Exception:
            continue
    return None

def is_path_approved(file_path: str) -> bool:
    """True when the exact file or one of its parent folders is approved."""
    resolved = Path(file_path).expanduser().resolve()
    data = _load_manifest()
    if str(resolved) in data.get("approved_files", {}):
        return True
    return find_approved_folder(str(resolved)) is not None

def approve_file_permanently(file_path: str) -> str:
    resolved = Path(file_path).expanduser().resolve()
    if resolved.is_dir():
        return approve_folder_permanently(str(resolved))
    p = str(resolved)
    if is_forbidden_path(Path(p)):
        raise PermissionError(f"Access to sensitive system path '{p}' is strictly forbidden.")
    data = _load_manifest()
    data.setdefault("approved_files", {})[p] = {
        "approved_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    _save_manifest(data)
    return p

def approve_folder_permanently(folder_path: str) -> str:
    """Approve a folder and, recursively, everything beneath it.

    Individual files inside an approved folder no longer prompt. The
    forbidden-path check still runs per file at access time, so a `.git`
    directory or `.env` nested in the approved tree stays blocked.
    """
    if not folder_path:
        raise ValueError("Empty folder path provided.")
    resolved = Path(folder_path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise NotADirectoryError(f"Folder does not exist: {resolved}")
    p = str(resolved)
    if is_forbidden_path(resolved):
        raise PermissionError(f"Access to sensitive system folder '{p}' is strictly forbidden.")
    data = _load_manifest()
    data.setdefault("approved_folders", {})[p] = {
        "approved_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    # A folder grant supersedes individual file grants inside it.
    files = data.get("approved_files", {})
    for key in [k for k in files if _is_within(k, p)]:
        files.pop(key, None)
    _save_manifest(data)
    return p

def revoke_file_access(file_path: str):
    p = str(Path(file_path).resolve())
    data = _load_manifest()
    removed = False
    if p in data.get("approved_files", {}):
        del data["approved_files"][p]
        removed = True
    if p in data.get("approved_folders", {}):
        del data["approved_folders"][p]
        removed = True
    if removed:
        _save_manifest(data)

def revoke_folder_access(folder_path: str):
    revoke_file_access(folder_path)

def ask_user_file_permission_dialog(file_path: str, is_folder: bool = False) -> str:
    """
    Pops up a synchronous native Windows MessageBox / Qt Dialog asking the user:
    Returns: 'ALWAYS', 'ONCE', or 'DENY'
    """
    p = str(Path(file_path).resolve())
    kind = "folder" if is_folder else "file"
    scope = "this folder and everything inside it" if is_folder else "this file"
    try:
        import ctypes
        MB_YESNOCANCEL = 0x00000003
        MB_ICONWARNING = 0x00000030
        MB_TOPMOST = 0x00040000
        title = "JARVIS Study Security - Permission Request"
        body = (
            f"JARVIS is requesting permission to read this {kind} for your study session:\n\n"
            f"{p}\n\n"
            f"Click [YES] to ALWAYS allow {scope} (Add to Approved List)\n"
            f"Click [NO] to allow ONCE for this session only\n"
            f"Click [CANCEL] to DENY access"
        )
        res = ctypes.windll.user32.MessageBoxW(
            0, body, title, MB_YESNOCANCEL | MB_ICONWARNING | MB_TOPMOST
        )
        # IDYES = 6, IDNO = 7, IDCANCEL = 2
        if res == 6:
            return "ALWAYS"
        elif res == 7:
            return "ONCE"
        return "DENY"
    except Exception:
        return "DENY"

def request_verified_file_access(file_path: str) -> Path:
    """
    Validate file access. Approved folders are checked as directory prefixes,
    so files inside an approved folder never trigger another popup. Anything
    outside the whitelist is blocked and asks the user first.
    """
    if not file_path:
        raise ValueError("Empty file path provided.")
    resolved = Path(file_path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File does not exist: {resolved}")
    if resolved.is_dir():
        raise IsADirectoryError(
            f"'{resolved}' is a folder. Approve it as a folder or use list_folder.")
    if is_forbidden_path(resolved):
        raise PermissionError(f"SECURITY BLOCK: Access to '{resolved.name}' is permanently restricted.")

    key = str(resolved)
    manifest = _load_manifest()
    if key in manifest.get("approved_files", {}):
        return resolved
    if find_approved_folder(key) is not None:
        return resolved

    decision = ask_user_file_permission_dialog(key)
    if decision == "ALWAYS":
        approve_file_permanently(key)
        return resolved
    elif decision == "ONCE":
        return resolved
    else:
        raise PermissionError(f"User denied read access to file: {resolved.name}")

def request_verified_folder_access(folder_path: str) -> Path:
    """Validate folder access, prompting once unless it is already approved."""
    if not folder_path:
        raise ValueError("Empty folder path provided.")
    resolved = Path(folder_path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise NotADirectoryError(f"Folder does not exist: {resolved}")
    if is_forbidden_path(resolved):
        raise PermissionError(f"SECURITY BLOCK: Access to '{resolved.name}' is permanently restricted.")

    key = str(resolved)
    if key in _load_manifest().get("approved_folders", {}):
        return resolved
    if find_approved_folder(key) is not None:
        return resolved

    decision = ask_user_file_permission_dialog(key, is_folder=True)
    if decision == "ALWAYS":
        approve_folder_permanently(key)
        return resolved
    elif decision == "ONCE":
        return resolved
    else:
        raise PermissionError(f"User denied read access to folder: {resolved.name}")

def request_verified_path_access(path: str) -> Path:
    """Dispatch to the file or folder guard based on what the path is."""
    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        return request_verified_folder_access(str(resolved))
    return request_verified_file_access(str(resolved))

def request_verified_write_access(file_path: str) -> Path:
    """Validate write/modify access for an existing file or a new file.

    An approved folder grants writes anywhere beneath it. An existing
    individual file must be approved. A new path outside the whitelist asks the
    user to approve its parent folder before anything is created.
    """
    if not file_path:
        raise ValueError("Empty file path provided.")
    resolved = Path(file_path).expanduser().resolve()
    if is_forbidden_path(resolved):
        raise PermissionError(f"SECURITY BLOCK: Access to '{resolved.name}' is permanently restricted.")

    if resolved.exists():
        if resolved.is_dir():
            request_verified_folder_access(str(resolved))
            return resolved
        return request_verified_file_access(str(resolved))

    parent = resolved.parent
    if not parent.exists():
        raise FileNotFoundError(f"Parent folder does not exist: {parent}")
    if find_approved_folder(str(parent)) is not None:
        return resolved
    # The user must deliberately grant the folder before a new file is created.
    request_verified_folder_access(str(parent))
    return resolved
