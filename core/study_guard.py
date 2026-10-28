"""
Study Mode safety lock: while Study Mode is on, the physical webcam is OFF-LIMITS.
The desktop screen (screen capture / OCR) stays allowed - that is how JARVIS
reads question papers and PDF books.

Pure-Python and Qt-free so main.py, ui.py, plugins and actions can all import it.
The lock is set synchronously by whoever turns Study Mode on, so there is no
window between "mode on" and "camera refused".

Any code that opens the webcam should call require_camera() first:

    from core import study_guard
    study_guard.require_camera()        # raises CameraLocked in Study Mode
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_active = False

CAMERA_REFUSAL = (
    "[STUDY_MODE_LOCK] The webcam is locked while Study Mode is on; it was not "
    "touched. Do not retry. Tell the user in one short sentence that the camera "
    "stays off in Study Mode and they can leave Study Mode first if they want it. "
    "The desktop screen can still be read: screen_process with angle='screen'."
)


class CameraLocked(RuntimeError):
    """Raised when something tries to open the webcam during Study Mode."""


def set_study_active(on: bool) -> None:
    global _active
    with _lock:
        _active = bool(on)


def study_active() -> bool:
    with _lock:
        return _active


def camera_locked() -> bool:
    return study_active()


def require_camera() -> None:
    if camera_locked():
        raise CameraLocked("webcam is locked while Study Mode is on")
