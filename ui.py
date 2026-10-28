from __future__ import annotations
import threading as _th

import html
import json
import math
import re
import os
import platform
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

from PyQt6.QtCore import (
    QEasingCurve, QLineF, QObject, QPointF,
    QPoint, QPropertyAnimation, QRectF, Qt, QTimer,
    pyqtSignal, QEvent, QVariantAnimation,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent,
    QFont as _QtFont, QPainter as _QtPainter, QPalette,
    QKeySequence, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsDropShadowEffect, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

# Study Station. Imported here, at module level, on purpose: QtWebEngineWidgets
# has to be loaded before the QApplication exists.
from core import study_guard
from core.study_canvas import StudyCanvasWidget, HAVE_STUDY
try:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
except Exception:
    pass

# ── Native readability ──────────────────────────────────────────────────────
# One platform-appropriate monospace face for the whole HUD. Every widget below
# asks for "Courier New" (the legacy default); the QFont wrapper maps that name
# to the real UI font, so no call site needs to change and nothing is
# monkey-patched at runtime.
_SYSTEM = platform.system()          # "Windows" | "Darwin" | "Linux"
MONO_FONT = {"Windows": "Cascadia Code",
             "Darwin":  "SF Mono"}.get(_SYSTEM, "DejaVu Sans Mono")
_MONO_FALLBACKS = {"Windows": ["Consolas", "Courier New"],
                   "Darwin":  ["Menlo", "Monaco", "Courier"]}.get(
                       _SYSTEM, ["Liberation Mono", "Monospace"])
_LEGACY_FAMILIES = {"courier new", "courier", "monospace", "consolas"}
# The old HUD drew most text at 5-8 pt. Raise the tiny sizes when a legacy
# family name is mapped (the UI font is narrower than Courier New, so the
# layouts keep their widths).
_SIZE_MAP = {5: 9, 6: 9, 7: 9, 8: 10, 9: 10, 10: 11}


class QFont(_QtFont):
    """QFont whose legacy monospace names resolve to the platform UI font."""

    def __init__(self, *args, **kwargs):
        if args and isinstance(args[0], str) and args[0].lower() in _LEGACY_FAMILIES:
            args = list(args)
            args[0] = MONO_FONT
            if len(args) > 1 and isinstance(args[1], int) and args[1] in _SIZE_MAP:
                args[1] = _SIZE_MAP[args[1]]
            args = tuple(args)
        super().__init__(*args, **kwargs)
        if self.family() == MONO_FONT:
            # If the preferred face isn't installed Qt falls through this list.
            self.setFamilies([MONO_FONT] + _MONO_FALLBACKS)



def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Single source of truth for the release name — the window title, the header
# badge and the readme must never disagree again.
APP_VERSION  = "MARK LV"
APP_PROTOCOL = APP_VERSION.split()[-1]

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    # Surfaces — deep, slightly cool, so translucent glass layers read clearly.
    BG        = "#0a0e13"
    PANEL     = "#10161d"
    PANEL2    = "#131a22"
    GLASS     = "#06101a"      # base tint of the acrylic panels (used with alpha)
    BORDER    = "#223040"
    BORDER_B  = "#34506a"
    BORDER_A  = "#2a4258"
    # Accent — cyan/teal for borders, core rings, status badges, highlights.
    PRI       = "#00d4ff"
    PRI_DIM   = "#007a99"
    PRI_GHO   = "#001f2e"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    # Text — neutral and high-contrast; never re-tinted by the accent colour.
    TEXT      = "#e6edf3"      # primary
    TEXT_MED  = "#b6c2cd"      # secondary
    TEXT_DIM  = "#8b98a6"      # muted
    WHITE     = "#ffffff"
    DARK      = "#070a0e"
    BAR_BG    = "#121a22"


def _rgba(hex_colour: str, alpha: float) -> str:
    """'#rrggbb' + alpha -> 'rgba(r, g, b, a)' for Qt style sheets."""
    h = hex_colour.lstrip("#")
    return f"rgba({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}, {alpha:.2f})"


def _glass_border(strong: bool = False) -> str:
    """Subtle neon edge for glass cards (follows the accent colour)."""
    return _rgba(C.PRI, 0.32 if strong else 0.2)


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed.
# Text colours are deliberately absent: they stay neutral whatever accent is set.
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "GLASS", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}



def _txt(hex_colour: str) -> str:
    """Text drawn in an accent/border colour is hard to read (thin, saturated
    on dark). The accent stays on borders, rings and fills; the *letters* get
    the neutral text palette instead."""
    h = (hex_colour or "").lower()
    if h == C.PRI.lower():
        return C.TEXT
    if h == C.PRI_DIM.lower():
        return C.TEXT_MED
    if h in (C.BORDER.lower(), C.BORDER_B.lower(), C.BORDER_A.lower()):
        return C.TEXT_DIM
    return hex_colour


# Type scale for the HUD shell: one size per role, hierarchy through weight and
# colour rather than a different size on every widget.
_FS_CAPTION = 9      # hints, dates, footer, tags
_FS_BODY    = 10     # labels, list values, button text
_FS_DATA    = 11     # headline numbers (metrics)
_FS_TITLE   = 18     # assistant name


def _f(size: int, bold: bool = False) -> QFont:
    return QFont(MONO_FONT, size,
                 QFont.Weight.Bold if bold else QFont.Weight.Normal)


def _section_label(text: str) -> QLabel:
    """Section heading: Title Case, bold, primary text — always the same look."""
    l = QLabel(text)
    l.setFont(_f(_FS_BODY, True))
    l.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none; "
                    f"padding: 2px 0 1px 0;")
    return l


class QPainter(_QtPainter):
    """QPainter that draws accent-coloured text in the neutral text colour."""

    def drawText(self, *args):
        pen = self.pen()
        n = _txt(pen.color().name())
        if n.lower() == pen.color().name().lower():
            return super().drawText(*args)
        swap = QPen(pen)
        c = QColor(n); c.setAlpha(pen.color().alpha())
        swap.setColor(c)
        self.setPen(swap)
        try:
            return super().drawText(*args)
        finally:
            self.setPen(pen)


def _apply_app_theme(app: QApplication) -> None:
    """App-wide UI font and a dark palette, so any widget without its own
    style sheet still lands on the glass theme instead of Fusion's grey."""
    f = QFont(MONO_FONT, 10)
    f.setStyleStrategy(_QtFont.StyleStrategy.PreferAntialias)
    app.setFont(f)
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor(C.BG))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(C.TEXT))
    pal.setColor(QPalette.ColorRole.Base,            QColor(C.PANEL))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(C.PANEL2))
    pal.setColor(QPalette.ColorRole.Text,            QColor(C.TEXT))
    pal.setColor(QPalette.ColorRole.Button,          QColor(C.PANEL2))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor(C.TEXT))
    pal.setColor(QPalette.ColorRole.ToolTipBase,     QColor(C.PANEL))
    pal.setColor(QPalette.ColorRole.ToolTipText,     QColor(C.TEXT))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(C.PRI_DIM))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(C.TEXT_DIM))
    app.setPalette(pal)


DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Re-derives the whole teal-family palette from the chosen accent colour
    (hue shift — brightness/saturation ratios are preserved, design stays intact).
    Painted elements (HUD, waveform, metrics) pick up the new colour on the next
    frame; stylesheet-based panels pick it up when they are rebuilt.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # near-grey accent → the whole theme is desaturated

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    """A snapshot of the accent-linked colours currently on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    LIVE full theme change. Replaces the old palette colours with the new ones
    in EVERY widget's stylesheet across the app and repaints them. This way the
    colour change applies INSTANTLY across the whole interface — panels, buttons,
    borders included — not just the painted elements. No restart needed.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return

    def _rgba_prefix(h: str) -> str:
        h = h.lstrip("#")
        return f"rgba({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)},"

    # Glass surfaces are written as rgba(r, g, b, a) — retheme those too.
    mapping.update({_rgba_prefix(o): _rgba_prefix(n) for o, n in list(mapping.items())})
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        # Probe caches — GPU (NVML) and temperature (WMI) are the expensive
        # queries; initialise their handles once and reuse them instead of
        # rebuilding a connection on every poll.
        self._slow_tick = 0            # gpu/temp refreshed every 3rd cycle
        self._pynvml    = None         # cached pynvml module + device handle
        self._pynvml_h  = None
        self._pynvml_ok = None         # None=untested, False=unavailable here
        self._nv_unix   = None         # cached (lib, dev) for Linux/macOS NVML
        self._wmi_conn  = None         # cached WMI connection (creating one is slow)
        self._wmi_ok    = None         # None=untested, False=unavailable here
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(2.0)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        # GPU and temperature change slowly and are the most expensive probes
        # (NVML / WMI) — refresh them every 3rd cycle (~6 s) instead of every
        # cycle, reusing the previous reading in between.
        self._slow_tick = (self._slow_tick + 1) % 3
        if self._slow_tick == 1:
            gpu = self._get_gpu()
            tmp = self._get_temp()
        else:
            gpu = self.gpu
            tmp = self.tmp

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free; initialise once and reuse the handle.
        # Re-initialising NVML on every poll is slow, so cache it and stop
        # retrying pynvml entirely once it proves unavailable here.
        if self._pynvml_ok is not False:
            try:
                if self._pynvml_h is None:
                    import warnings
                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", category=FutureWarning)
                        import pynvml  # type: ignore
                    pynvml.nvmlInit()
                    self._pynvml    = pynvml
                    self._pynvml_h  = pynvml.nvmlDeviceGetHandleByIndex(0)
                    self._pynvml_ok = True
                return float(self._pynvml.nvmlDeviceGetUtilizationRates(self._pynvml_h).gpu)
            except Exception:
                self._pynvml_ok = False

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes — init once, reuse
        try:
            import ctypes

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            if self._nv_unix is None:
                _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"
                nv = ctypes.CDLL(_lib)
                nv.nvmlInit_v2()
                dev = ctypes.c_void_p()
                nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
                self._nv_unix = (nv, dev)

            nv, dev = self._nv_unix
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess). Reuse a single
        # connection — building a fresh wmi.WMI() on every poll spins up a COM
        # connection each time and is very slow. Give up after one failure.
        if _OS == "Windows" and self._wmi_ok is not False:
            try:
                if self._wmi_conn is None:
                    import wmi  # type: ignore
                    self._wmi_conn = wmi.WMI(namespace="root/wmi")
                tz = self._wmi_conn.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                self._wmi_ok   = False
                self._wmi_conn = None

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        # Arc Reactor Core is the exclusive permanent HUD centerpiece.
        self.hud_style = "core"
        self._core_phase = 0.0

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._step_t     = time.time()
        self._blink      = True
        self._blink_tick = 0

        # Rescaled-face cache: the smooth rescale is expensive, so we keep the
        # last result and only rebuild it when the (quantised) size changes.

        # Static grid-dot layer, pre-rendered once per size/theme into a pixmap
        # so paintEvent blits it in one call instead of thousands of drawPoint()s.
        self._grid_cache: QPixmap | None = None
        self._grid_key = None
        # Repaint throttle counter (idle frames drop to ~20 Hz — see _step()).
        self._paint_tick = 0

        # Live audio reactivity: _live_amp is written from the audio threads
        # (0.0–1.0), _amp_disp is the smoothed value the paint code reads.
        self._live_amp  = 0.0
        self._amp_disp  = 0.0
        self._base_scale = 1.0    # slow "breathing" target; amp is added per-frame
        self._base_halo  = 55.0

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def set_audio_level(self, level: float) -> None:
        """Thread-safe entry point for the audio threads. Stores the louder of
        the incoming level and the current value so brief gaps between chunks
        don't make the waveform stutter; _step() decays it back down."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        if lv < 0.0:
            lv = 0.0
        elif lv > 1.0:
            lv = 1.0
        if lv > self._live_amp:
            self._live_amp = lv

    def _make_grid(self, W: int, H: int) -> QPixmap:
        """Pre-render the static grid-dot background into a transparent pixmap so
        paintEvent can blit it once per frame instead of running a nested
        drawPoint() loop across the whole widget every 16 ms."""
        pm = QPixmap(max(1, W), max(1, H))
        pm.fill(Qt.GlobalColor.transparent)
        gp = QPainter(pm)
        gp.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                gp.drawPoint(x, y)
        gp.end()
        return pm

    def _step(self):
        self._tick += 1
        now = time.time()

        # Audio threads push peaks into _live_amp; decay it toward silence so
        # gaps between chunks fade out instead of freezing, then smooth it.
        self._live_amp *= 0.86
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.45
        amp = self._amp_disp

        dt = now - self._step_t
        self._step_t = now
        # Integrated phase for Arc Reactor ring rotations
        self._core_phase += min(0.10, max(0.0, dt))

        # Arc Reactor Core: slow "breathing" base target, lifted by the level.
        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._base_scale = 1.03
                self._base_halo  = 122.0
            elif self.muted:
                self._base_scale = random.uniform(0.998, 1.002)
                self._base_halo  = random.uniform(15, 28)
            else:
                self._base_scale = random.uniform(1.001, 1.008)
                self._base_halo  = random.uniform(48, 68)
            self._last_t = now

        if self.muted:
            self._tgt_scale, self._tgt_halo = self._base_scale, self._base_halo
        elif self.speaking:
            self._tgt_scale = self._base_scale + amp * 0.13
            self._tgt_halo  = self._base_halo  + amp * 95.0
        else:
            self._tgt_scale = self._base_scale + amp * 0.06
            self._tgt_halo  = self._base_halo  + amp * 75.0

        sp = 0.38 if self.speaking else (0.30 if amp > 0.02 else 0.15)
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
            _blinked = True
        else:
            _blinked = False

        # Repaint throttling — advancing the animation state above is cheap at
        # 60 Hz, but the paint is heavy. Active (speaking, audio, thinking) runs
        # at ~30 Hz, which is the frame rate animation has used for talking
        # characters forever and is indistinguishable here; idle drops to ~20 Hz
        # so a sleeping HUD stops pinning a CPU core. The visuals stay smooth
        # either way because the animation state keeps stepping at 60 Hz.
        self._paint_tick = (self._paint_tick + 1) % 6
        active = (self.speaking or amp > 0.02
                  or self.state in ("THINKING", "PROCESSING"))
        if _blinked or (self._paint_tick % 2 == 0 if active
                        else self._paint_tick % 3 == 0):
            # Nothing is on screen when the window is hidden or minimised, so
            # rendering the core into it is pure waste — and this app is meant
            # to sit running all day. The animation state above keeps stepping,
            # so it picks up mid-motion instead of snapping when you come back.
            if self._on_screen():
                self.update()

    def _on_screen(self) -> bool:
        """True only when this canvas can actually be seen by the user."""
        try:
            if not self.isVisible():
                return False
            win = self.window()
            return not (win.isMinimized() or win.isHidden())
        except Exception:
            return True      # never let a visibility check stop the HUD drawing

    # ── reactor core ─────────────────────────────────────────────────────────
    # The centrepiece for anyone who did not want a face looking back at them.
    # Built from the same budget as the head — software QPainter, no GPU — and
    # from the same principle: everything on it means something. The rings turn
    # at a rate the state sets, the spectrum ring is the real audio level, and
    # the core brightens with the voice. Nothing here is decoration that moves
    # for its own sake, which is what made the old glowing orb feel dead.

    def _core_colours(self):
        if self.muted:
            return qcol(C.MUTED_C), qcol(C.MUTED_C)
        if self.speaking:
            return qcol(C.PRI), qcol(C.ACC)
        if self.state in ("THINKING", "PROCESSING"):
            return qcol(C.PRI), qcol(C.ACC2)
        if self.state == "LISTENING":
            return qcol(C.PRI), qcol(C.GREEN)
        return qcol(C.PRI), qcol(C.PRI_DIM)

    def _paint_core(self, p: QPainter, cx: float, cy: float, r: float,
                    W: float = 0.0, H: float = 0.0):
        """Draw the reactor at (cx, cy) with outer radius r, using the whole
        canvas (W x H) for the marks that frame it."""
        main, acc = self._core_colours()
        bg = qcol(C.BG)
        amp = self._amp_disp
        t = self._core_phase
        live = (self.speaking or amp > 0.04) and not self.muted

        def blend(col: QColor, a: float) -> QColor:
            """Pre-mix onto the background instead of asking Qt to composite.
            The raster engine's opaque path is several times faster than its
            translucent one, and everything here is a line or an arc."""
            k = max(0.0, min(1.0, a))
            return QColor(int(bg.red()   + (col.red()   - bg.red())   * k),
                          int(bg.green() + (col.green() - bg.green()) * k),
                          int(bg.blue()  + (col.blue()  - bg.blue())  * k))

        p.setBrush(Qt.BrushStyle.NoBrush)

        # 1. The atmosphere. One radial gradient doing what a stack of discs did
        #    badly: a wide, soft body of light that gives the thing presence
        #    before any detail is read. This single element decides whether the
        #    HUD looks vast or looks small, so it is drawn first and drawn big.
        # Concentrated rather than spread: a gradient reaching the outer rim
        # washes the whole disc a flat dim blue and reads as fog. Ending it at
        # two thirds leaves it a body of light with somewhere to fall off to,
        # which is what makes it look lit rather than tinted.
        lift = 1.0 + 0.55 * amp + (0.18 if self.speaking else 0.0)
        p.setPen(Qt.PenStyle.NoPen)
        for gr, a0, a1 in ((r * 0.70, 0.30, 0.0), (r * 0.34, 0.34, 0.0)):
            g = QRadialGradient(cx, cy, gr)
            g.setColorAt(0.00, blend(main, min(0.95, a0 * lift)))
            g.setColorAt(0.45, blend(main, min(0.95, a0 * lift * 0.52)))
            g.setColorAt(0.78, blend(main, min(0.95, a0 * lift * 0.18)))
            g.setColorAt(1.00, blend(main, a1))
            p.setBrush(QBrush(g))
            p.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2, gr * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 2. Frame marks at the corners of the whole canvas, not of the circle.
        #    They are what set the scale: the eye reads the reactor as filling
        #    the room rather than sitting in the middle of it.
        if W > 40 and H > 40:
            m, arm = min(W, H) * 0.035, min(W, H) * 0.055
            p.setPen(QPen(blend(main, 0.45), 1.4))
            for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                x = cx + sx * (W / 2 - m)
                y = cy + sy * (H / 2 - m)
                p.drawLine(QLineF(x, y, x - sx * arm, y))
                p.drawLine(QLineF(x, y, x, y - sy * arm))

        # 3. Crosshair across the full canvas, broken around the core so it
        #    frames the reactor rather than crossing it.
        p.setPen(QPen(blend(main, 0.16), 1))
        gap = r * 0.62
        if W > 40:
            p.drawLine(QLineF(cx - W / 2, cy, cx - gap, cy))
            p.drawLine(QLineF(cx + gap, cy, cx + W / 2, cy))
        if H > 40:
            p.drawLine(QLineF(cx, cy - H / 2, cx, cy - gap))
            p.drawLine(QLineF(cx, cy + gap, cx, cy + H / 2))

        # 4. Two thin outer circles. Sparse on purpose — a dense ring reads as a
        #    grey band at this size, and restraint is what made the original
        #    look expensive.
        for rr, a in ((1.00, 0.34), (0.93, 0.16)):
            rad = r * rr
            p.setPen(QPen(blend(main, a), 1))
            p.drawEllipse(QRectF(cx - rad, cy - rad, rad * 2, rad * 2))

        # 5. Long, sparse graduations: 24 majors reaching well in from the rim,
        #    with shorter minors between them.
        major, minor = [], []
        for i in range(72):
            a = math.radians(i * 5.0)
            ca, sa = math.cos(a), math.sin(a)
            if i % 3 == 0:
                major.append(QLineF(cx + ca * r * 0.885, cy + sa * r * 0.885,
                                    cx + ca * r * 0.985, cy + sa * r * 0.985))
            else:
                minor.append(QLineF(cx + ca * r * 0.945, cy + sa * r * 0.945,
                                    cx + ca * r * 0.985, cy + sa * r * 0.985))
        p.setPen(QPen(blend(main, 0.42), 1.3))
        p.drawLines(major)
        p.setPen(QPen(blend(main, 0.18), 1))
        p.drawLines(minor)

        # 6. Sweeping arcs. Long spans, not dashes — the original's grandeur
        #    came from a few big strokes. Speed is the state: idle drifts,
        #    thinking hurries, speaking runs.
        rate = 1.0 + (1.9 if self.state in ("THINKING", "PROCESSING") else 0.0) \
                   + (1.2 if self.speaking else 0.0)
        for k, (rr, span, count, dirn, col, a, wid) in enumerate((
                (0.955, 118, 2, +1, acc,  0.75, 2.0),
                (0.845, 82,  3, -1, main, 0.38, 1.3),
                (0.760, 150, 1, +1, acc,  0.45, 1.6),
                (0.660, 64,  4, -1, main, 0.26, 1.1),
                (0.545, 128, 2, +1, main, 0.30, 1.2))):
            rad = r * rr
            p.setPen(QPen(blend(col, a), wid))
            box = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
            base = (t * rate * (9 + k * 6) * dirn) % 360.0
            for sgm in range(count):
                p.drawArc(box, int((base + sgm * (360.0 / count)) * 16),
                          int(span * 16))

        # 7. The voice, as a ring of graduations that grow with it. Kept out at
        #    a wide radius so it never crowds the middle.
        n = 60
        ring = r * 0.415
        spikes = []
        for i in range(n):
            a = math.radians(i * (360.0 / n))
            ca, sa = math.cos(a), math.sin(a)
            wob = 0.5 + 0.5 * math.sin(t * 2.3 + i * 0.42)
            idle = 0.018 + 0.012 * math.sin(t * 1.2 + i * 0.7)
            h = r * (idle + (amp * 0.20 * wob if live else 0.0))
            spikes.append(QLineF(cx + ca * ring, cy + sa * ring,
                                 cx + ca * (ring + h), cy + sa * (ring + h)))
        p.setPen(QPen(blend(acc if live else main, 0.25 + 0.5 * amp), 1.6))
        p.drawLines(spikes)

        # 8. The inner ring the name sits in.
        inner = r * 0.355
        p.setPen(QPen(blend(acc, 0.30 + 0.45 * amp), 1.5))
        p.drawEllipse(QRectF(cx - inner, cy - inner, inner * 2, inner * 2))

        # 9. The name, sized from the string rather than from the radius alone:
        #    "J.A.R.V.I.S" and a name someone renamed to "MAX" are very
        #    different widths, and a fixed fraction of r spills one of them past
        #    the ring it is supposed to sit inside.
        name = self._assistant_name or ""
        if name:
            space = max(1.0, r * 0.018)
            fsz = max(8, int(min(r * 0.105,
                                 (inner * 1.75) / max(1, len(name)) * 1.6 - space)))
            f = QFont("Courier New", fsz, QFont.Weight.Bold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, space)
            p.setFont(f)
            p.setPen(QPen(blend(qcol(C.WHITE), 0.6 + 0.4 * min(1.0, amp * 2)), 1))
            p.drawText(QRectF(cx - r, cy - fsz, r * 2, fsz * 2),
                       Qt.AlignmentFlag.AlignCenter, name)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():      # device not ready (e.g. 0-size during layout) — skip cleanly
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # grid dots — blitted from a cached layer; rebuilt only when the size
        # or the theme's ghost colour changes (so live re-theming still works).
        _gkey = (W, H, C.PRI_GHO)
        if self._grid_cache is None or self._grid_key != _gkey:
            self._grid_cache = self._make_grid(W, H)
            self._grid_key   = _gkey
        p.drawPixmap(0, 0, self._grid_cache)

        # ── Arc Reactor Core ────────────────────────────────────────────────
        _sy_status = cy + fw * 0.40
        _band_t = 12.0
        _band_h = max(60.0, _sy_status - 12.0 - _band_t)
        _r = min(W * 0.46, _band_h / 2.0)
        self._paint_core(p, cx, _band_t + _band_h / 2.0, _r, W, _band_h)

        # status text
        sy = _sy_status
        if self.muted:
            txt, col = "⊘  MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        # waveform — reacts to the real audio level (mic while listening,
        # JARVIS's own voice while speaking). Falls back to a gentle idle
        # ripple when there's no sound. _amp_disp is the smoothed 0–1 level.
        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        amp = self._amp_disp
        mid = (N - 1) / 2.0
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C)
            else:
                env     = (1.0 - abs(i - mid) / mid) ** 0.7      # center-weighted hump
                shimmer = 0.55 + 0.45 * math.sin(self._tick * 0.18 + i * 0.7)
                idle    = 3.0 + 2.0 * math.sin(self._tick * 0.09 + i * 0.6)
                hgt     = int(max(2, min(24, idle + amp * 22.0 * env * shimmer)))
                if amp > 0.05:
                    cl = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
                else:
                    cl = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

        p.end()   # end deterministically so the backing store never flushes an active painter

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(44)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        v = max(0.0, min(100.0, pct))
        if v == self._value and text == self._text:
            return          # unchanged — skip the repaint
        self._value = v
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 7
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(_f(_FS_CAPTION))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 6, 50, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(_f(_FS_DATA, True))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 5, W - 8, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

        p.end()

