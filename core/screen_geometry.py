"""Screen geometry helpers: DPI awareness and monitor layout.

`ensure_dpi_awareness()` must run before QApplication is built. Windows treats a
process as DPI-unaware by default, so a 1920x1080 display at 150% scaling
reports 1280x720 and every screenshot (including the F9 study capture) comes
back blurry and upscaled. It is pinned once, early, to PerMonitor-v2.

Coordinates here are PHYSICAL pixels measured from the top-left of the primary
monitor (the virtual desktop can extend to negative x/y). Qt works in LOGICAL
pixels; `scale_factor` reports the difference for the startup log line.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass

try:
    import mss as _mss
except ImportError:  # pragma: no cover - mss is a hard requirement
    _mss = None

_IS_WINDOWS = sys.platform == "win32"

# ── DPI awareness ────────────────────────────────────────────────────────────
# Set once per process; Windows ignores every later attempt. `_dpi_lock` makes
# the "have we done this yet" check safe when the UI thread and a worker thread
# race to be first.
_dpi_lock = threading.Lock()
_dpi_done = False
_dpi_mode = "unset"


def ensure_dpi_awareness() -> str:
    """Declare this process DPI-aware, at the strongest mode available.

    Returns the mode that ended up applying, as a short string, so a caller can
    log it. Safe to call from anywhere and any number of times: only the first
    call does anything, and a failure is never fatal — the app still runs, it
    just measures the screen in the OS's scaled units instead of real pixels.

    Call this as the FIRST statement of main(), before QApplication is built.
    Qt sets PerMonitor-v1 for itself at construction and will not upgrade, so if
    Qt gets there first this can only ever fall back to v1.
    """
    global _dpi_done, _dpi_mode
    with _dpi_lock:
        if _dpi_done:
            return _dpi_mode
        _dpi_done = True

        if not _IS_WINDOWS:
            _dpi_mode = "not-windows"
            return _dpi_mode

        # PerMonitor-v2, and it has to be requested through
        # SetProcessDpiAwarenessContext. The obvious-looking alternative,
        # shcore.SetProcessDpiAwareness(2), is PROCESS_PER_MONITOR_DPI_AWARE —
        # which is PerMonitor **v1**, not v2. Both make GetProcessDpiAwareness
        # report 2, so the difference is invisible unless you compare the
        # context handle itself, which is how this was caught:
        #   shcore(2)                            -> NOT equal to PMv2
        #   SetProcessDpiAwarenessContext(-4)    -> equal to PMv2
        # v2 is what reports true physical pixels on a scaled display and what
        # rescales correctly when a window crosses between monitors of
        # different scaling.
        try:
            u = ctypes.windll.user32
            u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 is the pseudo-handle -4.
            if u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4 & 0xFFFFFFFFFFFFFFFF)):
                _dpi_mode = "per-monitor-v2"
                return _dpi_mode
        except Exception:
            pass

        # PerMonitor v1 — Windows 8.1 and later.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            _dpi_mode = "per-monitor-v1"
            return _dpi_mode
        except Exception:
            pass

        # System-DPI-aware: correct on a single display, wrong across monitors
        # of different scaling. Better than being unaware.
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            _dpi_mode = "system-aware"
            return _dpi_mode
        except Exception:
            pass

        _dpi_mode = "unaware"
        return _dpi_mode


def dpi_mode() -> str:
    """What `ensure_dpi_awareness` settled on, or "unset" if never called."""
    return _dpi_mode


# ── Monitors ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Monitor:
    """One physical display, in physical pixels of the virtual desktop.

    `index` is mss's own numbering, which is what `capture(monitor=...)` takes.
    Note it starts at 1: mss's index 0 is the bounding box of every display
    combined, which is not a monitor anyone can point at.
    """
    index: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False
    name: str = ""

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) — the shape mss.grab accepts."""
        return (self.left, self.top, self.width, self.height)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


def _mss_available() -> bool:
    return _mss is not None


