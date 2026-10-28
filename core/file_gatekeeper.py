# -*- coding: utf-8 -*-
"""
ZERO-TRUST FILE ACCESS GATEKEEPER (`core/file_gatekeeper.py`).

Stable import surface for the gatekeeper the Study Edition mandates. The real
implementation lives in `core/security_policy.py` (it also owns the camera
kill-switch and the manual-only screen-capture token), and both names stay
importable so nothing here ever becomes a second copy of the truth:

    from core.file_gatekeeper import request_verified_file_access

Every approved path lives in `data/approved_files.json` (absolute:
core.security_policy.APPROVED_MANIFEST). Unapproved reads are blocked and an
explicit native permission dialog is shown to the user — Allow [Once] /
[Always for this file] / [Deny]. System directories, browser profiles, .env,
SSH keys and hidden folders are denied unconditionally.
"""

from core.security_policy import (  # noqa: F401
    APPROVED_MANIFEST,
    FORBIDDEN_PATTERNS,
    ask_user_file_permission_dialog,
    approve_file_permanently,
    approve_folder_permanently,
    is_forbidden_path,
    is_path_approved,
    list_approved_files,
    list_approved_folders,
    request_verified_file_access,
    request_verified_folder_access,
    request_verified_path_access,
    request_verified_write_access,
    revoke_file_access,
    revoke_folder_access,
    storage_inventory,
)

# Transparency: where the approved-path whitelist physically lives.
APPROVED_FILES_JSON = APPROVED_MANIFEST