_CLIPBOARD_QUIET_UNTIL = 0.0     # copies made by the HUD itself must not pop the clipboard panel


def _copy_to_clipboard(text: str) -> None:
    global _CLIPBOARD_QUIET_UNTIL
    _CLIPBOARD_QUIET_UNTIL = time.time() + 1.0
    QApplication.clipboard().setText(text)


class _CopyIconButton(QPushButton):
    """The small copy glyph under a chat bubble: two overlapping rounded squares.

    It is DRAWN, not typed as an emoji. An emoji ignores the style sheet's text
    colour, so the old "transparent" clipboard emoji was never really hidden and
    sat under every message. A drawn glyph follows the theme, and it is truly
    invisible (alpha 0) until the cursor is over its message row — _HoverReveal
    calls reveal(). After a click it turns into a green tick for a moment."""

    _W, _H = 24, 20

    def __init__(self, get_text, parent=None):
        super().__init__(parent)
        self.setObjectName("copy_btn")
        self.setFixedSize(self._W, self._H)
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Copy")
        self.setStyleSheet("QPushButton#copy_btn { background: transparent; border: none; }")
        self._get_text = get_text
        self._alpha = 0.0           # 0 = invisible, 1 = fully shown
        self._want = False          # is the cursor over the row right now?
        self._copied = False
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(140)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._on_fade)
        self.clicked.connect(self._do_copy)

    # ── show / hide ──────────────────────────────────────────────────────
    def _on_fade(self, v) -> None:
        self._alpha = float(v)
        self.update()

    def _fade_to(self, target: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._alpha)
        self._fade.setEndValue(float(target))
        self._fade.start()

    def reveal(self, on: bool) -> None:
        self._want = bool(on)
        if not self._copied:                # a tick in progress stays put
            self._fade_to(1.0 if on else 0.0)

    # ── copying ──────────────────────────────────────────────────────────
    def _do_copy(self) -> None:
        try:
            _copy_to_clipboard(self._get_text())
        except RuntimeError:                # the bubble was trimmed away
            return
        self._copied = True
        self.setToolTip("Copied")
        self._fade_to(1.0)
        QTimer.singleShot(1200, self._end_copied)

    def _end_copied(self) -> None:
        try:
            self._copied = False
            self.setToolTip("Copy")
            self._fade_to(1.0 if (self._want or self.underMouse()) else 0.0)
        except RuntimeError:                # button deleted meanwhile
            pass

    # ── drawing ──────────────────────────────────────────────────────────
    def enterEvent(self, e) -> None:
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e) -> None:
        super().leaveEvent(e)
        self.update()

    def paintEvent(self, _e) -> None:
        if self._alpha < 0.02:
            return
        w, h = float(self.width()), float(self.height())
        hover = self.underMouse() and not self._copied
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._alpha)
        if hover:                           # soft highlight behind the glyph
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcol(C.PRI, 38))
            p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 5, 5)
        colour = qcol(C.GREEN if self._copied else (C.PRI if hover else C.TEXT_MED))
        pen = QPen(colour, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._copied:
            cx, cy = w / 2, h / 2
            tick = QPainterPath()
            tick.moveTo(cx - 4.5, cy + 0.2)
            tick.lineTo(cx - 1.5, cy + 3.2)
            tick.lineTo(cx + 4.5, cy - 3.2)
            pen.setWidthF(1.8)
            p.setPen(pen)
            p.drawPath(tick)
        else:
            s, d, r, gap = 8.0, 4.0, 2.0, 1.6      # square, offset, corner, gap
            x0, y0 = (w - (s + d)) / 2, (h - (s + d)) / 2
            back = QPainterPath()                  # only the part the front square leaves visible
            back.moveTo(x0 + s, y0 + d - gap)
            back.lineTo(x0 + s, y0 + r)
            back.quadTo(x0 + s, y0, x0 + s - r, y0)
            back.lineTo(x0 + r, y0)
            back.quadTo(x0, y0, x0, y0 + r)
            back.lineTo(x0, y0 + s - r)
            back.quadTo(x0, y0 + s, x0 + r, y0 + s)
            back.lineTo(x0 + d - gap, y0 + s)
            p.setPen(pen)
            p.drawPath(back)
            p.drawRoundedRect(QRectF(x0 + d, y0 + d, s, s), r, r)
        p.end()


class _HoverReveal(QObject):
    """Shows `btn` while the cursor is anywhere over `host` (the whole message row,
    including the empty space beside the bubble) and hides it when it leaves."""

    def __init__(self, host: QWidget, btn: "_CopyIconButton"):
        super().__init__(host)
        self._btn = btn
        host.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:
        t = ev.type()
        try:
            if t == QEvent.Type.Enter:
                self._btn.reveal(True)
            elif t == QEvent.Type.Leave:
                self._btn.reveal(False)
        except RuntimeError:                # button already deleted
            pass
        return False


class _LogHost(QWidget):
    """Log content holder. Tells LogWidget when its layout changes."""

    def __init__(self, on_layout, parent=None):
        super().__init__(parent)
        self._on_layout = on_layout

    def event(self, ev):
        r = super().event(ev)
        if ev.type() == QEvent.Type.LayoutRequest:
            self._on_layout()
        return r

class LogWidget(QScrollArea):
    """Activity log rendered as chat bubbles.

    You:    -> compact card on the right
    JARVIS: -> glowing glass card with a cyan left edge and a sender badge
    SYS/FILE/ERR/... -> minimal status pills

    The public surface is unchanged: append_log() is thread-safe (it goes
    through a signal) and `_ai_name_lc` tracks the assistant's name.
    """
    _sig = pyqtSignal(str)

    _MAX_BUBBLES = 300
    _TAG_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _.\-]{0,23}):\s*(.*)$", re.S)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("logScroll")
        self.setWidgetResizable(False)
        self._syncing = False
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFont(QFont(MONO_FONT, 10))
        self.setStyleSheet(f"""
            QScrollArea#logScroll {{
                background: {_rgba(C.GLASS, 0.55)};
                border: 1px solid {_glass_border()};
                border-radius: 8px;
            }}
            QScrollArea#logScroll > QWidget > QWidget#logHost {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: transparent; width: 8px; margin: 4px 2px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_rgba(C.PRI, 0.28)}; border-radius: 3px; min-height: 28px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {_rgba(C.PRI, 0.5)}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px; border: none; background: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)
        self.viewport().setAutoFillBackground(False)

        self._host = _LogHost(self._sync_host)
        self._host.setObjectName("logHost")
        self._lay = QVBoxLayout(self._host)
        self._lay.setContentsMargins(8, 8, 8, 8)
        self._lay.setSpacing(8)
        self._lay.setSizeConstraint(self._lay.SizeConstraint.SetNoConstraint)
        self._lay.addStretch(1)          # last item = stretch, chats sit on top
        self.setWidget(self._host)

        self._queue: list[str] = []
        self._typing = False
        self._ai_name_lc = "jarvis"       # updated when assistant name changes
        self._history: list[str] = []     # plain-text transcript for Copy chat

        # typewriter state for the bubble currently being "spoken"
        self._type_lbl: QLabel | None = None
        self._type_text = ""
        self._type_pos = 0
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)

        # auto-scroll pin: follow new content unless the user scrolled up
        self._user_scrolled_up = False
        sb = self.verticalScrollBar()
        sb.rangeChanged.connect(self._on_range)
        sb.valueChanged.connect(self._on_value)

        self._sig.connect(self._enqueue)

    # ── public API ────────────────────────────────────────────────────────
    def append_log(self, text: str):
        self._sig.emit(text)

    def viewportEvent(self, e):
        r = super().viewportEvent(e)
        if e.type() == QEvent.Type.Resize:
            self._sync_host()
        return r

    def _sync_host(self):
        """Host height = max(viewport, content). No extra blank area, and
        nothing to drag when all chats already fit."""
        if self._syncing:
            return
        self._syncing = True
        try:
            for _ in range(3):          # scrollbar may appear and change width
                vp = self.viewport()
                vw, vh = vp.width(), vp.height()
                lay = self._lay
                h = lay.heightForWidth(vw) if lay.hasHeightForWidth() \
                    else lay.sizeHint().height()
                self._host.resize(vw, max(h, vh))
                if vp.width() == vw:
                    break
        finally:
            self._syncing = False

    # ── scrolling ─────────────────────────────────────────────────────────
    def _on_value(self, v: int):
        sb = self.verticalScrollBar()
        self._user_scrolled_up = (v < sb.maximum() - 30)

    def _on_range(self, _lo: int, hi: int):
        if not self._user_scrolled_up:
            self.verticalScrollBar().setValue(hi)

    def _pin_bottom(self):
        """Jump to the bottom now, and again once the layout has settled."""
        if self._user_scrolled_up:
            return
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())
        QTimer.singleShot(0, self._pin_bottom_now)

    def _pin_bottom_now(self):
        if not self._user_scrolled_up:
            sb = self.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ── message classification ────────────────────────────────────────────
    def _classify(self, text: str) -> tuple[str, str, str]:
        """-> (kind, label, body)  kind in you|ai|file|err|sys"""
        m = self._TAG_RE.match(text)
        label, body = (m.group(1).strip(), m.group(2)) if m else ("", text)
        tl = text.lower()
        pfx = label.lower() + ":" if label else ""
        if pfx == "you:":
            return "you", "YOU", body
        if pfx in (f"{self._ai_name_lc}:", "jarvis:"):
            return "ai", label.upper(), body
        if pfx == "file:":
            return "file", "FILE", body
        if "err" in tl:
            return "err", (label.upper() if label and len(label) <= 6 else "ERR"), body
        # SYS / QUIZ / anything else: keep a short tag as the pill badge
        return "sys", (label.upper() if label and len(label) <= 8 else "SYS"), body

    # ── bubble building blocks ────────────────────────────────────────────
    def _badge(self, text: str, fg: str, bg: str, size: int = _FS_BODY) -> QLabel:
        """Small rounded sender tag: YOU / JARVIS / SYS / ERR ..."""
        b = QLabel(str(text))
        b.setFont(_f(size, True))
        b.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        b.setStyleSheet(
            f"color: {fg}; background: {bg}; border: none; "
            f"border-radius: 4px; padding: 1px 7px;")
        return b

    def _text_label(self, text: str, color: str, size: int = 11) -> QLabel:
        """Wrapping, selectable message text.

        Plain text on purpose: a reply containing < or & must appear as written,
        not be read as HTML. The minimum width is 1 so that one very long word
        (a path, a URL) can never stretch the whole panel sideways."""
        lbl = QLabel(text)
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setWordWrap(True)
        lbl.setFont(_f(size))
        lbl.setMinimumWidth(1)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setCursor(Qt.CursorShape.IBeamCursor)
        lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        return lbl

    # ── copying ───────────────────────────────────────────────────────────
    def _copy_button(self, get_text) -> "_CopyIconButton":
        """Small copy icon; invisible until the cursor is over its message row."""
        return _CopyIconButton(get_text)

    def _make_card(self, kind: str, label: str, body: str) -> tuple[QWidget, QLabel]:
        row = QFrame()
        row.setObjectName("chatRow")
        
        # The copy icon is revealed by _HoverReveal (cursor over the row), not by QSS.
        row.setStyleSheet("QFrame#chatRow { background: transparent; }")
        
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 4) # নিচে বাটনের জন্য একটু স্পেস
        rl.setSpacing(0)

        # মূল মেসেজ বাবল
        card_container = QWidget()
        c_lay = QVBoxLayout(card_container)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(2)

        card = QFrame()
        card.setObjectName("bubble")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(11, 8, 11, 9)
        cl.setSpacing(4)

        if kind == "you":
            card.setStyleSheet(f"""
                QFrame#bubble {{
                    background: rgba(255, 255, 255, 0.045);
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    border-right: 3px solid {_rgba(C.PRI, 0.75)};
                    border-radius: 8px;
                }}""")
            head = self._badge(label, C.GREEN, "rgba(0, 255, 136, 0.12)", _FS_BODY)
            
            hl = QHBoxLayout(); hl.setContentsMargins(0, 0, 0, 0)
            body_lbl = self._text_label(body, C.TEXT)
            hl.addStretch(1); hl.addWidget(head)
            cl.addLayout(hl)
            cl.addWidget(body_lbl)
            
            # বাবল-এর নিচে ছোট করে কপি বাটন
            cp = self._copy_button(body_lbl.text)
            row._copy_btn = cp
            _HoverReveal(row, cp)
            c_lay.addWidget(card)
            
            under_lay = QHBoxLayout(); under_lay.setContentsMargins(0, 0, 4, 0)
            under_lay.addStretch(1); under_lay.addWidget(cp)
            c_lay.addLayout(under_lay)
            
            rl.addStretch(1)
            rl.addWidget(card_container, 6)
            
        elif kind == "ai":
            card.setStyleSheet(f"""
                QFrame#bubble {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                        stop:0 {_rgba(C.PRI, 0.13)}, stop:1 {_rgba(C.PRI, 0.05)});
                    border: 1px solid {_rgba(C.PRI, 0.22)};
                    border-left: 3px solid {_rgba(C.PRI, 0.7)};
                    border-radius: 8px;
                }}""")
            head = self._badge(label, C.PRI, _rgba(C.PRI, 0.16), _FS_BODY)
            
            hl = QHBoxLayout(); hl.setContentsMargins(0, 0, 0, 0)
            body_lbl = self._text_label(body, C.TEXT)
            hl.addWidget(head); hl.addStretch(1)
            cl.addLayout(hl)
            cl.addWidget(body_lbl)
            
            # বাবল-এর নিচে ছোট কপি বাটন
            cp = self._copy_button(body_lbl.text)
            if not body:
                cp.hide()           # টাইপ হওয়া শেষ না হওয়া পর্যন্ত লুকানো থাকবে
            row._copy_btn = cp
            _HoverReveal(row, cp)
            
            c_lay.addWidget(card)
            
            under_lay = QHBoxLayout(); under_lay.setContentsMargins(4, 0, 0, 0)
            under_lay.addWidget(cp); under_lay.addStretch(1)
            c_lay.addLayout(under_lay)
            
            rl.addWidget(card_container, 6)
            rl.addStretch(1)

        else:
            # status pill: tag + message, one compact rounded line
            fg, bg, edge = {
                "file": ("#5ee6a8", "rgba(94, 230, 168, 0.09)", "rgba(94, 230, 168, 0.28)"),
                "err":  ("#ff8a9b", "rgba(255, 85, 110, 0.10)", "rgba(255, 85, 110, 0.32)"),
            }.get(kind, (C.TEXT_MED, "rgba(255, 255, 255, 0.04)", "rgba(255, 255, 255, 0.09)"))
            card.setStyleSheet(f"""
                QFrame#bubble {{
                    background: {bg}; border: 1px solid {edge}; border-radius: 10px;
                }}""")
            cl.setContentsMargins(8, 3, 10, 3)
            cl.setSpacing(0)
            pl = QHBoxLayout(); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(7)
            tag = self._badge(label, fg, "rgba(255, 255, 255, 0.06)", 9)
            tag.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
            body_lbl = self._text_label(body, fg if kind != "sys" else C.TEXT_MED, 11)
            # Pills size to their text; a long message is capped at the row
            # width and wraps instead of shrinking to the badge (see the
            # Maximum policy on the card below).
            body_lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            pl.addWidget(tag, 0, Qt.AlignmentFlag.AlignTop)
            pl.addWidget(body_lbl, 1)
            cl.addLayout(pl)
            # The pill gets the same 6:1 share of the row as a chat bubble.
            # Without a stretch factor the row gave it only its sizeHint —
            # and a wrapping QLabel reports a small, already-wrapped hint, so
            # a long system message ended up a narrow column with the text
            # cut off. A share of the row makes it wrap at the panel width.
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            rl.addWidget(card, 6)
            rl.addStretch(1)
        return row, body_lbl

    def _add_row(self, row: QWidget):
        # insert before the trailing stretch
        self._lay.insertWidget(self._lay.count() - 1, row)
        # drop the oldest bubbles (they sit at index 0)
        while self._lay.count() - 1 > self._MAX_BUBBLES:
            item = self._lay.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()
        self._pin_bottom()

    # ── queue / typewriter ────────────────────────────────────────────────
    def _enqueue(self, text: str):
        self._queue.append(text)
        self._pin_bottom()
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        text = self._queue.pop(0)
        kind, label, body = self._classify(text)
        body = body.rstrip()
        self._history.append(f"{label.title() if kind in ('you', 'ai') else label}: {body}")
        if len(self._history) > self._MAX_BUBBLES:
            del self._history[:len(self._history) - self._MAX_BUBBLES]
        if kind == "ai" and body:
            row, lbl = self._make_card(kind, label, "")
            self._add_row(row)
            self._type_row = row
            self._type_lbl, self._type_text, self._type_pos = lbl, body, 0
            self._tmr.start(14)
        else:
            row, _lbl = self._make_card(kind, label, body)
            self._add_row(row)
            QTimer.singleShot(10, self._next)

    def _step(self):
        lbl = self._type_lbl
        if lbl is None or self._type_pos >= len(self._type_text):
            self._tmr.stop()
            self._type_lbl = None
            try:
                cp = getattr(getattr(self, "_type_row", None), "_copy_btn", None)
                if cp is not None:
                    cp.show()
            except RuntimeError:
                pass
            QTimer.singleShot(40, self._next)
            return
        # a few characters per tick keeps long replies from crawling
        self._type_pos = min(len(self._type_text),
                             self._type_pos + (1 if len(self._type_text) < 120 else 3))
        try:
            lbl.setText(self._type_text[:self._type_pos])
        except RuntimeError:               # bubble trimmed away mid-typing
            self._type_lbl = None
            self._type_pos = len(self._type_text)
            return
        if not self._user_scrolled_up:
            sb = self.verticalScrollBar()
            sb.setValue(sb.maximum())

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


# Phase 5 — documents the Study Station can extract locally (no vision tokens).
_STUDY_DOC_EXTS = {".pdf", ".docx", ".pptx", ".txt", ".md", ".rst", ".json", ".png", ".jpg", ".jpeg", ".webp"}

# ── Batch drop ingest ───────────────────────────────────────────────────────
# A dropped folder used to be ignored (only urls()[0] was read, and only if it
# was a file). These helpers expand a mixed drop of files and folders into a
# flat list of readable files, entirely off the Qt thread: walking a deep tree
# is filesystem work, not UI work.

# What is worth pulling out of a dropped folder. Everything else (.exe, .dll,
# .pyc, media, binaries) is skipped so a project directory does not drag
# thousands of unusable paths into the prompt.
_INGEST_EXTS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".txt", ".md", ".rst",
    ".csv", ".json", ".xml", ".log", ".yaml", ".yml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".java", ".cpp",
    ".c", ".h", ".go", ".rs", ".sql", ".sh", ".ps1",
}
_MAX_DROP_FILES  = 64     # sanity cap — a whole drive is not one document
_MAX_FOLDER_DEPTH = 12    # refuse to descend past this many levels
_SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv",
              "dist", "build", ".idea", ".vscode"}

# One shared, bounded pool for folder expansion. Small on purpose: two workers
# is plenty for a drop, and it keeps the GIL pressure off the audio threads.
_DROP_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dropzone")


def _is_hidden_path(p: Path) -> bool:
    """True for dot-files and Office/editor lock files."""
    n = p.name
    return n.startswith(".") or n.startswith("~$")


def collect_dropped_files(paths, max_files: int = _MAX_DROP_FILES) -> list[str]:
    """Expand a drop of files AND folders into a flat list of readable files.

    Pure filesystem work — no Qt, no network — so it is safe (and intended) to
    run on a worker thread. Directories are walked recursively with os.walk;
    hidden/system files, lock files, unsupported binaries and common build
    directories are skipped. The result is de-duplicated and sorted. Never
    raises: a bad path is skipped, not fatal.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key in seen:
            return
        try:
            if not p.is_file():
                return
        except OSError:
            return
        seen.add(key)
        out.append(str(p))

    for raw in paths or []:
        if len(out) >= max_files:
            break
        try:
            root_path = Path(str(raw))
            if root_path.is_dir():
                base_depth = len(root_path.parts)
                for root, dirs, files in os.walk(root_path):
                    if len(Path(root).parts) - base_depth > _MAX_FOLDER_DEPTH:
                        dirs[:] = []          # too deep — do not descend
                        continue
                    dirs[:] = [d for d in dirs
                               if d not in _SKIP_DIRS and not d.startswith(".")]
                    for fn in files:
                        if len(out) >= max_files:
                            break
                        fp = Path(root) / fn
                        if _is_hidden_path(fp):
                            continue
                        if fp.suffix.lower() not in _INGEST_EXTS:
                            continue
                        _add(fp)
            elif root_path.is_file():
                _add(root_path)
        except Exception:
            continue
    out.sort()
    return out[:max_files]