def monitors() -> list[Monitor]:
    """Every physical display, primary first.

    mss's list already puts the primary at index 1, but it is sorted explicitly
    here so callers can rely on `monitors()[0]` being the primary rather than
    depending on an mss implementation detail.
    """
    if not _mss_available():
        # Last-ditch fallback so callers never have to handle None. Unaware
        # processes get scaled numbers here, which is exactly the bug this
        # module exists to avoid — hence the warning.
        if _IS_WINDOWS:
            u = ctypes.windll.user32
            w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            print("[ScreenGeometry] mss unavailable; falling back to GetSystemMetrics "
                  "(coordinates may be DPI-scaled)")
            return [Monitor(1, 0, 0, w, h, True, "primary")]
        return [Monitor(1, 0, 0, 1920, 1080, True, "primary")]

    with _mss.MSS() as sct:
        raw = list(sct.monitors)
    if not raw:
        return []

    # raw[0] is the union of all displays; raw[1:] are the real ones.
    real = raw[1:] if len(raw) > 1 else raw
    out: list[Monitor] = []
    for i, m in enumerate(real, start=1):
        out.append(Monitor(
            index=i,
            left=int(m["left"]), top=int(m["top"]),
            width=int(m["width"]), height=int(m["height"]),
            is_primary=bool(m.get("is_primary", i == 1)),
            name=str(m.get("name", "")),
        ))
    out.sort(key=lambda m: (not m.is_primary, m.index))
    return out


def primary() -> Monitor:
    """The primary display. Never returns None; synthesises one if mss is out."""
    ms = monitors()
    if not ms:
        return Monitor(1, 0, 0, 1920, 1080, True, "primary")
    for m in ms:
        if m.is_primary:
            return m
    return ms[0]


def virtual_desktop() -> Monitor:
    """The bounding box of every display, as one Monitor.

    Its origin can be negative, which is why callers must not assume (0, 0).
    Use this to capture everything at once.
    """
    ms = monitors()
    if not ms:
        return primary()
    left = min(m.left for m in ms)
    top = min(m.top for m in ms)
    right = max(m.right for m in ms)
    bottom = max(m.bottom for m in ms)
    return Monitor(0, left, top, right - left, bottom - top, False, "virtual-desktop")


# ── Physical <-> logical ─────────────────────────────────────────────────────
# Qt positions top-level windows in logical pixels; everything in this module is
# physical. The ratio is per-monitor, so the caller says which monitor the point
# is on and this reads that monitor's Qt screen. Imported lazily because this
# module is imported before QApplication exists.

def _qt_screen_for(monitor: Monitor | None):
    try:
        from PyQt6.QtGui import QGuiApplication
    except ImportError:
        return None, 1.0
    app = QGuiApplication.instance()
    if app is None:
        return None, 1.0
    screens = app.screens()
    if not screens:
        return None, 1.0

    target = monitor or primary()
    # Match on the screen's PHYSICAL geometry against the monitor's rect. Qt
    # reports screen geometry in logical pixels, so multiply back by the ratio
    # to compare like with like.
    for s in screens:
        g = s.geometry()
        dpr = float(s.devicePixelRatio() or 1.0)
        if (round(g.left() * dpr), round(g.top() * dpr),
                round(g.width() * dpr), round(g.height() * dpr)) == target.rect:
            return s, dpr
    # No exact match (monitor layout changed since Qt started): fall back to
    # the screen whose physical origin is closest to the monitor's.
    def dist(s) -> int:
        g = s.geometry()
        dpr = float(s.devicePixelRatio() or 1.0)
        return abs(round(g.left() * dpr) - target.left) + abs(round(g.top() * dpr) - target.top)
    s = min(screens, key=dist)
    return s, float(s.devicePixelRatio() or 1.0)


def scale_factor(monitor: Monitor | None = None) -> float:
    """That monitor's Qt device-pixel-ratio. 1.0 when Qt is not running.

    This is the number to divide by when turning a physical measurement into
    something `setGeometry` will honour.
    """
    _, dpr = _qt_screen_for(monitor)
    return dpr or 1.0


# ── Cursor ───────────────────────────────────────────────────────────────────


# ── Capture ──────────────────────────────────────────────────────────────────


# A change smaller than this many pixels is treated as noise. The measured floor
# is zero, so this only has to clear genuine small artefacts — a caret, a
# subpixel-shifted glyph — while staying well under the ~40 px a single
# character change produces.
_MIN_CHANGED_PX = 24


def describe() -> str:
    """A one-line summary for the log — the thing to read when a click misses."""
    ms = monitors()
    parts = [f"dpi={dpi_mode()}"]
    for m in ms:
        parts.append(f"#{m.index}{'(primary)' if m.is_primary else ''} "
                     f"{m.width}x{m.height}@{m.left},{m.top}"
                     f"{' scale=' + str(scale_factor(m)) if scale_factor(m) != 1.0 else ''}")
    vd = virtual_desktop()
    parts.append(f"virtual={vd.width}x{vd.height}@{vd.left},{vd.top}")
    return " | ".join(parts)