class FileDropZone(QWidget):
    file_selected  = pyqtSignal(str)     # legacy handoff — head of the batch only
    files_selected = pyqtSignal(list)    # batch handoff — List[str] of paths
    _scan_done     = pyqtSignal(list)    # worker thread → Qt thread (queued)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(50)
        self._current_file:  str | None = None
        self._current_files: list[str]  = []
        self._hovering  = False
        self._drag_over = False
        self._scanning  = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        self._scan_done.connect(self._on_scan_done)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        # The marching-ants dashed border is only meaningful while the user is
        # hovering, dragging, or waiting on a folder scan. When idle, skip the
        # repaint entirely instead of redrawing the whole zone 25×/s forever —
        # that idle repaint held the GIL and stole time from the audio threads.
        if not (self._hovering or self._drag_over or self._scanning):
            return
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        """Accept EVERY dropped URL — files and folders alike.

        Only the cheap URL→path conversion runs here on the Qt thread. Expanding
        a folder walks the filesystem, so that job is handed to _DROP_POOL; the
        worker returns through the _scan_done signal, which Qt delivers back on
        the GUI thread. The event loop is never blocked by a big drop.
        """
        self._drag_over = False
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
        if paths:
            self._scanning = True
            self._drop_off(paths)
        self._canvas.update()

    def _drop_off(self, paths: list[str]) -> None:
        """Queue the raw dropped paths for expansion on the shared pool."""
        def _work():
            try:
                found = collect_dropped_files(paths)
            except Exception:
                found = []
            try:
                self._scan_done.emit(found)   # cross-thread — Qt queues it
            except RuntimeError:
                pass                          # widget destroyed mid-scan
        _DROP_POOL.submit(_work)

    def _on_scan_done(self, files: list) -> None:
        self._scanning = False
        self._canvas.update()
        if files:
            self._set_files(list(files))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def current_files(self) -> list[str]:
        return list(self._current_files)

    def clear_file(self):
        self.setFixedHeight(50)
        self._current_file = None
        self._current_files = []
        self._canvas.update()

    def _browse(self):
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("📂 Load Study Context (Files or Folder)")
        box.setText(
            "আপনি কি এক বা একাধিক ফাইল (.md, .json, .pdf, ছবি) সিলেক্ট করতে চান, "
            "নাকি পুরো একটি ফোল্ডার একসাথে আপলোড করতে চান?"
        )
        btn_multi = box.addButton("📄 Select Multiple Files", QMessageBox.ButtonRole.AcceptRole)
        btn_folder = box.addButton("📁 Select Entire Folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()

        if clicked is btn_folder:
            folder = QFileDialog.getExistingDirectory(
                self, "📁 Select Folder to Ingest into JARVIS Study Vault", str(Path.home())
            )
            if folder:
                self._scanning = True
                self._canvas.update()
                self._drop_off([folder])
            return

        if clicked is not btn_multi:
            return

        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Multiple Study Files for JARVIS", str(Path.home()),
            "Study Context (*.md *.json *.pdf *.docx *.pptx *.txt *.png *.jpg *.jpeg *.webp);;"
            "All Files (*.*);;"
            "Markdown & JSON (*.md *.json *.txt);;"
            "Images (*.jpg *.jpeg *.png *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx)",
        )
        if paths:
            self._set_files([p for p in paths if Path(p).is_file()])

    def _set_files(self, paths: list[str], emit: bool = True):
        paths = [p for p in paths if p]
        if not paths:
            return
        self.setFixedHeight(84)
        self._current_files = list(paths)
        self._current_file  = paths[0]
        self._canvas.update()
        if not emit:
            return
        # Legacy consumers keep working on the head of the list …
        self.file_selected.emit(self._current_file)
        # … while the batch handoff carries them all to the Live session.
        self.files_selected.emit(list(paths))

    def set_file_quiet(self, path: str):
        """Load a path WITHOUT emitting the handoff signals.

        Used by the phone-upload path, which has already told the Live session
        about the file and must not trigger a second, duplicate notification.
        """
        self._set_files([path], emit=False)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        if z._drag_over:    bg_col = qcol(C.PRI, 34)
        elif z._hovering:   bg_col = qcol(C.PRI, 18)
        else:               bg_col = qcol(C.GLASS, 150)
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 170)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.PRI, 120)
        else:                 border_col = qcol(C.PRI, 51)

        # Smooth solid edge at rest; marching dashes only while interacting.
        active = z._drag_over or z._hovering
        pen = QPen(border_col, 1.5 if active else 1.0,
                   Qt.PenStyle.DashLine if active else Qt.PenStyle.SolidLine)
        if active:
            pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

        p.end()

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Compact single-line prompt, measured and elided to the slot. The
        # panel is _RIGHT_W wide minus margins, and the full string is wider
        # than the canvas on Windows — drawn raw it was clipped mid-word
        # ("Click to Brow"). elidedText() shortens it to fit the width we
        # actually have instead of losing the end of the sentence.
        slot = max(40, W - 16)
        p.setFont(QFont("Courier New", 8))
        line = p.fontMetrics().elidedText(
            "📂  Drop files or folder here  ·  Click to Browse",
            Qt.TextElideMode.ElideRight, slot)
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, 0, W, H), Qt.AlignmentFlag.AlignCenter, line)

        p.setFont(QFont("Courier New", 7))
        sub = p.fontMetrics().elidedText(
            "Images · Video · Audio · PDF · Docs · Code · Data",
            Qt.TextElideMode.ElideRight, slot)
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, cy + 28, W, 18), Qt.AlignmentFlag.AlignCenter, sub)

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        z      = self._z
        n_files = len(getattr(z, "_current_files", None) or [z._current_file])
        path   = Path(z._current_file)
        cat    = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        try:
            size_str = _fmt_size(path.stat().st_size)
        except OSError:
            size_str = "—"
        if n_files > 1:
            size_str = f"{n_files} files"
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        # Elided to the measured slot instead of a fixed 34-character cut:
        # the panel width is what limits the name here, not a character count.
        name = path.name
        if n_files > 1:
            name = f"{name}  (+{n_files - 1} more)"
        name = p.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, tw)
        p.drawText(QRectF(tx, H * 0.12, tw, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.12 + 20, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        # Elide on the left: the folder at the end of a path is the part that
        # identifies the file.
        par = p.fontMetrics().elidedText(str(path.parent),
                                         Qt.TextElideMode.ElideLeft, tw)
        p.drawText(QRectF(tx, H * 0.12 + 38, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {_txt(color)}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY(S)  —  add backups after commas", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…  (backup keys: AIza…, AIza…)")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class HueWheel(QWidget):
    """
    Circular colour picker. The user drags the handle (small white circle)
    around the wheel to choose from ALL hues. The filled circle in the centre
    is a live preview of the selected colour.
    """

    hue_picked    = pyqtSignal(str)   # while dragging (live)
    hue_committed = pyqtSignal(str)   # when the handle is released

    _RING = 16   # ring thickness (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometry helpers ─────────────────────────────────────────────────────
    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # screen y goes down — flip to math axis
        ang = math.atan2(dy, dx)      # [-π, π], counter-clockwise
        return (ang / (2 * math.pi)) % 1.0

    # ── drawing ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # centre preview circle
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # draggable handle
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)
        p.end()

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — change assistant name, user name, UI colour and voice."""

    saved = pyqtSignal(str, str, str, str)   # assistant_name, user_name, ui_color, voice
    _OW, _OH = 400, 588

    def __init__(self, assistant_name="JARVIS", user_name="",
                 ui_color=DEFAULT_UI_COLOR, voice="", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {_txt(color)}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(_lbl("⚙  CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Courier New", 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(QFont("Courier New", 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        # ── Assistant voice — Gemini prebuilt voices ─────────────────────────
        # Names are language-neutral proper nouns, so the row reads the same in
        # every locale. Selecting one and applying rebuilds the Live session.
        from memory.config_manager import AVAILABLE_VOICES, DEFAULT_VOICE
        lay.addSpacing(4)
        lay.addWidget(_lbl("ASSISTANT VOICE", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._sel_voice   = (voice or DEFAULT_VOICE)
        if self._sel_voice not in AVAILABLE_VOICES:
            self._sel_voice = DEFAULT_VOICE
        self._voice_btns: dict[str, QPushButton] = {}
        voice_row = QHBoxLayout(); voice_row.setSpacing(4)
        for _v in AVAILABLE_VOICES:
            b = QPushButton(_v)
            b.setCheckable(True)
            b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=_v: self._on_voice_pick(name))
            self._voice_btns[_v] = b
            voice_row.addWidget(b)
        lay.addLayout(voice_row)
        self._refresh_voice_btns()

        # ── UI colour — colour wheel ─────────────────────────────────────────
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None   # callable(hex) — live preview; MainWindow wires it

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (custom hex colour)")
        self._hex_input.setFont(QFont("Courier New", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Courier New", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    # ── voice selection ──────────────────────────────────────────────────────
    def _on_voice_pick(self, name: str):
        self._sel_voice = name
        self._refresh_voice_btns()

    def _refresh_voice_btns(self):
        """Highlight the selected voice pill; dim the rest."""
        for name, b in self._voice_btns.items():
            on = (name == self._sel_voice)
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {C.PRI_GHO}; color: {C.TEXT};
                        border: 1px solid {C.PRI}; border-radius: 3px; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_MED};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
                """)

    # ── colour flow ──────────────────────────────────────────────────────────
    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        """Updates the selected colour; hex box + wheel stay in sync, theme is live-previewed."""
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        # While dragging: update the hex box, don't apply the theme yet
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        # Handle released → live-preview the whole interface
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        # If a preview was applied, revert to the colour from launch
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _save(self):
        name = self._name_input.text().strip() or "JARVIS"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR, self._sel_voice)
        self.hide()


class PluginManagerOverlay(QWidget):
    """Floating overlay — lists discovered plugins with per-plugin ON/OFF toggles."""

    _OW = 420

    def __init__(self, plugins: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginManagerOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🧩  PLUGIN MANAGER")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(hdr)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        if not plugins:
            empty = QLabel("No plugins found in /plugins.")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(empty)

        for p in plugins:
            lay.addLayout(self._build_row(p))

        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.adjustSize()

    def _build_row(self, p: dict) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)

        label_text = p["name"] if p["valid"] else f"{p['name']}  (⚠ {p['file']})"
        lbl = QLabel(label_text)
        lbl.setFont(QFont("Courier New", 8))
        lbl.setStyleSheet(f"color: {C.TEXT if p['valid'] else C.TEXT_DIM}; background: transparent;")
        lbl.setToolTip(p["description"] if p["valid"] else p["error"])
        lbl.setWordWrap(False)
        row.addWidget(lbl, stretch=1)

        btn = QPushButton()
        btn.setFixedSize(72, 24)
        btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        if not p["valid"]:
            btn.setText("BROKEN")
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
            """)
        else:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._style_toggle(btn, p["enabled"])
            btn.clicked.connect(lambda _, name=p["name"], b=btn: self._toggle(name, b))
        row.addWidget(btn)
        return row

    def _style_toggle(self, btn: QPushButton, enabled: bool):
        if enabled:
            btn.setText("ON")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            btn.setText("OFF")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)

    def _toggle(self, name: str, btn: QPushButton):
        from memory.config_manager import get_plugin_enabled, save_plugin_enabled
        new_val = not get_plugin_enabled(name)
        save_plugin_enabled(name, new_val)
        self._style_toggle(btn, new_val)


class _HudOverlay(QWidget):
    """Base for the floating panels placed by hand over the HUD.

    They are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide or shrink: the HUD keeps
    painting around them and their last frame stays on screen as a ghost. Any
    overlay positioned with _centre_overlay needs this."""

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            # Repaint exactly what we were covering, before we stop covering it.
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)


class ConfirmBanner(_HudOverlay):
    """The gate in front of an action that cannot be taken back.

    The old confirmation was a tool parameter the model filled in itself, which
    means it confirmed its own shutdown requests. This is the interface asking,
    and the answer travels from a human finger to core/confirm.py without the
    model in the loop. Nothing blocks while it is up: the assistant keeps
    talking, so this costs no latency — unlike the old gate, which spent two
    tool round trips on every power command."""

    answered = pyqtSignal(bool)
    _OW = 430

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmBanner {{
                background: rgba(14, 3, 0, 250);
                border: 1px solid {C.ACC};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        hdr = QLabel("⚠  CONFIRM")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(hdr)

        ttl = QLabel(title)
        ttl.setWordWrap(True)
        ttl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(ttl)

        if detail:
            dtl = QLabel(detail)
            dtl.setWordWrap(True)
            dtl.setFont(QFont("Courier New", 8))
            dtl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            lay.addWidget(dtl)

        row = QHBoxLayout(); row.setSpacing(8)

        yes = QPushButton("▸  CONFIRM")
        yes.setFixedHeight(32)
        yes.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC};
                border: 1px solid {C.ACC}; border-radius: 3px; }}
            QPushButton:hover {{ background: rgba(255,107,0,40); }}
        """)
        yes.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(yes)

        no = QPushButton("CANCEL")
        no.setFixedHeight(32)
        no.setFont(QFont("Courier New", 9))
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        no.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(no)
        lay.addLayout(row)

        # Default focus on CANCEL: if someone hits Enter without reading, the
        # safe answer wins.
        no.setDefault(True)
        no.setFocus()


class AudioDeviceOverlay(_HudOverlay):
    """Choose which microphone JARVIS listens to and which speakers it uses.

    Both audio streams used to open with no `device=` at all, so they always
    took the OS default — which on Windows moves by itself the moment a headset
    is plugged in. 'JARVIS can't hear me' is usually 'JARVIS is listening to the
    webcam'."""

    picked = pyqtSignal()      # emitted after Apply, when something changed
    _OW = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.audio_devices import list_devices, DEFAULT_LABEL
        from memory.config_manager import get_input_device, get_output_device

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AudioDeviceOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🎧  AUDIO DEVICES")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        _combo_css = (
            f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; border: 1px solid {C.BORDER}; }}"
        )

        def _row(label: str, kind: str, current: str) -> QComboBox:
            cap = QLabel(label)
            cap.setFont(QFont("Courier New", 8))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(cap)

            box = QComboBox()
            box.setFont(QFont("Courier New", 9))
            box.setFixedHeight(30)
            box.setStyleSheet(_combo_css)
            # The list is served from a cache warmed on a background thread at
            # startup, so opening this panel never blocks the Qt thread on the
            # host audio API.
            box.addItem(DEFAULT_LABEL, "")
            for name in list_devices(kind):
                box.addItem(name, name)
            idx = box.findData(current) if current else 0
            box.setCurrentIndex(idx if idx >= 0 else 0)
            if current and idx < 0:
                # Saved device is not plugged in right now. Show it rather than
                # silently resetting the user's choice to default.
                box.addItem(f"{current}  (not connected)", current)
                box.setCurrentIndex(box.count() - 1)
            lay.addWidget(box)
            return box

        self._in_box  = _row("MICROPHONE — what JARVIS hears you with",
                             "input", get_input_device())
        lay.addSpacing(4)
        self._out_box = _row("SPEAKERS — what JARVIS talks through",
                             "output", get_output_device())

        note = QLabel("Applying reconnects the session. Your conversation is kept.")
        note.setWordWrap(True)
        note.setFont(QFont("Courier New", 7))
        note.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addSpacing(6)
        lay.addWidget(note)

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  APPLY")
        ok.setFixedHeight(32)
        ok.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _apply(self):
        from memory.config_manager import (
            get_input_device, get_output_device,
            save_input_device, save_output_device,
        )
        new_in  = self._in_box.currentData()  or ""
        new_out = self._out_box.currentData() or ""
        changed = (new_in != get_input_device()) or (new_out != get_output_device())
        save_input_device(new_in)
        save_output_device(new_out)
        self.hide()
        # Only rebuild the session if something actually moved — a no-op Apply
        # should not cost a reconnect.
        if changed:
            self.picked.emit()


class MemoryOverlay(_HudOverlay):
    """Everything JARVIS has stored about you, and when it learned it.

    Memory used to be a 2200-character store that deleted its oldest entries
    when full and mentioned it only on stdout. The cap is gone; this panel is
    the other half of that change — a memory you cannot inspect is a memory you
    cannot trust, and 'delete' has to be something the person can do."""

    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MemoryOverlay {{
                background: rgba(0, 6, 10, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self._rebuild()

    def _clear_layout(self):
        """Take every item out of the layout and detach it from the widget tree
        in this call.

        deleteLater() on its own is not enough: it queues destruction for the
        next event-loop pass, and until then the old rows are still children of
        this widget and still paint — which is what drew half of the previous
        panel over the new one. setParent(None) removes them from the tree now;
        deleteLater() then frees them safely."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # hide() stops it painting in this frame; deleteLater() frees it
                # safely afterwards. setParent(None) would also stop the paint,
                # but it turns the widget into a top-level window for the moment
                # between the two calls, which is not something to leave lying
                # around inside a click handler.
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def _settle(self, before):
        """Size the panel to its content, re-centre it, and repaint what the old
        size covered.

        The re-size has to happen here rather than at the end of _rebuild
        because Qt has not polished the freshly-created children at that point,
        so the size hint it would read is the empty-layout one. Measured: a
        first adjustSize() returned 32 px for a panel whose content needed 155,
        and a second call — after the same widgets had been through the event
        loop — returned 155. So this runs twice: once now, once on the next
        turn, from _rebuild.

        The re-centre and the repaint are needed because the overlay is placed
        by hand and is in no layout: shrinking it leaves it off-centre and
        leaves its former pixels on screen, since nothing tells the parent that
        region changed. The repaint has to cover the union of the old and new
        rectangles."""
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()

        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width()  - self.width())  // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    def _rebuild(self):
        before = self.geometry()
        self._clear_layout()

        from memory.memory_manager import all_entries_for_ui

        hdr = QLabel("🧠  WHAT JARVIS REMEMBERS")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        rows = all_entries_for_ui()

        cap = QLabel(f"{len(rows)} stored facts — newest first. "
                     f"Nothing here is sent anywhere; it lives in "
                     f"memory/long_term.json on this machine.")
        cap.setWordWrap(True)
        cap.setFont(QFont("Courier New", 7))
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._lay.addWidget(cap)

        if not rows:
            empty = QLabel("Nothing stored yet.")
            empty.setFont(QFont("Courier New", 9))
            empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(420, 34 * len(rows) + 10))
            scroll.setStyleSheet(
                f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                f"background: transparent; }}"
            )
            inner = QWidget()
            ilay  = QVBoxLayout(inner)
            ilay.setContentsMargins(6, 6, 6, 6)
            ilay.setSpacing(3)

            for r in rows:
                line = QHBoxLayout(); line.setSpacing(6)
                txt = QLabel(f"<b>{r['key'].replace('_', ' ')}</b> "
                             f"<span style='color:{C.TEXT_MED}'>— {r['value']}</span>")
                txt.setWordWrap(True)
                txt.setFont(QFont("Courier New", 8))
                txt.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                line.addWidget(txt, 1)

                meta = QLabel(f"{r['category'][:4]} · {r['updated'] or '—'}")
                meta.setFont(QFont("Courier New", 7))
                meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                line.addWidget(meta)

                rm = QPushButton("✕")
                rm.setFixedSize(20, 20)
                rm.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                rm.setCursor(Qt.CursorShape.PointingHandCursor)
                rm.setToolTip("Forget this")
                rm.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
                """)
                rm.clicked.connect(
                    lambda _=False, c=r["category"], k=r["key"]: self._forget(c, k))
                line.addWidget(rm)

                holder = QWidget()
                holder.setLayout(line)
                ilay.addWidget(holder)

            ilay.addStretch()
            scroll.setWidget(inner)
            self._lay.addWidget(scroll)

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        self._lay.addWidget(close)

        self._settle(before)
        # …and again once Qt has polished the new children, because the size
        # hint is not final until then. Harmless when the first pass already
        # got it right: _settle is idempotent.
        QTimer.singleShot(0, lambda g=before: self._settle(g))

    def _forget(self, category: str, key: str):
        from memory.memory_manager import forget
        forget(key, category)
        # Rebuild on the NEXT event-loop turn, not inside this click handler.
        # The rebuild destroys the very ✕ button that emitted this signal, and
        # Qt is entitled to touch the sender after a slot returns; tearing it
        # down mid-emission is how a widget ends up half-alive on screen.
        QTimer.singleShot(0, self._rebuild)



class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Jarvis actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Courier New", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class PluginSettingsOverlay(QWidget):
    """Floating overlay — renders per-plugin settings forms.

    Fully generic: it iterates the settings schemas a plugin declared via its
    PLUGIN_SETTINGS constant (delivered by PluginRegistry.settings_schemas) and
    builds a form for each. It knows NOTHING about any specific plugin, so the
    core stays clean and plugins remain pure drop-in — install a plugin that
    declares fields (e.g. the 3D-printer suite) and its section appears here;
    install none and this panel simply says there's nothing to configure.
    """

    _test_done = pyqtSignal(str, bool, str)   # namespace, ok, message
    _OW = 460

    def __init__(self, sections: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginSettingsOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._sections = sections or []
        self._widgets: dict[tuple, object] = {}    # (namespace, key) -> input widget
        self._types:   dict[tuple, str]    = {}     # (namespace, key) -> field type
        self._status_labels: dict[str, QLabel] = {} # namespace -> status QLabel
        self._test_done.connect(self._on_test_done)

        self._fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
                    f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        root.addWidget(self._lbl("⚙  PLUGIN SETTINGS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        if not self._sections:
            root.addWidget(self._lbl(
                "No configurable plugins are installed.\nDrop a plugin that needs "
                "settings (like the 3D-printer suite) into the plugins folder and "
                "it will show up here.", 9, color=C.TEXT_DIM))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            inner = QWidget()
            inner.setStyleSheet("background: transparent;")
            form = QVBoxLayout(inner)
            form.setContentsMargins(0, 0, 6, 0)
            form.setSpacing(6)
            for sec in self._sections:
                self._build_section(form, sec)
            form.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        # ── bottom buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        if self._sections:
            save_btn = QPushButton("▸  SAVE")
            save_btn.setFixedHeight(34)
            save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.TEXT};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            save_btn.clicked.connect(self._save_all)
            btn_row.addWidget(save_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _lbl(self, txt, fs=9, bold=False, color=C.PRI,
             align=Qt.AlignmentFlag.AlignLeft):
        w = QLabel(txt); w.setAlignment(align); w.setWordWrap(True)
        w.setFont(QFont("Courier New", fs,
                        QFont.Weight.Bold if bold else QFont.Weight.Normal))
        w.setStyleSheet(f"color: {_txt(color)}; background: transparent;")
        return w

    def _build_section(self, form: QVBoxLayout, sec: dict):
        ns     = sec.get("namespace") or sec.get("plugin") or "plugin"
        title  = sec.get("title") or ns
        fields = sec.get("fields") or []
        values = sec.get("values") or {}

        form.addSpacing(4)
        form.addWidget(self._lbl(title, 10, True, C.PRI))

        for field in fields:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key   = field["key"]
            ftype = (field.get("type") or "text").lower()
            label = field.get("label") or key
            default = field.get("default")
            stored  = values.get(key, default)

            form.addWidget(self._lbl(label.upper(), 8, color=C.TEXT_DIM))

            if ftype == "choice":
                w = QComboBox()
                w.addItems([str(o) for o in field.get("options", [])])
                w.setFont(QFont("Courier New", 9))
                w.setFixedHeight(30)
                w.setStyleSheet(
                    f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
                    f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
                    f"selection-background-color: {C.PRI_GHO}; }}")
                if stored is not None:
                    w.setCurrentText(str(stored))
            elif ftype == "toggle":
                w = QPushButton()
                w.setCheckable(True)
                w.setChecked(bool(stored))
                w.setFixedHeight(28)
                w.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                w.setCursor(Qt.CursorShape.PointingHandCursor)
                self._style_toggle(w)
                w.toggled.connect(lambda _=False, b=w: self._style_toggle(b))
            else:  # text / password
                w = QLineEdit("" if stored is None else str(stored))
                w.setFont(QFont("Courier New", 10))
                w.setFixedHeight(30)
                w.setStyleSheet(self._fs)
                if field.get("placeholder"):
                    w.setPlaceholderText(str(field["placeholder"]))
                if ftype == "password":
                    w.setEchoMode(QLineEdit.EchoMode.Password)

            self._widgets[(ns, key)] = w
            self._types[(ns, key)]   = ftype
            form.addWidget(w)

        # optional test/connect action button + status line
        action = sec.get("action")
        if isinstance(action, dict) and callable(action.get("run")):
            form.addSpacing(2)
            ab = QPushButton(str(action.get("label") or "TEST"))
            ab.setFixedHeight(30)
            ab.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            ab.setCursor(Qt.CursorShape.PointingHandCursor)
            ab.setStyleSheet(f"""
                QPushButton {{ background: #00091a; color: {C.TEXT};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            ab.clicked.connect(lambda _=False, n=ns: self._run_action(n))
            form.addWidget(ab)

        status = self._lbl("", 8, color=C.TEXT_DIM)
        self._status_labels[ns] = status
        form.addWidget(status)

        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        form.addWidget(line)

    def _style_toggle(self, btn: QPushButton):
        on = btn.isChecked()
        btn.setText("ON" if on else "OFF")
        if on:
            btn.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.TEXT}; "
                              f"border: 1px solid {C.PRI}; border-radius: 3px; }}")
        else:
            btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                              f"border: 1px solid {C.BORDER}; border-radius: 3px; }}")

    # ── data ──────────────────────────────────────────────────────────────────
    def _gather(self, ns: str) -> dict:
        out = {}
        for (n, key), w in self._widgets.items():
            if n != ns:
                continue
            t = self._types.get((n, key), "text")
            if t == "choice":
                out[key] = w.currentText()
            elif t == "toggle":
                out[key] = w.isChecked()
            else:
                out[key] = w.text().strip()
        return out

    def _save_ns(self, ns: str):
        from memory.config_manager import save_plugin_config
        save_plugin_config(ns, self._gather(ns))

    def _save_all(self):
        for sec in self._sections:
            ns = sec.get("namespace") or sec.get("plugin")
            if ns:
                self._save_ns(ns)
                lbl = self._status_labels.get(ns)
                if lbl:
                    lbl.setText("Saved ✓")
                    lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")

    def _run_action(self, ns: str):
        sec = next((s for s in self._sections
                    if (s.get("namespace") or s.get("plugin")) == ns), None)
        if not sec:
            return
        run_fn = (sec.get("action") or {}).get("run")
        if not callable(run_fn):
            return
        self._save_ns(ns)                 # persist what the user typed before testing
        values = self._gather(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Testing…")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        def worker():
            try:
                res = run_fn(values)
                if isinstance(res, tuple) and len(res) == 2:
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), str(res)
            except Exception as e:
                ok, msg = False, str(e)
            self._test_done.emit(ns, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, ns: str, ok: bool, msg: str):
        lbl = self._status_labels.get(ns)
        if not lbl:
            return
        lbl.setText(msg)
        color = C.PRI if ok else "#ff6b6b"
        lbl.setStyleSheet(f"color: {_txt(color)}; background: transparent;")


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {_txt(color)}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont(MONO_FONT, 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.TEXT};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("Press\nNEW CODE")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: transparent; border-radius: 10px;"
            )
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                # Only ever render a QR the mobile app can actually pair with:
                # it must carry the one-time `key`. A bare URL would be rejected.
                self._update_qr(auto if "key=" in (auto or "") else "")
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


def _api_field_qss(error: bool = False) -> str:
    """One look for every key input in the API overlay."""
    edge = C.RED if error else "rgba(255, 255, 255, 0.12)"
    focus = C.RED if error else C.PRI
    return f"""
        QLineEdit {{
            background: rgba(0, 0, 0, 0.30); color: {C.TEXT};
            border: 1px solid {edge}; border-radius: 6px; padding: 0 8px;
            selection-background-color: {_rgba(C.PRI, 0.45)};
        }}
        QLineEdit:focus {{ border: 1px solid {focus}; }}
    """


class GeminiKeyRow(QWidget):
    """One key field with its own ✕ button, and optionally its stack number.

    The number is what makes a stack of keys sortable in a person's head: with
    several free keys, "Key #2 is cooling" is actionable and "8f3a…b91c is
    cooling" is not. It is off by default so the Gemini list is unchanged.
    """

    remove_requested = pyqtSignal(object)      # emits the row itself
    _H = 32
    _NW = 30                                   # width of the "#N" column

    def __init__(self, text: str = "", parent=None, number: int = 0,
                 placeholder: str = "AIza…"):
        super().__init__(parent)
        self.setFixedHeight(self._H)
        self._err = False
        self._number = 0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._num = QLabel("")
        self._num.setFont(_f(_FS_CAPTION, True))
        self._num.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._num.setFixedWidth(self._NW)
        self._num.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._num)
        self.set_number(number)

        self.edit = QLineEdit(text)
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFont(_f(_FS_BODY))
        self.edit.setFixedHeight(self._H)
        self.edit.setStyleSheet(_api_field_qss())
        self.edit.textChanged.connect(lambda _t: self.set_error(False))
        lay.addWidget(self.edit, 1)

        self._x = QPushButton("✕")
        self._x.setFixedSize(self._H, self._H)
        self._x.setFont(_f(_FS_BODY, True))
        self._x.setCursor(Qt.CursorShape.PointingHandCursor)
        self._x.setToolTip("Remove this key")
        self._x.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px; }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 51, 85, 0.25);
                border-color: rgba(255, 51, 85, 0.6); }}
        """)
        self._x.clicked.connect(lambda: self.remove_requested.emit(self))
        lay.addWidget(self._x)

    @property
    def number(self) -> int:
        return self._number

    def set_number(self, n: int) -> None:
        """Set this row's 1-based number. 0 hides the column entirely."""
        self._number = int(n or 0)
        self._num.setText(f"#{self._number}" if self._number else "")
        self._num.setVisible(bool(self._number))

    def text(self) -> str:
        return self.edit.text().strip()

    def set_reveal(self, on: bool) -> None:
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if on
                              else QLineEdit.EchoMode.Password)

    def set_error(self, on: bool) -> None:
        if on != self._err:
            self._err = on
            self.edit.setStyleSheet(_api_field_qss(on))


class GeminiKeyList(QWidget):
    """Dynamic list of key fields.

    `+` adds a row, `✕` on a row removes it. The last row is never deleted —
    its ✕ just clears it — so there is always somewhere to type.
    Up to _VISIBLE rows show at once; more scroll inside the list.

    `numbered=True` puts `#1`, `#2`, … in front of each field and renumbers
    after every add/remove, so the number always matches the position in
    `keys()` — which is exactly the order `core.model_router` rotates through.
    """

    changed = pyqtSignal()           # row count changed → host may resize
    _SP = 6
    _VISIBLE = 4

    def __init__(self, keys=(), parent=None, *, title: str = "Gemini API Keys",
                 placeholder: str = "AIza…", numbered: bool = False):
        super().__init__(parent)
        self._rows: list[GeminiKeyRow] = []
        self._reveal = False
        self._numbered = bool(numbered)
        self._placeholder = placeholder

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(_section_label(title), 1)
        self._add_btn = QPushButton("+")
        self._add_btn.setFixedSize(28, 28)
        self._add_btn.setFont(_f(_FS_DATA, True))
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setToolTip(f"Add another key to {title}")
        self._add_btn.setStyleSheet(f"""
            QPushButton {{ background: {_rgba(C.PRI, 0.12)}; color: {C.TEXT};
                border: 1px solid {_rgba(C.PRI, 0.45)}; border-radius: 6px; }}
            QPushButton:hover {{ background: {_rgba(C.PRI, 0.28)}; border-color: {C.PRI}; }}
            QPushButton:pressed {{ background: {_rgba(C.PRI, 0.40)}; }}
        """)
        self._add_btn.clicked.connect(lambda: self.add_row())
        head.addWidget(self._add_btn)
        lay.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollArea > QWidget > QWidget {{ background: transparent; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {_rgba(C.PRI, 0.30)};
                border-radius: 3px; min-height: 20px; }}
            QScrollBar::handle:vertical:hover {{ background: {_rgba(C.PRI, 0.5)}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        self._host = QWidget()
        self._host.setObjectName("geminiKeyHost")
        self._host.setStyleSheet("QWidget#geminiKeyHost { background: transparent; }")
        self._v = QVBoxLayout(self._host)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(self._SP)
        self._v.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._host)
        lay.addWidget(self._scroll)

        for k in (list(keys) or [""]):
            self.add_row(k, focus=False)

    # ── rows ───────────────────────────────────────────────────────────────
    def add_row(self, text: str = "", focus: bool = True) -> GeminiKeyRow:
        row = GeminiKeyRow(text, self._host,
                           number=len(self._rows) + 1 if self._numbered else 0,
                           placeholder=self._placeholder)
        row.set_reveal(self._reveal)
        row.remove_requested.connect(self.remove_row)
        self._v.addWidget(row)
        self._rows.append(row)
        self._renumber()
        self._fit()
        if focus:
            row.edit.setFocus()
            QTimer.singleShot(0, self._scroll_to_end)
        return row

    def remove_row(self, row: GeminiKeyRow) -> None:
        if row not in self._rows:
            return
        if len(self._rows) == 1:               # keep one field alive: just clear it
            row.edit.clear()
            row.edit.setFocus()
            return
        self._rows.remove(row)
        self._v.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._renumber()
        self._fit()

    def _renumber(self) -> None:
        """Numbers must equal positions in `keys()` — that is the contract the
        Groq stack rotates on, so a stale number would name the wrong key."""
        if not self._numbered:
            return
        for i, row in enumerate(self._rows):
            row.set_number(i + 1)

    def _fit(self) -> None:
        n = max(1, len(self._rows))
        h = n * GeminiKeyRow._H + (n - 1) * self._SP
        cap = self._VISIBLE * GeminiKeyRow._H + (self._VISIBLE - 1) * self._SP
        self._scroll.setFixedHeight(min(h, cap) + 2)
        self.changed.emit()

    def _scroll_to_end(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ── data ───────────────────────────────────────────────────────────────
    def keys(self) -> list[str]:
        """Non-empty, de-duplicated, in order. A field holding several keys
        separated by commas/spaces (pasted list) is split, like the setup box."""
        out: list[str] = []
        for r in self._rows:
            for k in r.text().replace(",", " ").split():
                if k not in out:
                    out.append(k)
        return out

    def set_reveal(self, on: bool) -> None:
        self._reveal = on
        for r in self._rows:
            r.set_reveal(on)

    def flag_empty(self) -> None:
        """Mark the first field red and focus it (nothing was entered)."""
        if self._rows:
            self._rows[0].set_error(True)
            self._rows[0].edit.setFocus()


class ApiConfigOverlay(_HudOverlay):          # _HudOverlay is a QWidget
    """Glass popup: Gemini key list + OpenRouter key + Groq key stack.

    [APPLY & SAVE] writes config/api_keys.json (read-modify-write, so every
    other setting survives) and calls core.gemini.api_keys(refresh=True).
    Gemini schema matches SetupOverlay: first key → "gemini_api_key",
    the rest → "gemini_api_keys". Empty OpenRouter field removes its key.
    Groq is a numbered STACK rather than a single field, because its free tier
    is per-key and holding several is the point; it is written as
    "groq_api_key" + "groq_api_keys" and read back by core.model_router, which
    rotates through it and names each key by the same number shown here.
    """

    saved  = pyqtSignal()
    closed = pyqtSignal()

    _OW = 440
    _OPENROUTER_FIELD = "openrouter_api_key"
    _GROQ_FIELD       = "groq_api_key"
    _GROQ_LIST        = "groq_api_keys"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("apiConfig")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self._OW)
        self.setStyleSheet(f"""
            QWidget#apiConfig {{
                background: {_rgba(C.GLASS, 0.86)};
                border: 1px solid {_glass_border(True)};
                border-radius: 14px;
            }}
            QWidget#apiConfig QLabel {{ background: transparent; border: none; }}
        """)

        cfg = _read_full_config()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 16, 22, 18)
        lay.setSpacing(8)

        # ── title bar ──
        bar = QHBoxLayout()
        bar.setSpacing(6)
        title = QLabel("◈  API CONFIGURATION")
        title.setFont(_f(_FS_DATA + 1, True))
        title.setStyleSheet(f"color: {C.TEXT};")
        bar.addWidget(title, 1)

        small = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px; }}
            QPushButton:hover {{ color: #ffffff; background: {_rgba(C.PRI, 0.14)};
                border-color: {_rgba(C.PRI, 0.5)}; }}
            QPushButton:checked {{ color: {C.TEXT}; background: {_rgba(C.PRI, 0.22)};
                border-color: {C.PRI}; }}
        """
        self._eye = QPushButton("👁")
        self._eye.setCheckable(True)
        self._eye.setFixedSize(28, 28)
        self._eye.setCursor(Qt.CursorShape.PointingHandCursor)
        self._eye.setToolTip("Show / hide keys")
        self._eye.setStyleSheet(small)
        self._eye.toggled.connect(self._set_reveal)
        bar.addWidget(self._eye)

        xbtn = QPushButton("✕")
        xbtn.setFixedSize(28, 28)
        xbtn.setFont(_f(_FS_BODY, True))
        xbtn.setCursor(Qt.CursorShape.PointingHandCursor)
        xbtn.setToolTip("Close without saving  [Esc]")
        xbtn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px; }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 51, 85, 0.25);
                border-color: rgba(255, 51, 85, 0.6); }}
        """)
        xbtn.clicked.connect(self._do_close)
        bar.addWidget(xbtn)
        lay.addLayout(bar)

        sub = QLabel("Stored locally in config/api_keys.json")
        sub.setFont(_f(_FS_CAPTION))
        sub.setStyleSheet(f"color: {C.TEXT_DIM};")
        lay.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_glass_border()};")
        lay.addWidget(sep)

        # ── Gemini (dynamic) ──
        self._gemini = GeminiKeyList(self._saved_gemini_keys(cfg))
        self._gemini.changed.connect(self._refit)
        lay.addWidget(self._gemini)
        lay.addSpacing(4)

        # ── OpenRouter ──
        self._or_edit = self._key_field(cfg.get(self._OPENROUTER_FIELD, ""), "sk-or-…")
        lay.addWidget(_section_label("OpenRouter API Key"))
        lay.addWidget(self._or_edit)

        # ── Groq, numbered stack ──
        # A list, not a field: Groq's free tier is per-key, so stacking several
        # and rotating is the whole point. The number in front of each row is the
        # same number the log prints when that key is used or rested, and it is
        # the key's position in `groq_api_keys`.
        lay.addSpacing(4)
        self._groq = GeminiKeyList(
            self._saved_keys(cfg, self._GROQ_FIELD, self._GROQ_LIST),
            title="Groq API Keys",
            placeholder="gsk_…",
            numbered=True,
        )
        self._groq.changed.connect(self._refit)
        lay.addWidget(self._groq)

        # A read-only view onto the router's own key records: the same numbers
        # the log prints, so the screen and the log cannot tell two different
        # stories about which key is in use. Not a second source of truth.
        self._groq_state = QLabel("")
        self._groq_state.setFont(_f(_FS_CAPTION))
        self._groq_state.setWordWrap(True)
        self._groq_state.setStyleSheet(f"color: {C.TEXT_DIM};")
        lay.addWidget(self._groq_state)

        # ── status + buttons ──
        self._status = QLabel("")
        self._status.setFont(_f(_FS_CAPTION))
        self._status.setWordWrap(True)
        self._status.setMinimumHeight(16)
        lay.addWidget(self._status)

        row = QHBoxLayout()
        row.setSpacing(8)
        cancel = QPushButton("CANCEL")
        cancel.setFixedHeight(34)
        cancel.setFont(_f(_FS_BODY, True))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: rgba(255, 255, 255, 0.04); color: {C.TEXT_MED};
                border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px; }}
            QPushButton:hover {{ color: {C.TEXT}; background: {_rgba(C.PRI, 0.08)};
                border-color: {_rgba(C.PRI, 0.4)}; }}
        """)
        cancel.clicked.connect(self._do_close)
        row.addWidget(cancel, 1)

        apply_btn = QPushButton("APPLY && SAVE")     # && = literal & (no mnemonic)
        apply_btn.setFixedHeight(34)
        apply_btn.setFont(_f(_FS_BODY, True))
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {_rgba(C.PRI, 0.30)}, stop:1 {_rgba(C.PRI, 0.12)});
                color: {C.TEXT}; border: 1px solid {_rgba(C.PRI, 0.6)}; border-radius: 6px; }}
            QPushButton:hover {{ border-color: {C.PRI}; background: {_rgba(C.PRI, 0.34)}; }}
            QPushButton:pressed {{ background: {_rgba(C.PRI, 0.46)}; }}
        """)
        apply_btn.clicked.connect(self._apply)
        row.addWidget(apply_btn, 2)
        lay.addLayout(row)

        self._refresh_key_state()        # the overlay is rebuilt on every open

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _key_field(text: str, placeholder: str) -> QLineEdit:
        e = QLineEdit(str(text or ""))
        e.setEchoMode(QLineEdit.EchoMode.Password)
        e.setPlaceholderText(placeholder)
        e.setFont(_f(_FS_BODY))
        e.setFixedHeight(32)
        e.setStyleSheet(_api_field_qss())
        return e

    @staticmethod
    def _saved_keys(cfg: dict, *fields: str) -> list[str]:
        """Every non-empty key stored under any of `fields`, in order, deduped.

        Accepts a single string or a list per field, and splits comma/space
        separated pastes — the same leniency `GeminiKeyList.keys()` applies on
        the way out, so a round trip through the UI does not change the file.
        """
        out: list[str] = []
        for field in fields:
            v = cfg.get(field)
            for item in (v if isinstance(v, (list, tuple)) else [v]):
                for k in str(item or "").replace(",", " ").split():
                    if k not in out:
                        out.append(k)
        return out

    @staticmethod
    def _saved_gemini_keys(cfg: dict) -> list[str]:
        return ApiConfigOverlay._saved_keys(cfg, "gemini_api_key", "gemini_api_keys")

    def _refresh_key_state(self) -> None:
        """Show which Groq key number is active / cooling, from the router itself."""
        try:
            from core import model_router
            self._groq_state.setText("Groq: " + model_router.key_status("groq"))
        except Exception as e:                            # noqa: BLE001 - cosmetic only
            self._groq_state.setText("")
            print(f"[UI] model_router.key_status failed: {e}")

    def _set_reveal(self, on: bool) -> None:
        self._gemini.set_reveal(on)
        self._groq.set_reveal(on)
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        self._or_edit.setEchoMode(mode)

    def _say(self, msg: str, error: bool = False) -> None:
        self._status.setStyleSheet(f"color: {C.RED if error else C.GREEN};")
        self._status.setText(msg)

    def _refit(self) -> None:
        """Rows were added/removed: resize and keep the panel centred."""
        # Qt passes a child's size-hint change upward through a *posted* event,
        # so at this point our own layout still holds the old hint. Refresh
        # bottom-up by hand, then resize.
        self._gemini.layout().invalidate()
        self._gemini.layout().activate()
        self._gemini.updateGeometry()
        self._groq.layout().invalidate()
        self._groq.layout().activate()
        self._groq.updateGeometry()
        lay = self.layout()
        lay.invalidate()
        lay.activate()
        self.adjustSize()
        p = self.parentWidget()
        if p is not None:
            self.move(max(0, (p.width()  - self.width())  // 2),
                      max(0, (p.height() - self.height()) // 2))

    # ── save ───────────────────────────────────────────────────────────────
    @classmethod
    def _write(cls, gemini: list[str], openrouter: str, groq: list[str]) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = _read_full_config()                # keep every other setting
        data["gemini_api_key"] = gemini[0]
        if len(gemini) > 1:
            data["gemini_api_keys"] = gemini[1:]
        else:
            data.pop("gemini_api_keys", None)
        if openrouter:
            data[cls._OPENROUTER_FIELD] = openrouter
        else:
            data.pop(cls._OPENROUTER_FIELD, None)
        # Groq gets the same schema as Gemini — first key in the plain field, the
        # rest in the list — because core.model_router already reads both that
        # way. Anything else that knows "groq_api_key" keeps working.
        if groq:
            data[cls._GROQ_FIELD] = groq[0]
            if len(groq) > 1:
                data[cls._GROQ_LIST] = groq[1:]
            else:
                data.pop(cls._GROQ_LIST, None)
        else:
            data.pop(cls._GROQ_FIELD, None)
            data.pop(cls._GROQ_LIST, None)
        tmp = API_FILE.with_name(API_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
        os.replace(tmp, API_FILE)                 # never leaves a half-written file

    def _apply(self) -> None:
        gemini = self._gemini.keys()
        if not gemini:
            self._gemini.flag_empty()
            self._say("Add at least one Gemini key.", error=True)
            return
        try:
            self._write(gemini,
                        self._or_edit.text().strip(),
                        self._groq.keys())
        except Exception as e:                    # noqa: BLE001 - shown, not fatal
            self._say(f"Save failed — {e}", error=True)
            return
        try:                                      # key list is cached; reload it now
            from core import gemini as _g
            _g.api_keys(refresh=True)
        except Exception as e:                    # noqa: BLE001
            print(f"[UI] gemini.api_keys(refresh=True) failed: {e}")
        try:                                      # the router re-reads on mtime, so just report
            from core import model_router
            print(f"[Router] Groq: {model_router.key_status('groq')}")
        except Exception as e:                    # noqa: BLE001
            print(f"[UI] model_router.key_status failed: {e}")
        self.saved.emit()
        self._do_close()

    def _do_close(self) -> None:
        self.hide()
        self.closed.emit()


class SideDrawer(_HudOverlay):
    """One sliding sidebar for Setup and Controls.

    Docks to the left edge between the header and the footer and slides in and
    out with a 250 ms OutCubic animation. It holds one page per tab; the header's
    Setup / Controls buttons choose the page, so the drawer has no tab strip of
    its own. There is only ever one drawer, so nothing can stack on top of itself.
    """
    state_changed = pyqtSignal(str)      # "" when closed, else the open tab key

    _W = 292
    _MS = 250

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("sideDrawer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._top = 0
        self._h = 400
        self._tab = ""
        self._open = False
        self._pages: dict[str, int] = {}
        self._captions: dict[str, str] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        bar = QHBoxLayout(); bar.setSpacing(6)
        self._caption = QLabel("")
        self._caption.setFont(_f(_FS_CAPTION))
        bar.addWidget(self._caption, 1)      # what this page is for
        self._close_btn = QPushButton("✕")
        self._close_btn.setFixedSize(28, 28)
        self._close_btn.setFont(_f(_FS_BODY, True))
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setToolTip("Close  [Esc]")
        self._close_btn.clicked.connect(self.slide_close)
        bar.addWidget(self._close_btn)
        lay.addLayout(bar)

        self._stack = QStackedWidget()
        lay.addWidget(self._stack, 1)

        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(self._MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._anim_done)
        self.restyle()
        self.hide()

    # ── content ────────────────────────────────────────────────────────────
    def add_tab(self, key: str, label: str, page: QWidget, caption: str) -> None:
        """Register a page. `label` is kept for the callers' sake; the header's
        own buttons are what the user clicks, so no button is made here."""
        self._pages[key] = self._stack.addWidget(page)
        self._captions[key] = caption

    def restyle(self) -> None:
        self.setStyleSheet(f"""
            QWidget#sideDrawer {{
                background: rgba(4, 12, 22, 0.94);
                border: 1px solid {_rgba(C.PRI, 0.3)};
                border-left: none;
                border-top-right-radius: 8px;
                border-bottom-right-radius: 8px;
            }}
            QWidget#sideDrawer QLabel {{ background: transparent; border: none;
                                         color: {C.TEXT_DIM}; }}
        """)
        self._close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px; }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 51, 85, 0.25);
                border-color: rgba(255, 51, 85, 0.6); }}
        """)

    # ── state ──────────────────────────────────────────────────────────────
    def is_open(self) -> bool:
        return self._open

    def place(self, top: int, height: int) -> None:
        """Re-dock after the window changed size."""
        self._top, self._h = top, max(120, height)
        self.setFixedSize(self._W, self._h)
        if self._anim.state() != QPropertyAnimation.State.Running:
            self.move(0 if self._open else -self._W, self._top)

    def set_tab(self, key: str) -> None:
        if key not in self._pages:
            return
        self._tab = key
        self._stack.setCurrentIndex(self._pages[key])
        self._caption.setText(self._captions.get(key, ""))
        if self._open:
            self.state_changed.emit(key)

    def toggle(self, key: str) -> None:
        if self._open and self._tab == key:
            self.slide_close()
        else:
            self.slide_open(key)

    def slide_open(self, key: str) -> None:
        self.set_tab(key)
        if self._open:                       # already out: just switched tab
            self.state_changed.emit(key)
            return
        start = self.pos() if self.isVisible() else QPoint(-self._W, self._top)
        self._open = True
        self.setFixedSize(self._W, self._h)
        self.move(start)
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(start)
        self._anim.setEndValue(QPoint(0, self._top))
        self._anim.start()
        self.state_changed.emit(key)

    def slide_close(self) -> None:
        if not self._open:
            return
        self._open = False
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(-self._W, self._top))
        self._anim.start()
        self.state_changed.emit("")

    def _anim_done(self) -> None:
        if not self._open:
            self.hide()



class _ViewportStage(QWidget):
    """Holds the connected [System Monitor + HUD/Board] sliding track underneath,
    and the [Activity Log] panel on top at the right edge (two-whiteboards effect)."""

    def __init__(self, win: "MainWindow", parent=None):
        super().__init__(parent)
        self._win = win

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._win._layout_stage()


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _wake_btns_sig   = pyqtSignal()          # wake state resolved off-thread
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _wake_dl_sig    = pyqtSignal(bool, str)  # wake-word install finished (ok, message)
    _quiz_sig       = pyqtSignal(str, object, object)  # (topic, questions, grader)
    _study_mode_sig  = pyqtSignal(bool)  # (on, notify_voice) — Study Station on/off (thread-safe)
    _study_board_sig = pyqtSignal(str, str)  # (op, JSON) -> study canvas
    _notepad_set_sig = pyqtSignal(str)       # Thread-safe notepad text updater

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        # Apply the saved UI colour BEFORE panels/stylesheets are built
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — {APP_VERSION}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.request_say       = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen
        self.get_plugins       = None   # callable: () -> list[dict], set by JarvisLive
        self.get_plugin_settings = None # callable: () -> list[dict] settings schemas, set by JarvisLive
        self.on_wake_toggle    = None   # callable: (enable: bool) -> str, set by JarvisLive
        self.on_wake_manual    = None   # callable: () -> None — manual sleep/wake
        self.on_push_to_talk   = None   # callable: (enable: bool) -> str scope
        self.on_study_document = None   # callable: (path: str) -> None — Phase 5 ingest
        self.on_study_documents = None  # callable: (paths: list[str]) -> None — batch ingest
        self.ptt_hold          = None   # callable: (held: bool) -> None — windowed chord
        self.on_barge_toggle   = None   # callable: (enable: bool) -> str, set by JarvisLive
        self.get_barge_state   = None   # callable: () -> dict {enabled, sensitivity, ...}
        self.wake_get_state    = None   # callable: () -> dict {enabled, awake, ready}
        self._muted            = False
        self._current_file: str | None = None
        self._current_files: list[str] = []
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None
        self._audio_overlay  = None
        self._memory_overlay  = None
        self._plugin_manager_overlay  = None
        self._plugin_settings_overlay  = None

        central = QWidget()
        central.setObjectName("glassRoot")
        central.setStyleSheet(f"""
            QWidget {{ background: {C.BG}; }}
            QWidget#glassRoot {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {C.BG}, stop:0.55 {_rgba(C.PRI_GHO, 1.0)}, stop:1 {C.BG});
            }}
        """)
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        self._stage = _ViewportStage(self)
        self._slide_offset = 0          # 0 = normal, -(_LEFT_W + 8) = slid left for Study Mode
        self._chat_offset = 0           # 0 = chat visible, +(_RIGHT_W + 8) = chat slid off right
        self._slide_track = QWidget(self._stage)
        self._slide_track.setStyleSheet('background: transparent;')
        track_lay = QHBoxLayout(self._slide_track)
        track_lay.setContentsMargins(0, 0, 0, 0)
        track_lay.setSpacing(8)

        self._left_panel = self._build_left_panel()
        track_lay.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()
        self._quiz_panel = self._build_quiz_panel()

        # Stack: 0 = animated HUD, then the Study Station board (its page loads on first use)
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._study_on = False
        self._left_anim = None
        self._study_canvas = StudyCanvasWidget(self)
        self._study_canvas.user_event.connect(self._on_study_event)
        self._hud_cam_stack.addWidget(self._study_canvas)
        self._study_idx = self._hud_cam_stack.indexOf(self._study_canvas)
        self._study_canvas.ensure_loaded()

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.addWidget(self._quiz_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)

        # Horizontal splitter for Study Mode: [Draggable Notepad | Study Board]
        self._study_notepad_cont = self._build_study_notepad()
        self._work_split = QSplitter(Qt.Orientation.Horizontal)
        self._work_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                width: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI};
            }}
        """)
        self._work_split.addWidget(self._study_notepad_cont)
        self._work_split.addWidget(self._center_split)
        self._work_split.setStretchFactor(0, 1)
        self._work_split.setStretchFactor(1, 3)
        self._work_split.setCollapsible(1, False)
        track_lay.addWidget(self._work_split, stretch=1)

        self._right_panel = self._build_right_panel()
        self._right_panel.setParent(self._stage)
        self._right_panel.raise_()

        root.addWidget(self._stage, stretch=1)
        root.addWidget(self._build_footer())

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._side_drawer = self._build_side_drawer()
        self._side_drawer.state_changed.connect(self._on_drawer_state)
        self._place_drawer()
        QApplication.instance().installEventFilter(self)   # click-outside closes
        self._warm_wake_state()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metric update timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._wake_btns_sig.connect(self._refresh_wake_btns)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._wake_dl_sig.connect(self._on_wake_install_done)
        self._quiz_sig.connect(self._show_quiz)
        self._study_mode_sig.connect(self._set_study_mode)
        self._study_board_sig.connect(self._on_study_board)
        self._notepad_set_sig.connect(self._set_notepad_text_slot)

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_study = QShortcut(QKeySequence("F8"), self)          # Study Station toggle
        sc_study.activated.connect(lambda: self._set_study_mode(not self._study_on))
        sc_chat = QShortcut(QKeySequence("Ctrl+H"), self)       # Hide / Show Chat Drawer
        sc_chat.activated.connect(self._toggle_right_panel)
        sc_note = QShortcut(QKeySequence("Ctrl+N"), self)       # Toggle Split-Screen Notepad
        sc_note.activated.connect(self._toggle_study_notepad)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._on_escape)

    # --- Study Station (Connected Sliding Track Architecture) ----------------
    def _layout_stage(self) -> None:
        """Position the connected [System Monitor + HUD/Board] track and the
        [Activity Log] foreground whiteboard without squishing any widget."""
        if not hasattr(self, "_stage"):
            return
        W = self._stage.width()
        H = self._stage.height()
        pad = 8
        avail_h = max(100, H - pad * 2)

        # Right panel (Activity Log) sits in the foreground on the right
        rx = W - pad - _RIGHT_W + int(getattr(self, "_chat_offset", 0))
        self._right_panel.setGeometry(rx, pad, _RIGHT_W, avail_h)
        self._right_panel.raise_()

        # Right edge visible to the user (left of Activity Log, or right window edge if chat hidden)
        right_vis = rx - pad

        # Track starts at x = pad + _slide_offset (slides from +8 to -148).
        tx = pad + int(getattr(self, "_slide_offset", 0))
        tw = max(320, right_vis - tx)
        self._slide_track.setGeometry(tx, pad, tw, avail_h)

    def _animate_left_panel(self, collapse: bool) -> None:
        """Slide the connected [System Monitor + Center Board] track left/right
        as one solid unit. System Monitor moves off-screen left while the board
        emerges from underneath the Activity Log."""
        if getattr(self, "_left_anim", None) is not None:
            self._left_anim.stop()

        step = _LEFT_W + 8  # 156 px
        start_x = int(getattr(self, "_slide_offset", 0))
        end_x = -step if collapse else 0

        anim = QVariantAnimation(self)
        anim.setDuration(300)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(start_x)
        anim.setEndValue(end_x)

        def _step(val) -> None:
            self._slide_offset = int(val)
            self._layout_stage()

        def _done() -> None:
            self._slide_offset = end_x
            self._layout_stage()
            self._left_anim = None

        anim.valueChanged.connect(_step)
        anim.finished.connect(_done)
        self._left_anim = anim
        anim.start()

    def _animate_right_panel(self, collapse: bool) -> None:
        """Slide the foreground Activity Log panel off the right edge [Ctrl+H]."""
        if getattr(self, "_right_anim", None) is not None:
            self._right_anim.stop()

        step = _RIGHT_W + 8
        start_x = int(getattr(self, "_chat_offset", 0))
        end_x = step if collapse else 0

        if not collapse:
            self._right_panel.show()

        anim = QVariantAnimation(self)
        anim.setDuration(280)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(start_x)
        anim.setEndValue(end_x)

        def _step(val) -> None:
            self._chat_offset = int(val)
            self._layout_stage()

        def _done() -> None:
            self._chat_offset = end_x
            if collapse:
                self._right_panel.hide()
            self._layout_stage()
            self._right_anim = None

        anim.valueChanged.connect(_step)
        anim.finished.connect(_done)
        self._right_anim = anim
        anim.start()

    def _toggle_right_panel(self) -> None:
        if not self._study_on:
            return
        is_hidden = getattr(self, "_chat_offset", 0) > 0 or not self._right_panel.isVisible()
        self._animate_right_panel(collapse=not is_hidden)

    def _build_study_notepad(self) -> QWidget:
        w = QWidget()
        w.setObjectName("StudyNotepad")
        w.setMinimumWidth(180)
        w.setStyleSheet(
            f"QWidget#StudyNotepad {{ background: {_rgba(C.GLASS, 0.82)}; "
            f"border: 1px solid {_glass_border()}; border-radius: 8px; }}"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        title = QLabel("📝  STUDY NOTEPAD")
        title.setFont(_f(_FS_BODY, True))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        hdr.addWidget(title)
        hdr.addStretch(1)
        chk_btn = QPushButton("✓ Check Notes")
        chk_btn.setFixedHeight(22)
        chk_btn.setFont(_f(_FS_CAPTION, True))
        chk_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        chk_btn.setToolTip("Ask JARVIS to review and correct your notes")
        chk_btn.setStyleSheet(
            f"QPushButton {{ background: {_rgba(C.PRI, 0.16)}; color: {C.TEXT}; "
            f"border: 1px solid {_rgba(C.PRI, 0.5)}; border-radius: 4px; padding: 0 8px; }}"
            f"QPushButton:hover {{ background: {_rgba(C.PRI, 0.30)}; border-color: {C.PRI}; }}"
        )
        chk_btn.clicked.connect(self._check_study_notes)
        hdr.addWidget(chk_btn)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setFont(_f(_FS_CAPTION, True))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("Hide Notepad  [Ctrl+N]")
        close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C.TEXT_DIM}; "
            f"border: 1px solid {C.BORDER}; border-radius: 4px; }}"
            f"QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}"
        )
        close_btn.clicked.connect(lambda: self._study_notepad_cont.hide())
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)
        self._study_notepad = QTextEdit()
        self._study_notepad.setAcceptRichText(False)
        # Wrap at the widget edge; without this a long line scrolls sideways and
        # the text is clipped rather than reflowed (default is WidgetWidth, set
        # explicitly so a future Qt change cannot regress it).
        self._study_notepad.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._study_notepad.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._study_notepad.setPlaceholderText(
            "Write your rough notes, formulas, code, or practice answers here...\n\n"
            "• Drag the right edge to resize this notepad.\n"
            "• Click '✓ Check Notes' (or ask JARVIS) to evaluate what you wrote."
        )
        self._study_notepad.setFont(_f(_FS_BODY + 1))
        self._study_notepad.setStyleSheet(
            f"QTextEdit {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 6px; padding: 8px; "
            f"selection-background-color: {C.PRI_DIM}; }}"
            f"QTextEdit:focus {{ border: 1px solid {_rgba(C.PRI, 0.65)}; }}"
        )
        lay.addWidget(self._study_notepad, stretch=1)
        self._notepad_text_cached: str = ""
        self._study_notepad.textChanged.connect(self._sync_notepad_text)
        w.hide()
        return w

    def _sync_notepad_text(self) -> None:
        """Main GUI thread slot: keeps a thread-safe copy of notepad text."""
        if hasattr(self, "_study_notepad"):
            self._notepad_text_cached = self._study_notepad.toPlainText()

    def _toggle_study_notepad(self) -> None:
        if not self._study_on:
            return
        if self._study_notepad_cont.isVisible():
            self._study_notepad_cont.hide()
        else:
            self._study_notepad_cont.show()
            total = max(600, self._work_split.width())
            self._work_split.setSizes([int(total * 0.32), int(total * 0.68)])
            self._study_notepad.setFocus()

    def _check_study_notes(self) -> None:
        txt = self.get_notepad_text().strip()
        if not txt:
            self._log_sig.emit("SYS: Notepad is empty — write something first.")
            return
        self._log_sig.emit("You: [Check Study Notepad]")
        if self.on_text_command:
            msg = (
                "[STUDY_CHECK_NOTES] Please review the notes I wrote in my Study Notepad, "
                "point out any mistakes or missing concepts, and show the corrected version "
                f"on the Study Board:\n\n{txt[:3000]}"
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def get_notepad_text(self) -> str:
        """Thread-safe: reads cached text without blocking or touching Qt widgets from worker threads."""
        return getattr(self, "_notepad_text_cached", "")

    def set_notepad_text(self, text: str) -> None:
        """Thread-safe: dispatches update to Qt GUI thread via signal."""
        self._notepad_set_sig.emit(str(text or ""))

    def _set_notepad_text_slot(self, text: str) -> None:
        """Main GUI thread slot: updates QTextEdit safely."""
        if hasattr(self, "_study_notepad"):
            self._study_notepad.setPlainText(str(text or ""))
            self._notepad_text_cached = str(text or "")
            if self._study_on and not self._study_notepad_cont.isVisible():
                self._toggle_study_notepad()

    def _set_study_mode(self, on: bool, notify_voice: bool = True) -> None:
        on = bool(on)
        if on == self._study_on:
            return
        if on and not HAVE_STUDY:
            self._log_sig.emit("SYS: Study board needs the Qt web engine: pip install PyQt6-WebEngine")
            return
        study_guard.set_study_active(on)
        self._study_on = on
        if on:
            if hasattr(self, "_metric_tmr"):
                self._metric_tmr.stop()
            self._study_canvas.ensure_loaded()
            self._hud_cam_stack.setCurrentIndex(self._study_idx)
            self._refit_center_split()
            # The splitter is often not laid out yet at the instant Study Mode is
            # switched on, so the height read above can be stale. Re-assert the
            # sizes once the layout has settled and the board has the room.
            QTimer.singleShot(150, self._refit_center_split)
        else:
            if hasattr(self, "_study_notepad_cont"):
                self._study_notepad_cont.hide()
            if hasattr(self, "_metric_tmr") and not self._metric_tmr.isActive():
                self._metric_tmr.start(2000)
            if getattr(self, "_chat_offset", 0) > 0 or not self._right_panel.isVisible():
                self._animate_right_panel(collapse=False)
            self._hud_cam_stack.setCurrentIndex(0)
            total = max(120, self._center_split.height())
            content = 260 if self._content_panel.isVisible() else 0
            quiz = 250 if self._quiz_panel.isVisible() else 0
            if content or quiz:
                self._center_split.setSizes([max(total - content - quiz, 120), content, quiz])
        self._animate_left_panel(collapse=on)
        self._log_sig.emit("SYS: Study Station " + ("ON — [Ctrl+H] Chat · [Ctrl+N] Notepad" if on else "OFF"))
        # Only notify speech if requested (suppressed during programmatic tool executions to avoid WebSocket collision)
        if notify_voice and getattr(self, "request_say", None):
            state_note = (
                "[MODE_SWITCH: STUDY_MODE_ON] The user just switched Study Mode ON (Study Board is now open on screen). "
                "You are now in STUDY MODE. Adopt your warm, dedicated Teacher/Mentor persona immediately. "
                "Do not speak unless the user just asked you something."
                if on else
                "[MODE_SWITCH: NORMAL_MODE_ON] The user just exited Study Mode and returned to Normal HUD Mode (STUDY_MODE = OFF). "
                "Switch back to your normal personal assistant persona. Do not speak unless the user just asked you something."
            )
            threading.Thread(target=self.request_say, args=(state_note,), daemon=True).start()

    def _on_study_board(self, op: str, payload: str) -> None:
        d = json.loads(payload) if payload else {}
        c = self._study_canvas
        if op == "solution":
            c.show_solution(d.get("title", ""), d.get("markdown", ""),
                            d.get("subject", ""), d.get("reveal", "steps"))
        elif op == "quiz":
            self._active_quiz_topic = d.get("title", "Practice Quiz")
            c.show_quiz(d.get("title", ""), d.get("questions", []), d.get("subject", ""))
        elif op == "flashcards":
            c.show_flashcards(d.get("title", ""), d.get("cards", []), d.get("subject", ""))
        elif op == "mindmap":
            c.show_mindmap(d.get("title", ""), d.get("tree", {}), d.get("subject", ""))
        elif op == "status":
            c.show_status(d.get("text", ""))
        elif op == "next":
            c.next_step()
        elif op == "prev":
            c.prev_step()
        elif op == "restore":
            c.restore(d.get("target", ""))
        elif op == "clear":
            c.clear()
    def _on_study_event(self, kind: str, data: dict) -> None:
        if kind == "board_cleared":
            try:
                from actions import study_quiz
                study_quiz.set_quiz_active(False)
            except Exception:
                pass
            return
        if kind == "exit":
            try:
                from actions import study_quiz
                study_quiz.set_quiz_active(False)
            except Exception:
                pass
            self._set_study_mode(False)
        elif kind == "explain_step" and self.on_text_command:
            msg = (f"বোর্ডের সমাধানের ধাপ {data.get('n', '')} ({data.get('title', '')}) সহজ বাংলায় বিস্তারিত বুঝিয়ে দাও।")
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()
        elif kind == "explain_line" and self.on_text_command:
            lbl = data.get("label", "")
            cnt = data.get("content", "")
            self._log_sig.emit(f"You: [{lbl} বুঝিয়ে দাও]")
            msg = (
                f"বোর্ডে দেওয়া অংকের `{lbl}` ({cnt}) কীভাবে এসেছে, আগের লাইন থেকে এই লাইনে কোন সূত্র বা বীজগণিতিক ক্যালকুলেশন করা হয়েছে তা সহজ বাংলায় ধাপে ধাপে বুঝিয়ে দাও।"
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()
        elif kind == "quiz_complete":
            try:
                from actions import study_quiz
                study_quiz.set_quiz_active(False)
            except Exception:
                pass
            if data.get("clear"):
                return
            topic = str(data.get("topic") or getattr(self, "_active_quiz_topic", "Practice Quiz"))
            score = data.get("score", 0)
            total = data.get("total", 0)
            mistakes = data.get("mistakes") or []
            self._log_sig.emit(f"QUIZ: {topic} — {score}/{total} correct")
            try:
                from memory import study_cache
                study_cache.add_mistakes(topic, mistakes)
            except Exception:
                pass
            if self.on_text_command:
                wrong_txt = "; ".join(
                    f"Q: {m.get('question','')} (Picked: {m.get('given','')}, Correct: {m.get('answer','')})"
                    for m in mistakes[:5]
                )
                msg = (
                    f"[STUDY_QUIZ_DONE — QUIZ IS NOW FINISHED AND CLOSED] Topic='{topic}', Score={score}/{total}. "
                    + (f"Mistakes made by student: {wrong_txt}. " if wrong_txt else "Perfect score! ")
                    + "Give the student brief, encouraging feedback in 1-2 sentences in Bangla and ask if they want any of the mistaken questions explained deeply. "
                    + "CRITICAL RULE: The quiz is OVER, so the anti-cheat lock is OFF. If the student asks you to explain, solve, or deeply clarify any question from this quiz (or anything else), you MUST call the `study_solve_problem` tool (with the full question/concept text and reveal='all') so the visual step-by-step explanation actually appears on the Study Board! Never claim you showed something on the board without calling `study_solve_problem`."
                )
                threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # Header title — keep it centred on the full bar width
        hc = getattr(self, "_header_center", None)
        if hc is not None:
            hc.setGeometry(0, 0, hc.parentWidget().width(), 54)
        # Side drawer — keep it docked between header and footer
        self._place_drawer()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.setVisible(True)
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.setVisible(False)

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            self._bar_tmp.setVisible(True)
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.setVisible(False)

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setObjectName("glassHeader")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setFixedHeight(54)
        w.setStyleSheet(f"""
            QWidget#glassHeader {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {_rgba(C.GLASS, 0.88)}, stop:1 {_rgba(C.GLASS, 0.70)});
                border-bottom: 1px solid {_glass_border()};
            }}
            QWidget#glassHeader QLabel {{ background: transparent; border: none; }}
        """)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        def _badge(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(_f(_FS_CAPTION))
            l.setStyleSheet(f"color: {_txt(color)}; background: transparent;")
            return l

        lay.addWidget(_badge(APP_VERSION, C.PRI_DIM))
        lay.addSpacing(8)
        # Pill toggles for the one side drawer. They light up with a neon edge
        # (and a soft glow) while their tab is open; pressing the lit one again
        # slides the drawer back in.
        def _pill(text: str, tip: str, tab: str) -> QPushButton:
            btn = QPushButton(text)
            btn.setFixedHeight(28)
            btn.setFont(_f(_FS_BODY, True))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setCheckable(True)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255, 255, 255, 0.04); color: {C.TEXT_MED};
                    border: 1px solid rgba(255, 255, 255, 0.10);
                    border-radius: 6px; padding: 0 12px;
                }}
                QPushButton:hover {{
                    color: {C.TEXT}; background: {_rgba(C.PRI, 0.10)};
                    border-color: {_rgba(C.PRI, 0.45)};
                }}
                QPushButton:checked {{
                    color: {C.PRI}; background: {_rgba(C.PRI, 0.18)};
                    border: 1px solid {_rgba(C.PRI, 0.9)};
                }}
            """)
            fx = QGraphicsDropShadowEffect(btn)
            fx.setBlurRadius(18); fx.setOffset(0, 0)
            fx.setColor(QColor(C.PRI)); fx.setEnabled(False)
            btn.setGraphicsEffect(fx)
            btn.clicked.connect(lambda _=False, t=tab: self._side_drawer.toggle(t))
            return btn

        self._drawer_btn = _pill("⚙  Setup", "Setup — things you set once", "setup")
        lay.addWidget(self._drawer_btn)
        lay.addSpacing(4)
        self._ctrl_btn = _pill("🎛  Controls", "Controls — the everyday switches", "controls")
        lay.addWidget(self._ctrl_btn)
        lay.addStretch()

        # Title block is an overlay spanning the FULL header width, so it is
        # centred on the window no matter how wide the left/right clusters are.
        self._header_center = QWidget(w)
        self._header_center.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._header_center.setStyleSheet("background: transparent;")
        mid = QVBoxLayout(self._header_center)
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(1)
        mid.addStretch(1)
        _disp = self._assistant_name.upper()
        self._title_lbl = QLabel(_disp)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(_f(_FS_TITLE, True))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(self._title_lbl)
        _sub_text = ("A Friendly Assistant"
                     if _disp in ("JARVIS", "J.A.R.V.I.S")
                     else "Personal AI Assistant")
        self._sub_lbl = QLabel(_sub_text)
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setFont(_f(_FS_CAPTION))
        self._sub_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        mid.addWidget(self._sub_lbl)
        mid.addStretch(1)
        self._header_center.setGeometry(0, 0, w.width(), 54)
        self._header_center.raise_()

        def _header_resize(event, _w=w):
            QWidget.resizeEvent(_w, event)
            self._header_center.setGeometry(0, 0, _w.width(), 54)
            self._header_center.raise_()
        w.resizeEvent = _header_resize

        lay.addStretch()

        right_col = QVBoxLayout(); right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(_f(_FS_BODY + 6, True))
        self._clock_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(_f(_FS_CAPTION))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%I:%M:%S %p"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("glassLeft")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"""
            QWidget#glassLeft {{
                background: {_rgba(C.GLASS, 0.75)};
                border: 1px solid {_glass_border()};
                border-radius: 8px;
            }}
            QWidget#glassLeft QWidget {{ background: transparent; }}
        """)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 12, 10, 12)
        lay.setSpacing(8)

        lay.addWidget(_section_label("System Monitor"))
        lay.addSpacing(2)

        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("RAM", C.ACC2)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TMP", "#ff6688")

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        lay.addSpacing(4)

        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: rgba(255, 255, 255, 0.04); "
            f"border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(8, 8, 8, 8)
        ip_lay.setSpacing(5)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(_f(_FS_BODY, True))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(_f(_FS_BODY))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(_f(_FS_BODY))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addSpacing(4)

        lay.addStretch()

        for txt, col in [
            ("AI Core\nActive",   C.GREEN),
            ("Security\nCleared", C.TEXT),
            ("Protocol\n" + APP_PROTOCOL, C.TEXT_DIM),
        ]:
            lbl = QLabel(txt)
            lbl.setFont(_f(_FS_CAPTION, True))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color: {_txt(col)}; background: rgba(255, 255, 255, 0.04);"
                f"border: 1px solid {_rgba(C.PRI, 0.2)}; border-radius: 6px; padding: 4px;"
            )
            lay.addWidget(lbl)

        return w
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("glassRight")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"""
            QWidget#glassRight {{
                background: {_rgba(C.GLASS, 0.75)};
                border: 1px solid {_glass_border()};
                border-radius: 8px;
            }}
            QWidget#glassRight QWidget {{ background: transparent; }}
        """)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 12, 10, 12)
        lay.setSpacing(8)

        _sec = _section_label

        _hdr = QHBoxLayout(); _hdr.setContentsMargins(0, 0, 0, 0)
        _hdr.addWidget(_sec("Activity Log"))
        _hdr.addStretch(1)
        lay.addLayout(_hdr)

        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_rgba(C.PRI, 0.18)}; background: {_rgba(C.PRI, 0.18)}; "
                          f"max-height: 1px; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("File Upload"))
        self._drop_zone = FileDropZone()
        # Batch handoff — the zone expands dropped files/folders on a worker
        # thread and delivers the whole List[str] here. (file_selected still
        # fires for any legacy listener, but it is not wired to avoid double
        # processing the same drop.)
        self._drop_zone.files_selected.connect(self._on_files_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(_f(_FS_CAPTION))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {_rgba(C.PRI, 0.18)}; background: {_rgba(C.PRI, 0.18)}; "
                           f"max-height: 1px; margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("Command Input"))
        lay.addLayout(self._build_input_row())

        self._interrupt_btn = QPushButton("✋  Interrupt  [Esc]")
        self._interrupt_btn.setFixedHeight(36)
        self._interrupt_btn.setFont(_f(_FS_BODY, True))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(255, 51, 85, 0.20), stop:1 rgba(255, 51, 85, 0.07));
                color: #ff8a9b;
                border: 1px solid rgba(255, 51, 85, 0.38);
                border-radius: 6px;
                padding: 0 12px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(255, 51, 85, 0.36), stop:1 rgba(255, 51, 85, 0.16));
                border: 1px solid {C.MUTED_C};
                color: #ffffff;
            }}
            QPushButton:pressed {{
                background: rgba(255, 51, 85, 0.40);
            }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        self._mute_btn = QPushButton("🎙  Microphone Active")
        self._mute_btn.setFixedHeight(34)
        self._mute_btn.setFont(_f(_FS_BODY, True))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        return w

    def _build_side_drawer(self) -> SideDrawer:
        """The single sliding drawer: a Setup tab and a Controls tab."""
        dim = f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.04); color: {C.TEXT_MED};
                border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px;
                text-align: left; padding: 0 12px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; background: {_rgba(C.PRI, 0.08)};
                border-color: {_rgba(C.PRI, 0.4)}; }}
            QPushButton:pressed {{ background: {_rgba(C.PRI, 0.18)}; }}
        """
        pri = f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {_rgba(C.PRI, 0.22)}, stop:1 {_rgba(C.PRI, 0.08)});
                color: {C.TEXT}; border: 1px solid {_rgba(C.PRI, 0.5)};
                border-radius: 6px; text-align: left; padding: 0 12px;
            }}
            QPushButton:hover {{ border-color: {C.PRI}; background: {_rgba(C.PRI, 0.28)}; }}
            QPushButton:pressed {{ background: {_rgba(C.PRI, 0.38)}; }}
        """
        self._BTN_PRI, self._BTN_DIM = pri, dim

        def _page():
            w = QWidget(); w.setStyleSheet("background: transparent;")
            lay = QVBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(8)
            return w, lay

        def _row(lay, button, style=dim, handler=None, height=34):
            button.setFixedHeight(height)
            button.setFont(_f(_FS_BODY))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if style:
                button.setStyleSheet(style)
            if handler is not None:
                button.clicked.connect(handler)
            lay.addWidget(button)
            return button

        def _group(lay, title):
            lay.addSpacing(4)
            lay.addWidget(_section_label(title))

        # ── Setup ──
        setup, sl = _page()
        _row(sl, QPushButton("◉  Remote Control"), pri,
             lambda: self._launch_panel(self._open_remote), 38)
        _group(sl, "System")
        _row(sl, QPushButton("⊞  Create Desktop Shortcut"), dim, self._create_desktop_shortcut)
        self._autostart_btn = _row(sl, QPushButton("◉  Auto-start: Off"), None, self._toggle_autostart)
        _group(sl, "Assistant")
        _row(sl, QPushButton("⚙  Customise Assistant"), dim,
             lambda: self._launch_panel(self._open_customize))
        _row(sl, QPushButton("🎧  Audio Devices"), dim,
             lambda: self._launch_panel(self._open_audio_devices))
        _row(sl, QPushButton("🧠  Memory"), dim,
             lambda: self._launch_panel(self._open_memory_panel))
        _group(sl, "Access")
        _row(sl, QPushButton("🔑  API Configuration"), dim,
             lambda: self._launch_panel(self._open_api_config))
        _group(sl, "Plugins")
        _row(sl, QPushButton("🧩  Plugins"), dim,
             lambda: self._launch_panel(self._open_plugin_manager))
        _row(sl, QPushButton("⚙  Plugin Settings"), dim,
             lambda: self._launch_panel(self._open_plugin_settings))
        sl.addStretch(1)

        # ── Controls ──

        ctrl, cl = _page()
        _row(cl, QPushButton("⛶  Fullscreen  [F11]"), dim, self._toggle_fullscreen)
        self._brief_btn = _row(cl, QPushButton(), None, self._toggle_brief)
        _group(cl, "Voice")
        self._wake_btn = _row(cl, QPushButton(), None, self._toggle_wake_word)
        self._wake_sleep_btn = _row(cl, QPushButton(), None, self._tap_wake_manual)
        # Neutral until the real state lands from the background warm-up.
        self._wake_btn.setText("🎙  Wake Word")
        self._wake_btn.setStyleSheet(dim)
        self._wake_sleep_btn.hide()
        self._ptt_btn = _row(cl, QPushButton(), None, self._toggle_ptt)
        self._refresh_talk_btns()
        # Speaking over JARVIS to cut it off sits right under push-to-talk:
        # they are the two halves of how the mic behaves while JARVIS talks.
        self._barge_btn = _row(cl, QPushButton(), None, self._toggle_barge)
        self._refresh_barge_btn()
        cl.addStretch(1)

        d = SideDrawer(self.centralWidget())
        d.add_tab("setup", "⚙  Setup", setup, "Things you set once")
        d.add_tab("controls", "🎛  Controls", ctrl, "The everyday switches")
        d.set_tab("setup")
        return d

    # ── drawer navigation ────────────────────────────────────────────────
    _PANEL_ATTRS = ("_remote_overlay", "_customize_overlay", "_audio_overlay",
                        "_memory_overlay",
                        "_plugin_manager_overlay",
                        "_plugin_settings_overlay", "_api_overlay")
    
    def _dismiss_panels(self) -> None:
        """Close every detail panel so only one is ever on screen."""
        for attr in self._PANEL_ATTRS:
            ov = getattr(self, attr, None)
            if ov is None:
                continue
            try:
                if attr == "_remote_overlay":
                    ov._do_close()           # stops its timer, emits closed
                else:
                    ov.hide()
                    ov.deleteLater()
            except RuntimeError:             # C++ object already gone
                pass
            setattr(self, attr, None)

    def _any_panel_open(self) -> bool:
        for attr in self._PANEL_ATTRS:
            ov = getattr(self, attr, None)
            try:
                if ov is not None and ov.isVisible():
                    return True
            except RuntimeError:
                pass
        return False

    def _launch_panel(self, opener) -> None:
        """Drawer item -> detail panel: slide the drawer away, then open the
        panel (each opener dismisses whatever was open before it)."""
        self._side_drawer.slide_close()
        opener()

    def _on_drawer_state(self, tab: str) -> None:
        self._drawer_btn.setChecked(tab == "setup")
        self._ctrl_btn.setChecked(tab == "controls")
        for btn, on in ((self._drawer_btn, tab == "setup"),
                        (self._ctrl_btn, tab == "controls")):
            fx = btn.graphicsEffect()
            if fx is not None:
                fx.setEnabled(on)
        if tab == "controls":
            self._refresh_wake_btns()    # cheap: the state was warmed at boot

    def _place_drawer(self) -> None:
        if not hasattr(self, "_side_drawer"):
            return
        cw = self.centralWidget()
        top = 54                          # header height
        self._side_drawer.place(top, cw.height() - top - 24)   # footer is 24

    def _on_escape(self) -> None:
        """Esc closes the drawer, then an open panel, and only then interrupts."""
        if self._side_drawer.is_open():
            self._side_drawer.slide_close()
        elif self._any_panel_open():
            self._dismiss_panels()
        else:
            self._do_interrupt()

    def eventFilter(self, obj, ev):
        if (ev.type() == QEvent.Type.MouseButtonPress
                and getattr(self, "_side_drawer", None) is not None
                and self._side_drawer.is_open()):
            try:
                gp = ev.globalPosition().toPoint()
                d = self._side_drawer
                if not d.rect().contains(d.mapFromGlobal(gp)):
                    on_toggle = any(b.rect().contains(b.mapFromGlobal(gp))
                                    for b in (self._drawer_btn, self._ctrl_btn))
                    if not on_toggle:
                        d.slide_close()
            except Exception:
                pass
        return False

    def _warm_wake_state(self) -> None:
        """Work out the wake-word state off the UI thread, once.

        It used to be resolved when the drawer opened, and that made opening the
        drawer take two seconds the first time — measured at 2.10s, all of it
        `import openwakeword` dragging in onnxruntime behind it. The button
        count had nothing to do with it; the Qt thread was simply waiting on an
        import. Afterwards the same check costs about a millisecond, so it only
        ever needed to happen somewhere other than in front of the user.
        """
        def work():
            try:
                self._wake_state()
            except Exception:
                pass
            self._wake_btns_sig.emit()
        threading.Thread(target=work, daemon=True, name="wake-state-warm").start()

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(_f(_FS_BODY + 1))
        self._input.setFixedHeight(34)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(255, 255, 255, 0.05); color: {C.TEXT};
                border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px;
                padding: 6px 12px;
                selection-background-color: {C.PRI_DIM};
            }}
            QLineEdit:hover {{ border: 1px solid {_rgba(C.PRI, 0.30)}; }}
            QLineEdit:focus {{
                background: {_rgba(C.PRI, 0.06)};
                border: 1px solid {_rgba(C.PRI, 0.85)};
            }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(34, 34)
        send.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {_rgba(C.PRI, 0.22)}, stop:1 {_rgba(C.PRI, 0.08)});
                color: {C.TEXT};
                border: 1px solid {_rgba(C.PRI, 0.45)}; border-radius: 6px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {_rgba(C.PRI, 0.38)}, stop:1 {_rgba(C.PRI, 0.16)});
                border: 1px solid {C.PRI};
            }}
            QPushButton:pressed {{ background: {_rgba(C.PRI, 0.40)}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.TEXT}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Courier New", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont("Courier New", 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        # Reflow to the panel width instead of overflowing to the right.
        self._content_display.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._content_display.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content_display.setFont(QFont("Courier New", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    # ── center split sizing (shared by the HUD panels and the Study Board) ───
    # The Study Board is the only surface that matters in Study Mode, so a
    # bottom panel must never squeeze it: while Study Mode is on, panels are
    # capped at a fifth of the splitter, and any space freed by a closing panel
    # is handed straight back to the board. Without this, a document review or
    # a chat answer arriving at the last study step re-sized the splitter for
    # the normal HUD, leaving the board with a fraction of the window and the
    # rest blank underneath it.
    _STUDY_PANEL_FRACTION = 0.22

    def _panel_height(self, want: int) -> int:
        """Height to give a bottom panel; capped while Study Mode owns the screen."""
        if not self._study_on:
            return want
        total = max(1, self._center_split.height())
        return max(120, min(want, int(total * self._STUDY_PANEL_FRACTION)))

    def _refit_center_split(self) -> None:
        """Re-assert the board as the primary surface of the center splitter."""
        if not self._study_on:
            return
        total = max(120, self._center_split.height())
        content = self._panel_height(260) if self._content_panel.isVisible() else 0
        quiz = self._panel_height(250) if self._quiz_panel.isVisible() else 0
        self._center_split.setSizes([max(total - content - quiz, 120), content, quiz])

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if self._study_on:
            self._refit_center_split()
        elif first_show:
            total = self._center_split.height()
            panel = self._panel_height(220)
            self._center_split.setSizes([max(total - panel, 120), panel])

    # ── quiz panel ───────────────────────────────────────────────────────────
    # An interactive twin of the content panel. The plugin only ever hands over
    # questions; everything about asking, marking and reporting happens here,
    # and the finished result is pushed back into the conversation the same way
    # a dropped file is — as a message JARVIS reads and responds to. That keeps
    # the tool call short (it returns the moment the board is up) and leaves the
    # talking to the assistant, in the user's own language.

    def _quiz_btn(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFont(QFont("Courier New", 8))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(24)
        edge = C.BORDER_B if primary else C.BORDER
        col = C.PRI if primary else C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {_txt(col)};
                border: 1px solid {edge}; border-radius: 2px;
                padding: 3px 9px; text-align: left;
            }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        """)
        return b

    def _build_quiz_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("QuizPanel")
        w.setStyleSheet(f"""
            QWidget#QuizPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout(); hdr.setSpacing(6)
        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        hdr.addWidget(dot)

        self._quiz_title_lbl = QLabel("QUIZ")
        self._quiz_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._quiz_title_lbl.setStyleSheet(
            f"color: {C.TEXT}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(self._quiz_title_lbl)
        hdr.addStretch()

        self._quiz_count_lbl = QLabel("")
        self._quiz_count_lbl.setFont(QFont("Courier New", 7))
        self._quiz_count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._quiz_count_lbl)

        quit_btn = QPushButton("DISMISS  ✕")
        quit_btn.setFont(QFont("Courier New", 7))
        quit_btn.setFixedHeight(18)
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        quit_btn.clicked.connect(self._hide_quiz)
        hdr.addWidget(quit_btn)
        lay.addLayout(hdr)

        rule = QFrame(); rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {C.BORDER};")
        lay.addWidget(rule)

        self._quiz_q_lbl = QLabel("")
        self._quiz_q_lbl.setWordWrap(True)
        self._quiz_q_lbl.setFont(QFont("Courier New", 9))
        self._quiz_q_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        lay.addWidget(self._quiz_q_lbl)

        self._quiz_answers = QWidget()
        self._quiz_answers.setStyleSheet("background: transparent;")
        self._quiz_answers_lay = QVBoxLayout(self._quiz_answers)
        self._quiz_answers_lay.setContentsMargins(0, 2, 0, 0)
        self._quiz_answers_lay.setSpacing(4)
        lay.addWidget(self._quiz_answers)

        self._quiz_note_lbl = QLabel("")
        self._quiz_note_lbl.setWordWrap(True)
        self._quiz_note_lbl.setFont(QFont("Courier New", 8))
        self._quiz_note_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._quiz_note_lbl.hide()
        lay.addWidget(self._quiz_note_lbl)

        foot = QHBoxLayout()
        foot.addStretch()
        self._quiz_next_btn = self._quiz_btn("NEXT  →", primary=True)
        self._quiz_next_btn.setFixedWidth(110)
        self._quiz_next_btn.clicked.connect(self._quiz_next)
        self._quiz_next_btn.hide()
        foot.addWidget(self._quiz_next_btn)
        lay.addLayout(foot)

        self._quiz = None
        return w

    def _show_quiz(self, topic: str, questions, grader=None):
        """Slot — Qt main thread. Puts a fresh quiz on the board."""
        if not questions:
            return
        self._quiz = {
            "topic": topic or "",
            "questions": list(questions),
            "grader": grader,
            "i": 0,
            "results": [],
            "answered": False,
        }
        self._quiz_title_lbl.setText((topic or "quiz").upper()[:48])
        first_show = not self._quiz_panel.isVisible()
        self._quiz_panel.show()
        if self._study_on:
            self._refit_center_split()
        elif first_show:
            total = self._center_split.height()
            panel = self._panel_height(250)
            self._center_split.setSizes([max(total - panel, 120), 0, panel])
        self._quiz_render()

    def _hide_quiz(self):
        self._quiz = None
        self._quiz_panel.hide()
        self._refit_center_split()

    def _quiz_clear_answers(self):
        while self._quiz_answers_lay.count():
            item = self._quiz_answers_lay.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()

    def _quiz_render(self):
        q = self._quiz["questions"][self._quiz["i"]]
        n, total = self._quiz["i"] + 1, len(self._quiz["questions"])
        self._quiz_count_lbl.setText(f"{n} / {total}")
        self._quiz_q_lbl.setText(q.get("question", ""))
        self._quiz_note_lbl.hide()
        self._quiz_next_btn.hide()
        self._quiz["answered"] = False
        self._quiz_clear_answers()

        opts = q.get("options") or []
        if opts:
            for text in opts:
                b = self._quiz_btn("   " + text)
                b.clicked.connect(lambda _=False, t=text: self._quiz_submit(t))
                self._quiz_answers_lay.addWidget(b)
        else:
            row = QWidget(); row.setStyleSheet("background: transparent;")
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            field = QLineEdit()
            field.setFont(QFont("Courier New", 9))
            field.setPlaceholderText("your answer")
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {C.PANEL2}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 2px; padding: 4px 7px;
                }}
                QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}
            """)
            send = self._quiz_btn("ANSWER", primary=True)
            send.setFixedWidth(90)
            field.returnPressed.connect(lambda: self._quiz_submit(field.text()))
            send.clicked.connect(lambda: self._quiz_submit(field.text()))
            h.addWidget(field, stretch=1)
            h.addWidget(send)
            self._quiz_answers_lay.addWidget(row)
            field.setFocus()

    def _quiz_submit(self, given: str):
        if self._quiz is None or self._quiz["answered"]:
            return
        self._quiz["answered"] = True
        q = self._quiz["questions"][self._quiz["i"]]
        grader = self._quiz.get("grader")
        verdict = None
        if callable(grader):
            try:
                verdict = grader(q, given)
            except Exception:
                verdict = None
        self._quiz["results"].append({
            "question": q.get("question", ""),
            "type": q.get("type", ""),
            "given": str(given or "").strip(),
            "answer": q.get("answer", ""),
            "correct": verdict,
        })

        for i in range(self._quiz_answers_lay.count()):
            wdg = self._quiz_answers_lay.itemAt(i).widget()
            if wdg is not None:
                wdg.setEnabled(False)

        if verdict is True:
            mark, colour = "✓  correct", C.GREEN
        elif verdict is False:
            mark, colour = "✕  " + str(q.get("answer", "")), C.RED
        else:
            # Open answers and near-miss gap-fills are JARVIS's to judge. Saying
            # so is honest; marking it wrong here would be a guess.
            mark, colour = "…  noted — I'll go over this one with you", C.ACC2
        note = q.get("note") or ""
        self._quiz_note_lbl.setText(mark + (("\n" + note) if note else ""))
        self._quiz_note_lbl.setStyleSheet(f"color: {colour}; background: transparent;")
        self._quiz_note_lbl.show()

        last = self._quiz["i"] >= len(self._quiz["questions"]) - 1
        self._quiz_next_btn.setText("FINISH  →" if last else "NEXT  →")
        self._quiz_next_btn.show()
        self._quiz_next_btn.setFocus()

    def _quiz_next(self):
        if self._quiz is None:
            return
        if self._quiz["i"] >= len(self._quiz["questions"]) - 1:
            self._quiz_finish()
        else:
            self._quiz["i"] += 1
            self._quiz_render()

    def _quiz_finish(self):
        if self._quiz is None:
            return
        topic = self._quiz["topic"]
        results = self._quiz["results"]
        right = sum(1 for r in results if r["correct"] is True)
        unsure = sum(1 for r in results if r["correct"] is None)
        total = len(results)
        self._quiz_panel.hide()
        self._quiz = None
        self._refit_center_split()

        self._log.append_log(f"QUIZ: {topic or 'quiz'} — {right}/{total} correct")

        # Hand it back to JARVIS as a message, not as a tool return: the tool
        # call ended minutes ago. This is the same channel a dropped file uses.
        lines = [f"[QUIZ_DONE] topic={topic or 'general'} | "
                 f"auto-marked {right}/{total} correct"
                 + (f", {unsure} still need your marking" if unsure else "")]
        for i, r in enumerate(results, 1):
            state = ("correct" if r["correct"] is True
                     else "wrong" if r["correct"] is False else "NEEDS MARKING")
            lines.append(
                f"{i}. [{r['type']}] {r['question']} | they answered: "
                f"{r['given'] or '(blank)'} | expected: {r['answer']} | {state}")
        lines.append(
            "Mark every question flagged NEEDS MARKING yourself — accept an answer "
            "that means the same thing. Then tell them how they did in their own "
            "language: the score, what they got wrong and why, in a couple of "
            "sentences. Offer another round only if it fits. "
            "Remember something only if it would still matter next week — that they "
            "are working through a subject, or keep missing the same thing. A score "
            "from one session is not worth a memory, and a memory per quiz would "
            "bury the things that are.")
        msg = "\n".join(lines)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setObjectName("glassFooter")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setFixedHeight(24)
        w.setStyleSheet(f"""
            QWidget#glassFooter {{
                background: {_rgba(C.GLASS, 0.80)};
                border-top: 1px solid {_glass_border()};
            }}
            QWidget#glassFooter QLabel {{ background: transparent; border: none; }}
        """)
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(_f(_FS_CAPTION))
            l.setStyleSheet(f"color: {_txt(color)}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F8] Study Mode  ·  [Ctrl+H] Hide Chat  ·  [Ctrl+N] Notepad  ·  [F11] Fullscreen"))
        lay.addStretch()
        return w

    def _on_files_selected(self, paths: list[str]):
        """Batch intake for the drop zone and the browse dialog. Qt thread only.

        Only bookkeeping happens here: hint text, log lines, and dispatching the
        real extraction to a worker thread. A drop of N files, or a folder
        expanded to its documents, arrives as one List[str].
        """
        paths = [str(p) for p in (paths or []) if p]
        if not paths:
            return
        self._current_file  = paths[0]
        self._current_files = list(paths)

        # Publish the batch to the file reader tool — the tool verifies every
        # file through core.security_policy on read, so nothing is registered.

        first = Path(paths[0])

        def _size(p: Path) -> str:
            try:
                return _fmt_size(p.stat().st_size)
            except OSError:
                return "—"

        if len(paths) == 1:
            icon, _ = _FILE_ICONS.get(_file_category(first), _FILE_ICONS["unknown"])
            size = _size(first)
            self._file_hint.setText(
                f"{icon}  {first.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
            self._log.append_log(f"FILE: {first.name} ({size}) loaded")
        else:
            self._file_hint.setText(
                f"📎  {len(paths)} files loaded  ·  "
                f"Tell {self._assistant_name} what to do with them")
            self._log.append_log(f"FILES: {len(paths)} files loaded")

        # Phase 5 — in Study Mode the extractable documents are read locally
        # (PyMuPDF / python-docx) and the clean text is handed to the Live
        # session. Extraction runs on a worker thread, so neither a big PDF nor
        # a folder of them can block the Qt event loop.
        docs  = [f for f in paths if Path(f).suffix.lower() in _STUDY_DOC_EXTS]
        study = study_guard.study_active()
        study_handled = False

        if study and docs and self.on_study_documents is not None:
            self._log.append_log(f"STUDY: extracting {len(docs)} document(s) locally …")
            threading.Thread(target=self.on_study_documents,
                             args=(docs,), daemon=True).start()
            study_handled = True
        elif study and docs and self.on_study_document is not None:
            # Fallback when only the per-file hook is wired by the host.
            self._log.append_log(f"STUDY: extracting {docs[0]} locally …")
            threading.Thread(target=self.on_study_document,
                             args=(docs[0],), daemon=True).start()
            study_handled = True

        # The generic "file uploaded" nudge is skipped when the study path
        # already sent a prompt, so the Live session is not told twice.
        if self.on_text_command and not study_handled:
            if len(paths) == 1:
                size = _size(first)
                msg = (
                    f"[FILE_UPLOADED] path={first} | name={first.name} | "
                    f"type={first.suffix.lstrip('.')} | size={size} | "
                    f"Briefly tell the user you can see the file '{first.name}' "
                    f"({size}) has been uploaded and ask what they'd like to do with it."
                )
            else:
                listing = "\n".join(f"- {f}" for f in paths[:_MAX_DROP_FILES])
                msg = (
                    f"[FILES_UPLOADED] count={len(paths)}\n{listing}\n"
                    f"Briefly tell the user you can see {len(paths)} files have been "
                    f"uploaded and ask what they'd like to do with them."
                )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        self._dismiss_panels()
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "JARVIS_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "JARVIS_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.jarvis.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("◉  Auto-start: On")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 6px; text-align: left; padding: 0 12px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  Auto-start: Off")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 6px; text-align: left; padding: 0 12px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    # ── Wake word settings ───────────────────────────────────────────────────

    def _wake_state(self) -> dict:
        """Combined state for the two wake-word buttons. Readiness is a cheap,
        deterministic on-disk check now (see core.wake_word.is_ready), so there
        is nothing to cache — the button never flickers to a stale value."""
        if self.wake_get_state:
            try:
                s = self.wake_get_state()
                return {"ready": bool(s.get("ready")),
                        "enabled": bool(s.get("enabled")),
                        "awake": bool(s.get("awake"))}
            except Exception:
                pass
        # Before JarvisLive has wired its callback (drawer built at startup).
        ready, enabled = False, False
        try:
            from core.wake_word import is_ready
            from memory.config_manager import get_wake_word_enabled
            ready, enabled = is_ready(), get_wake_word_enabled()
        except Exception:
            pass
        return {"ready": ready, "enabled": enabled, "awake": True}

    def _refresh_wake_btns(self):
        if not hasattr(self, '_wake_btn'):
            return
        st = self._wake_state()
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 6px;
                text-align: left; padding: 0 12px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 6px;
                text-align: left; padding: 0 12px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""
        self._wake_btn.setEnabled(True)
        if not st["ready"]:
            self._wake_btn.setText("⬇  Wake Word: Download")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()
        elif st["enabled"]:
            self._wake_btn.setText("🎙  Wake Word: On")
            self._wake_btn.setStyleSheet(_on)
            self._wake_sleep_btn.show()
            self._wake_sleep_btn.setText("😴  Sleep Now" if st["awake"] else "👂  Wake Now")
            self._wake_sleep_btn.setStyleSheet(_off)
        else:
            self._wake_btn.setText("🎙  Wake Word: Off")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()

    def _refresh_talk_btns(self):
        """Repaint the push-to-talk row from the saved setting."""
        if not hasattr(self, "_ptt_btn"):
            return
        from core.hotkey import chord_label
        from memory.config_manager import get_push_to_talk_enabled
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 6px;
                text-align: left; padding: 0 12px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 6px;
                text-align: left; padding: 0 12px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""

        ptt = get_push_to_talk_enabled()
        self._ptt_btn.setText(f"🎚  Push-to-talk: {chord_label()}" if ptt
                              else "🎚  Push-to-talk: Off")
        self._ptt_btn.setStyleSheet(_on if ptt else _off)
        self._ptt_btn.setToolTip(
            "Microphone stays closed until you hold the key — nothing is sent "
            "while you are not holding it." if ptt
            else "Hold a key to talk instead of streaming the mic continuously.")


    def _refresh_barge_btn(self):
        """Repaint the voice-interrupt row.

        The switch state comes from the saved config, not from JarvisLive: this
        row is built before the audio engine exists, so a callback that is not
        wired yet would show OFF on a machine where it is switched on. The saved
        config is authoritative for on/off either way — JarvisLive writes it
        whenever the toggle changes.

        The calibration hint is the opposite: only the live detector knows how
        this room sounds, so that part is added when the callback is available
        and simply omitted when it is not.
        """
        if not hasattr(self, "_barge_btn"):
            return
        from memory.config_manager import (get_barge_in_enabled,
                                           get_barge_in_sensitivity)
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 6px;
                text-align: left; padding: 0 12px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 6px;
                text-align: left; padding: 0 12px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""

        enabled = get_barge_in_enabled()
        sens    = get_barge_in_sensitivity()

        if not enabled:
            self._barge_btn.setText("✋  Voice Interrupt: Off")
            self._barge_btn.setStyleSheet(_off)
            self._barge_btn.setToolTip(
                "Let speaking over JARVIS stop it mid-sentence. Off by default "
                "because how well it works depends on your room.")
            return

        self._barge_btn.setText(f"✋  Voice Interrupt: {sens.title()}")
        self._barge_btn.setStyleSheet(_on)

        # Ask the live detector how the room is doing, if it is running.
        st = {}
        if self.get_barge_state:
            try:
                st = self.get_barge_state() or {}
            except Exception:
                st = {}
        if not st.get("calibrated"):
            _note = ("Still measuring this room's echo — interruptions begin "
                     "working after JARVIS has spoken a sentence or two.")
        elif st.get("reliable", True):
            _note = ("Speak over JARVIS and it stops. This room has been "
                     "measured, so it can tell your voice from its own echo.")
        else:
            _note = ("This room returns a lot of echo, so it waits for clearly "
                     "sustained speech. A headset mic would make it much more "
                     "responsive.")
        self._barge_btn.setToolTip(
            f"Speaking over JARVIS cuts it off mid-sentence. "
            f"Sensitivity: {sens} — tap to cycle. {_note}")

    def _toggle_barge(self):
        """Off → medium → low → high → off.

        A four-state cycle on one button rather than a switch plus a slider:
        sensitivity is the only knob, and it is the kind of thing people try once
        and then leave alone. Cycling keeps it to a single click.
        """
        from memory.config_manager import (get_barge_in_enabled,
                                           get_barge_in_sensitivity)
        order = ("medium", "low", "high")

        if not get_barge_in_enabled():
            want, sens = True, get_barge_in_sensitivity()
        else:
            cur = get_barge_in_sensitivity()
            try:
                nxt = order[order.index(cur) + 1]
            except (ValueError, IndexError):
                nxt = None       # 'high' is the end of the cycle
            want, sens = (False, cur) if nxt is None else (True, nxt)

        if self.on_barge_toggle:
            try:
                self.on_barge_toggle(want, sens)
            except TypeError:
                # A handler that only takes the flag — stay compatible.
                self.on_barge_toggle(want)
            except Exception as e:
                self._log.append_log(f"ERR: Voice interrupt failed — {e}")

        self._refresh_barge_btn()



    def _toggle_ptt(self):
        from memory.config_manager import (get_push_to_talk_enabled,
                                           save_push_to_talk_enabled)
        want = not get_push_to_talk_enabled()
        save_push_to_talk_enabled(want)
        scope = None
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(want)
            except Exception as e:
                self._log.append_log(f"ERR: Push-to-talk failed — {e}")
                save_push_to_talk_enabled(False)
                want = False
        self._apply_ptt_shortcut(want and scope != "global")
        self._refresh_talk_btns()

    def _apply_ptt_shortcut(self, needed: bool):
        """Bind the chord inside the window when no global hook is available.

        On macOS and Linux there is no dependency-free way to read global key
        state, so the chord is at least live whenever this window has focus.
        Qt gives no key-release for a QShortcut, so a press latches the mic open
        and a short timer closes it; held down, auto-repeat keeps pushing that
        timer out, which behaves like holding a key.
        """
        from PyQt6.QtGui import QKeySequence, QShortcut
        from core.hotkey import qt_sequence

        if not needed:
            sc = getattr(self, "_ptt_sc", None)
            if sc is not None:
                sc.setEnabled(False)
                self._ptt_sc = None
            self._ptt_hold(False)
            return
        if getattr(self, "_ptt_sc", None) is not None:
            return

        self._ptt_release = QTimer(self)
        self._ptt_release.setSingleShot(True)
        self._ptt_release.setInterval(420)
        self._ptt_release.timeout.connect(lambda: self._ptt_hold(False))

        def _press():
            self._ptt_hold(True)
            self._ptt_release.start()

        self._ptt_sc = QShortcut(QKeySequence(qt_sequence()), self)
        self._ptt_sc.setAutoRepeat(True)
        self._ptt_sc.activated.connect(_press)

    def _ptt_hold(self, held: bool):
        """Report a windowed press/release to whoever owns the microphone."""
        cb = getattr(self, "ptt_hold", None)
        if cb:
            try:
                cb(bool(held))
            except Exception:
                pass

    def _toggle_wake_word(self):
        st = self._wake_state()
        if not st["ready"]:
            # First time: download openwakeword + model in a worker thread.
            self._wake_btn.setText("⬇  DOWNLOADING… (one-time)")
            self._wake_btn.setEnabled(False)
            def _work():
                try:
                    from core.wake_word import install_and_download
                    ok, msg = install_and_download(
                        logger=lambda m: self._log_sig.emit(f"SYS: {m}"))
                except Exception as e:
                    ok, msg = False, str(e)
                if ok and self.on_wake_toggle:
                    try:
                        self.on_wake_toggle(True)   # auto-enable after a successful download
                    except Exception:
                        pass
                self._wake_dl_sig.emit(ok, msg)
            threading.Thread(target=_work, daemon=True).start()
            return
        # Already downloaded → just flip enabled/disabled through JarvisLive.
        if self.on_wake_toggle:
            try:
                self.on_wake_toggle(not st["enabled"])
            except Exception:
                pass
        self._refresh_wake_btns()

    def _on_wake_install_done(self, ok: bool, msg: str):
        self._log_sig.emit(f"SYS: {'Wake word ready.' if ok else 'Wake word setup failed: ' + msg}")
        self._refresh_wake_btns()

    def _tap_wake_manual(self):
        if self.on_wake_manual:
            try:
                self.on_wake_manual()
            except Exception:
                pass
        self._refresh_wake_btns()

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("☀  Morning Brief: On")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 6px;
                    text-align: left; padding: 0 12px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("☀  Morning Brief: Off")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 6px;
                    text-align: left; padding: 0 12px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        self._dismiss_panels()
        cfg = _read_full_config()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "JARVIS") or "JARVIS",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        """Live preview — paints the whole interface the new colour (does NOT write to config)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "",
                           voice: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — {APP_VERSION}")
        self._title_lbl.setText(display)
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            from memory.config_manager import get_voice, save_voice
            if voice != get_voice():
                save_voice(voice)
                voice_changed = True

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.append_log(f"SYS: Voice set — {voice}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        self._dismiss_panels()
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        self._dismiss_panels()
        ov = MemoryOverlay(parent=self.centralWidget())
        self._centre_overlay(ov)
        self._memory_overlay = ov


    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str):
        self._hide_confirm_banner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def _on_confirm_answered(self, accepted: bool):
        # Tear the banner down first: core.confirm.resolve() may be about to
        # shut the machine down, and a live widget mid-callback is not where you
        # want to be when that happens.
        self._hide_confirm_banner()
        try:
            from core.confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.append_log(f"ERR: Confirmation failed — {e}")

    def _open_plugin_manager(self):
        self._dismiss_panels()
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    def _open_plugin_settings(self):
        self._dismiss_panels()
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov   # keep a reference so it isn't GC'd

    def _open_api_config(self):
        self._dismiss_panels()
        ov = ApiConfigOverlay(parent=self.centralWidget())
        ov.saved.connect(self._on_api_config_saved)
        ov.closed.connect(self._dismiss_panels)   # tidy up on CANCEL / ✕ / after save
        self._centre_overlay(ov)
        self._api_overlay = ov                    # keep a reference so it isn't GC'd

    def _on_api_config_saved(self):
        self._log.append_log("SYS: API configuration saved. Keys reloaded.")

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        if time.time() < _CLIPBOARD_QUIET_UNTIL:
            return
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        self._set_muted(not self._muted)

    def _set_muted(self, muted: bool, note: str = ""):
        muted = bool(muted)
        if muted == self._muted:
            return
        self._muted = muted
        self.hud.muted = muted
        self._style_mute_btn()
        if muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted." + (f" {note}" if note else ""))
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active." + (f" {note}" if note else ""))

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("🔇  Microphone Muted")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 rgba(255, 51, 85, 0.18), stop:1 rgba(255, 51, 85, 0.06));
                    color: #ff8a9b;
                    border: 1px solid rgba(255, 51, 85, 0.4);
                    border-radius: 6px;
                    padding: 0 12px;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 rgba(255, 51, 85, 0.30), stop:1 rgba(255, 51, 85, 0.14));
                    border: 1px solid {C.MUTED_C};
                }}
            """)
        else:
            self._mute_btn.setText("🎙  Microphone Active")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 rgba(0, 255, 136, 0.17), stop:1 rgba(0, 255, 136, 0.05));
                    color: {C.GREEN};
                    border: 1px solid rgba(0, 255, 136, 0.35);
                    border-radius: 6px;
                    padding: 0 12px;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 rgba(0, 255, 136, 0.30), stop:1 rgba(0, 255, 136, 0.13));
                    border: 1px solid {C.GREEN};
                }}
            """)
       
    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            has_key = bool(d.get("gemini_api_key")) or bool(d.get("gemini_api_keys"))
            return has_key and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        self._dismiss_panels()
        if getattr(self, '_overlay', None) is not None:
            try:
                self._overlay.hide(); self._overlay.deleteLater()
            except RuntimeError:
                pass
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # Read-modify-write. This used to replace the whole file with just the
        # key and the OS, which silently erased the assistant's name, colours,
        # the detected camera, plugin settings — and now the backup keys.
        data = _read_full_config()
        entered = key.replace(",", " ").split()
        old: list[str] = []
        for v in (data.get("gemini_api_key"), data.get("gemini_api_keys")):
            if isinstance(v, (list, tuple)):
                old.extend(str(x) for x in v)
            elif v:
                old.append(str(v))
        old = [k for item in old for k in item.replace(",", " ").split()]
        # What was just typed goes to the front of the ladder; earlier keys
        # follow, so a good key is never lost. (A rejected one is simply tried
        # once and set aside again.)
        ladder: list[str] = []
        for k in entered + old:
            if k and k not in ladder:
                ladder.append(k)
        data["gemini_api_key"] = ladder[0]
        if len(ladder) > 1:
            data["gemini_api_keys"] = ladder[1:]
        else:
            data.pop("gemini_api_keys", None)
        data["os_system"] = os_name
        API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        try:                       # the key list is cached; make it see the new keys now
            from core import gemini as _g
            _g.api_keys(refresh=True)
        except Exception:
            pass
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        _apply_app_theme(self._app)
        self._win = MainWindow(face_path)
        self.root = _RootShim(self._app)
        # Phase 6 — every board artefact shown this session, in order, so the
        # export action has real content to compile (not just the notepad).
        self._study_history: list[dict] = []
        self._study_history_lock = _th.Lock()
        _attach_study_security_hud(self)

        try:
            from core.activity_hud import install as _install_activity_hud
            _install_activity_hud(self)
        except Exception as _e:
            print(f"[ActivityHUD] not installed: {_e}")
        self._win.show()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @current_file.setter
    def current_file(self, path: str | None) -> None:
        """Single-file handoff (phone uploads, legacy callers). Updates the
        drop-zone state QUIETLY — callers that own the notification have already
        sent it, so this must not fire a duplicate drop handoff."""
        if path:
            self._win._drop_zone.set_file_quiet(str(path))

    @property
    def current_files(self) -> list[str]:
        """Every path currently loaded — the batch view used by the Live session."""
        return self._win._drop_zone.current_files()

    @current_files.setter
    def current_files(self, paths) -> None:
        self._win._drop_zone._set_files([str(p) for p in (paths or []) if p])

    @property
    def on_study_document(self):
        return self._win.on_study_document

    @on_study_document.setter
    def on_study_document(self, cb):
        self._win.on_study_document = cb

    @property
    def on_study_documents(self):
        return self._win.on_study_documents

    @on_study_documents.setter
    def on_study_documents(self, cb):
        self._win.on_study_documents = cb

    @property
    def request_say(self):
        return self._win.request_say

    @request_say.setter
    def request_say(self, cb):
        self._win.request_say = cb

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    def show_confirm(self, title: str, detail: str) -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300])

    def hide_confirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    @property
    def get_plugin_settings(self):
        return self._win.get_plugin_settings

    @get_plugin_settings.setter
    def get_plugin_settings(self, cb):
        self._win.get_plugin_settings = cb

    @property
    def on_wake_toggle(self):
        return self._win.on_wake_toggle

    @on_wake_toggle.setter
    def on_wake_toggle(self, cb):
        self._win.on_wake_toggle = cb

    @property
    def on_wake_manual(self):
        return self._win.on_wake_manual

    @on_wake_manual.setter
    def on_wake_manual(self, cb):
        self._win.on_wake_manual = cb

    @property
    def wake_get_state(self):
        return self._win.wake_get_state

    @wake_get_state.setter
    def wake_get_state(self, cb):
        self._win.wake_get_state = cb

    def set_audio_level(self, level: float) -> None:
        """Thread-safe: feed a 0.0–1.0 live audio level to the HUD waveform.
        Called from the audio threads; a plain float store is atomic under the
        GIL, so no signal/lock is needed for this cosmetic value."""
        try:
            self._win.hud.set_audio_level(level)
        except Exception:
            pass

    @property
    def ptt_hold(self):
        return self._win.ptt_hold

    @ptt_hold.setter
    def ptt_hold(self, cb):
        self._win.ptt_hold = cb

    @property
    def on_push_to_talk(self):
        return self._win.on_push_to_talk

    @on_push_to_talk.setter
    def on_push_to_talk(self, cb):
        self._win.on_push_to_talk = cb

    @property
    def on_barge_toggle(self):
        return self._win.on_barge_toggle

    @on_barge_toggle.setter
    def on_barge_toggle(self, cb):
        self._win.on_barge_toggle = cb

    @property
    def get_barge_state(self):
        return self._win.get_barge_state

    @get_barge_state.setter
    def get_barge_state(self, cb):
        self._win.get_barge_state = cb

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def show_quiz(self, topic: str, questions, grade=None) -> None:
        """Thread-safe: put an interactive quiz on the board.

        `grade(question, given)` decides each answer — the plugin supplies it so
        the marking rules live with the questions rather than being duplicated
        here. Returning None from it means "JARVIS should judge this one", which
        is how open answers and near-miss gap-fills are handled.

        Returns immediately: the user answers at their own pace and the finished
        result is delivered back through on_text_command.
        """
        self._win._quiz_sig.emit(str(topic or ""), list(questions or []), grade)

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    # ── Study Station (core/study_canvas.py, plugins/study_mentor.py) ────────

    @property
    def study_mode_active(self) -> bool:
        return study_guard.study_active()

    def set_study_mode(self, on: bool, notify_voice: bool = False) -> None:
        """Thread-safe. The webcam lock is taken HERE, synchronously, so there is
        no gap between "mode on" and "camera refused"; the layout swap follows
        on the Qt thread."""
        if on and not HAVE_STUDY:
            return
        study_guard.set_study_active(bool(on))
        self._win._study_mode_sig.emit(bool(on))

    def _study_post(self, op: str, **payload) -> None:
        self._win._study_board_sig.emit(op, json.dumps(payload, ensure_ascii=False))

    def _record_study(self, kind: str, title: str, subject: str, text: str) -> None:
        """Append a rendered board artefact to the session history (Phase 6)."""
        text = (text or "").strip()
        if not text:
            return
        try:
            with self._study_history_lock:
                self._study_history.append({
                    "kind": kind, "title": title or kind.title(),
                    "subject": subject or "", "text": text,
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
        except Exception:
            pass

    @staticmethod
    def _render_flashcards(cards: list) -> str:
        out = []
        for i, c in enumerate(cards or [], 1):
            if isinstance(c, dict):
                q = c.get("front") or c.get("question") or c.get("q") or c.get("term") or ""
                a = c.get("back") or c.get("answer") or c.get("a") or c.get("definition") or ""
            elif isinstance(c, (list, tuple)) and len(c) >= 2:
                q, a = c[0], c[1]
            else:
                q, a = str(c), ""
            out.append(f"{i}. **{str(q).strip()}** — {str(a).strip()}")
        return "\n".join(out)

    @staticmethod
    def _render_quiz(questions: list) -> str:
        out = []
        for i, q in enumerate(questions or [], 1):
            if isinstance(q, dict):
                stem = q.get("question") or q.get("q") or q.get("prompt") or ""
                opts = q.get("options") or q.get("choices") or []
                ans = q.get("answer") or q.get("correct") or q.get("correct_answer") or ""
            else:
                stem, opts, ans = str(q), [], ""
            line = f"**Q{i}.** {str(stem).strip()}"
            if opts:
                line += "\n" + "\n".join(
                    f"   - {chr(65 + j)}. {str(o).strip()}" for j, o in enumerate(opts))
            if ans not in ("", None):
                line += f"\n   - _Answer: {str(ans).strip()}_"
            out.append(line)
        return "\n".join(out)

    @classmethod
    def _render_mindmap(cls, tree: dict, depth: int = 0) -> str:
        if not isinstance(tree, dict):
            return (("  " * depth) + f"- {tree}") if tree else ""
        label = tree.get("name") or tree.get("title") or tree.get("label") or "Topic"
        lines = [("  " * depth) + f"- {str(label).strip()}"]
        children = tree.get("children") or tree.get("nodes") or []
        for child in children:
            sub = cls._render_mindmap(child, depth + 1) if isinstance(child, dict) else \
                (("  " * (depth + 1)) + f"- {child}")
            if sub:
                lines.append(sub)
        return "\n".join(lines)

    def get_study_history(self) -> list[dict]:
        """Session board artefacts (solutions, quizzes, cards, mindmaps)."""
        try:
            with self._study_history_lock:
                return list(self._study_history)
        except Exception:
            return []

    def study_solution(self, title: str, markdown: str, subject: str = "",
                       reveal: str = "steps") -> None:
        self._study_post("solution", title=title, markdown=markdown,
                         subject=subject, reveal=reveal)
        self._record_study("solution", title, subject, markdown)

    def study_quiz(self, title: str, questions: list, subject: str = "") -> None:
        questions = questions or []
        self._study_post("quiz", title=title, questions=questions, subject=subject)
        self._record_study("quiz", title, subject, self._render_quiz(questions))

    def study_flashcards(self, title: str, cards: list, subject: str = "") -> None:
        cards = cards or []
        self._study_post("flashcards", title=title, cards=cards, subject=subject)
        self._record_study("flashcards", title, subject, self._render_flashcards(cards))

    def study_mindmap(self, title: str, tree: dict, subject: str = "") -> None:
        tree = tree or {}
        self._study_post("mindmap", title=title, tree=tree, subject=subject)
        self._record_study("mindmap", title, subject, self._render_mindmap(tree))

    def study_status(self, text: str) -> None:
        self._study_post("status", text=text)

    def study_next(self) -> None:
        self._study_post("next")

    def study_prev(self) -> None:
        self._study_post("prev")

    def study_restore(self, target: str = "") -> None:
        self._study_post("restore", target=target)

    def study_clear(self) -> None:
        self._study_post("clear")

    def get_notepad_text(self) -> str:
        return self._win.get_notepad_text()

    def set_notepad_text(self, text: str) -> None:
        self._win.set_notepad_text(text)

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

# ============================================================================
# [SECURE STUDY EDITION] RESPONSIVE HEADER BUTTONS (LEFT OF THE HEADER) & F9
# ============================================================================
def _resolve_qwidget(ui_obj):
    from PyQt6.QtWidgets import QWidget, QApplication
    if isinstance(ui_obj, QWidget):
        return ui_obj
    for attr in ("win", "window", "_win", "_window", "main_window", "root", "widget", "hud"):
        candidate = getattr(ui_obj, attr, None)
        if isinstance(candidate, QWidget):
            return candidate
    for val in getattr(ui_obj, "__dict__", {}).values():
        if isinstance(val, QWidget) and val.parentWidget() is None:
            return val
    for val in getattr(ui_obj, "__dict__", {}).values():
        if isinstance(val, QWidget):
            return val
    app = QApplication.instance()
    if app:
        for w in app.topLevelWidgets():
            if isinstance(w, QWidget) and w.width() >= 400:
                return w
    return None


def _find_button_by_text(root_widget, keyword: str):
    from PyQt6.QtWidgets import QPushButton
    kw = keyword.lower()
    for btn in root_widget.findChildren(QPushButton):
        txt = (btn.text() or "").lower()
        if kw in txt:
            return btn
    return None


def _attach_study_security_hud(window, _retry_count=0):
    try:
        from PyQt6.QtWidgets import (
            QPushButton, QFileDialog, QMessageBox, QWidget, QHBoxLayout, QSizePolicy
        )
        from PyQt6.QtGui import QShortcut, QKeySequence
        from PyQt6.QtCore import Qt, QTimer, QObject, QEvent, QPoint
        from core.security_policy import (
            issue_manual_screen_token,
            capture_screen_manual_only,
            approve_file_permanently,
            approve_folder_permanently,
        )
    except Exception as e:
        print(f"[STUDY-HUD] Failed to load HUD security extensions: {e}")
        return

    parent_widget = _resolve_qwidget(window)
    if parent_widget is None:
        if _retry_count < 20:
            try:
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(250, lambda: _attach_study_security_hud(window, _retry_count + 1))
            except Exception:
                pass
        return

    if getattr(parent_widget, "_study_hud_attached", False):
        return
    parent_widget._study_hud_attached = True

    def _trigger_manual_screen_capture():
        try:
            token = issue_manual_screen_token(ttl_seconds=10.0)
            shot_path = capture_screen_manual_only(token)
            print(f"[STUDY-HUD] 📸 Screen captured: {shot_path}")

            # Hand the image to the live session. JarvisLive injects it as a
            # multimodal part and writes the acknowledgement to the log.
            callback = None
            for target in (window, parent_widget):
                cb = getattr(target, "on_manual_screen_captured", None)
                if callable(cb):
                    callback = cb
                    break
            if callback is not None:
                try:
                    callback(shot_path)
                except Exception as cb_err:
                    print(f"[STUDY-HUD] ⚠️ Vision injection failed: {cb_err}")
                    for target in (window, parent_widget):
                        if hasattr(target, "write_log"):
                            target.write_log(
                                f"⚠️ Screen capture saved but not loaded into context: {cb_err}")
                            break
                return

            # No live bridge attached yet (startup, or no session).
            for target in (window, parent_widget):
                if hasattr(target, "write_log"):
                    target.write_log(
                        f"SYS: 📸 Screen capture saved: {shot_path} "
                        "(JARVIS is offline — start a session, then ask about it.)")
                    break
        except Exception as err:
            print(f"[STUDY-HUD] ⚠️ Capture error: {err}")
            for target in (window, parent_widget):
                if hasattr(target, "write_log"):
                    target.write_log(f"⚠️ Screen Capture Error: {err}")
                    break

    def _open_approved_files_manager():
        choice = QMessageBox(parent_widget)
        choice.setWindowTitle("🔒 Approve Study Files")
        choice.setText(
            "Approve individual study files, or an entire folder?\n\n"
            "Folder approval is recursive: every file inside becomes readable "
            "by JARVIS without another popup. Sensitive system paths stay blocked."
        )
        btn_files_choice = choice.addButton(
            "📄 Select Files", QMessageBox.ButtonRole.AcceptRole)
        btn_folder_choice = choice.addButton(
            "📁 Select Folder", QMessageBox.ButtonRole.ActionRole)
        choice.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        choice.exec()
        clicked = choice.clickedButton()

        if clicked is btn_folder_choice:
            folder = QFileDialog.getExistingDirectory(
                parent_widget, "🔒 Select Study Folder to Approve", "")
            if not folder:
                return
            try:
                approved = approve_folder_permanently(folder)
            except Exception as err:
                QMessageBox.warning(parent_widget, "Security Block", str(err))
                return
            for target in (window, parent_widget):
                if hasattr(target, "write_log"):
                    target.write_log(
                        f"🔒 Approved folder (recursive): {approved}")
                    break
            return

        if clicked is not btn_files_choice:
            return

        files, _ = QFileDialog.getOpenFileNames(
            parent_widget,
            "🔒 Select Study Files to Approve for JARVIS",
            "",
            "Study Files (*.pdf *.txt *.md *.docx *.png *.jpg *.jpeg *.webp *.csv *.json);;All Files (*)"
        )
        added = []
        for f in files:
            try:
                approve_file_permanently(f)
                added.append(f)
            except Exception as err:
                QMessageBox.warning(parent_widget, "Security Block", str(err))
        if added:
            for target in (window, parent_widget):
                if hasattr(target, "write_log"):
                    target.write_log(f"🔒 Approved {len(added)} study file(s).")
                    break

    # Same dark glassmorphism treatment as the native Setup / Controls pills.
    _HUD_BTN_QSS = """
        QPushButton {
            background: rgba(12, 26, 38, 0.88);
            color: #38BDF8;
            border: 1px solid rgba(56, 189, 248, 0.45);
            border-radius: 6px;
            padding: 0px;
            font-family: 'Segoe UI Emoji', 'Segoe UI', sans-serif;
            font-size: 14px;
        }
        QPushButton:hover {
            background: rgba(14, 116, 144, 0.45);
            border: 1px solid #38BDF8;
            color: #F0F9FF;
        }
        QPushButton:pressed {
            background: rgba(2, 132, 199, 0.65);
        }
    """
    _HUD_BTN_QSS_ALT = """
        QPushButton {
            background: rgba(12, 26, 38, 0.88);
            color: #C084FC;
            border: 1px solid rgba(56, 189, 248, 0.45);
            border-radius: 6px;
            padding: 0px;
            font-family: 'Segoe UI Emoji', 'Segoe UI', sans-serif;
            font-size: 14px;
        }
        QPushButton:hover {
            background: rgba(107, 33, 168, 0.45);
            border: 1px solid #C084FC;
            color: #FAF5FF;
        }
        QPushButton:pressed {
            background: rgba(126, 34, 206, 0.65);
        }
    """

    btn_screen = QPushButton("📸", parent_widget)
    btn_screen.setObjectName("studyHudCaptureBtn")
    btn_screen.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_screen.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    btn_screen.setFixedHeight(28)
    btn_screen.setStyleSheet(_HUD_BTN_QSS)
    btn_screen.setToolTip("Capture the screen and load it into JARVIS's context (F9 / Ctrl+Shift+S)")
    btn_screen.clicked.connect(_trigger_manual_screen_capture)

    btn_files = QPushButton("🔒", parent_widget)
    btn_files.setObjectName("studyHudFilesBtn")
    btn_files.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_files.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    btn_files.setFixedHeight(28)
    btn_files.setStyleSheet(_HUD_BTN_QSS_ALT)
    btn_files.setToolTip("Approve study files or a whole folder (recursive: all subfolders and files inside)")
    btn_files.clicked.connect(_open_approved_files_manager)

    # Label sets, widest first. The narrowest set is used when the header is
    # too tight to fit the full captions next to Setup / Controls.
    _LABELS = (
        ("📸", "🔒"),
    )

    _state = {"controls": None, "busy": False}

    def _locate_controls():
        """Resolve (and cache) the native 'Controls' pill inside the header."""
        ref = _state["controls"]
        if ref is not None:
            try:
                if ref.isVisible() and ref.width() > 0:
                    return ref
            except RuntimeError:
                _state["controls"] = None
        ref = (_find_button_by_text(parent_widget, "Controls")
               or _find_button_by_text(parent_widget, "Setup"))
        _state["controls"] = ref
        return ref

    def _widget_rect_in_parent(widget):
        """Return (left, top, width, height) of widget in parent_widget coords."""
        if widget is None:
            return None
        try:
            if not widget.isVisible() or widget.width() <= 0:
                return None
            top_left = widget.mapTo(parent_widget, QPoint(0, 0))
            return (top_left.x(), top_left.y(), widget.width(), widget.height())
        except RuntimeError:
            return None

    def _reposition_responsive():
        """
        Anchor the two HUD buttons directly to the RIGHT edge of the native
        'Controls' pill, recomputed on every resize / show / state change.
        Falls back to progressively shorter labels so the buttons never
        overflow the left cluster or collide with the centred title.
        """
        if _state["busy"]:
            return
        win_w_now = parent_widget.width()
        ref_chk = _locate_controls()
        ref_x = ref_chk.x() if ref_chk is not None else 0
        cache_key = (win_w_now, ref_x)
        if _state.get("last_pos_key") == cache_key and btn_screen.isVisible() and btn_screen.x() > 50:
            return
        _state["last_pos_key"] = cache_key
        _state["busy"] = True
        try:
            win_w = parent_widget.width()
            ref = _locate_controls()
            ref_rect = _widget_rect_in_parent(ref)

            if ref_rect is not None:
                ref_left, ref_top, ref_w, ref_h = ref_rect
                start_x = ref_left + ref_w + 8
                y_pos = ref_top
                h_btn = ref_h if 24 <= ref_h <= 30 else 28
            else:
                header = getattr(parent_widget, "_header_widget", None)
                start_x = 118
                h_btn = 28
                if header is not None and header.height() > 0:
                    y_pos = header.mapTo(parent_widget, QPoint(0, 0)).y()
                    y_pos += max(0, (header.height() - h_btn) // 2)
                else:
                    y_pos = 13

            # Right boundary: never cross the centred title or the clock.
            max_right = max(80, win_w - 16)
            title_lbl = getattr(parent_widget, "_title_lbl", None)
            try:
                if title_lbl is not None and title_lbl.isVisible():
                    # The title label spans the full header, so measure its
                    # text instead of its geometry: text stays centred.
                    tw = title_lbl.fontMetrics().horizontalAdvance(title_lbl.text())
                    max_right = min(max_right, (win_w - tw) // 2 - 12)
            except RuntimeError:
                pass
            clock_lbl = getattr(parent_widget, "_clock_lbl", None)
            clock_rect = _widget_rect_in_parent(clock_lbl)
            if clock_rect is not None:
                max_right = min(max_right, clock_rect[0] - 12)

            btn_screen.setFixedHeight(h_btn)
            btn_files.setFixedHeight(h_btn)

            gap = 8
            chosen = None
            btn_screen.setText("📸")
            btn_files.setText("🔒")
            w_screen, w_files = 32, 32
            btn_screen.setFixedSize(w_screen, h_btn)
            btn_files.setFixedSize(w_files, h_btn)
            if start_x + w_screen + gap + w_files <= max_right:
                chosen = (w_screen, w_files)

            if chosen is None:
                # Extremely narrow header: keep the shortcut alive, show one
                # icon-only button so nothing spills outside the header.
                btn_screen.setText("📸")
                btn_files.setText("🔒")
                btn_screen.adjustSize()
                btn_files.adjustSize()
                w_screen = btn_screen.sizeHint().width()
                w_files = btn_files.sizeHint().width()
                if start_x + w_screen + gap + w_files <= max_right:
                    chosen = (w_screen, w_files)
                elif start_x + w_screen <= max_right:
                    btn_files.hide()
                    chosen = (w_screen, 0)
                else:
                    btn_screen.hide()
                    btn_files.hide()
                    return

            w_screen, w_files = chosen
            btn_screen.setGeometry(start_x, y_pos, w_screen, h_btn)
            btn_screen.show()
            btn_screen.raise_()
            if w_files > 0:
                btn_files.setGeometry(start_x + w_screen + gap, y_pos, w_files, h_btn)
                btn_files.show()
                btn_files.raise_()
        except Exception as err:
            print(f"[STUDY-HUD] Reposition error: {err}")
        finally:
            _state["busy"] = False

    class _HeaderResizeFilter(QObject):
        def eventFilter(self, obj, event):
            et = event.type()
            if et in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Move,
                      QEvent.Type.WindowStateChange):
                _reposition_responsive()
            return False

    resize_filter = _HeaderResizeFilter(parent_widget)
    parent_widget.installEventFilter(resize_filter)

    # Also watch the header itself: layout changes there move the Controls pill
    # before the main window resize event reaches us.
    ref_btn = _locate_controls()
    if ref_btn is not None:
        header_widget = ref_btn.parentWidget()
        if header_widget is not None:
            parent_widget._header_widget = header_widget
            header_widget.installEventFilter(resize_filter)

    # Application-wide shortcuts (F9 and Ctrl+Shift+S). Bound to the window,
    # not to the buttons, so they work even when the HUD is tucked away.
    sc_f9 = QShortcut(QKeySequence("F9"), parent_widget)
    sc_f9.setContext(Qt.ShortcutContext.ApplicationShortcut)
    sc_f9.activated.connect(_trigger_manual_screen_capture)

    sc_alt = QShortcut(QKeySequence("Ctrl+Shift+S"), parent_widget)
    sc_alt.setContext(Qt.ShortcutContext.ApplicationShortcut)
    sc_alt.activated.connect(_trigger_manual_screen_capture)

    QTimer.singleShot(0, _reposition_responsive)
    QTimer.singleShot(120, _reposition_responsive)
    QTimer.singleShot(600, _reposition_responsive)
    QTimer.singleShot(1800, _reposition_responsive)
    parent_widget._study_sec_widgets = (btn_screen, btn_files, sc_f9, sc_alt, resize_filter)
