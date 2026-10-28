# -*- coding: utf-8 -*-
"""
core/activity_hud.py - a one-line "what is JARVIS doing right now" strip in the footer (bottom-right).

    ● ডায়াগ্রাম তৈরি করছি: Wheatstone Bridge — ২/৪ ছবি আঁকছি…  ·  14s

It is a pure addition: nothing in ui.py is rewritten. `install(ui)` is called once from JarvisUI.__init__
(apply_activity_hud.py adds that single line), finds the footer, appends one QLabel to it and gives JarvisUI a
thread-safe `set_activity(text, owner="tool")`.  Several things can talk at once, the most important wins:

    owner "forge"  long-running background work that reports its own steps (the diagram illustrator)
    owner "tool"   the tool the voice model just called   (main.py sets it, then clears it when the tool returns)
    owner "state"  "ভাবছি…" while the assistant is in the THINKING state

Everything is wrapped so that a failure here can never stop JARVIS from starting or from talking.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

PRIORITY = ("forge", "tool", "state")
STALE_AFTER_S = 600.0            # an owner that never cleared itself is dropped after 10 minutes

_TOOL_BN = {
    "web_search": "ওয়েবে খুঁজছি", "study_visualizer": "ডায়াগ্রাম তৈরি করছি", "study_solver": "সমাধান করছি",
    "study_solve_problem": "সমাধান করছি", "study_quiz": "কুইজ বানাচ্ছি", "study_flashcard": "ফ্ল্যাশকার্ড বানাচ্ছি",
    "study_mindmap": "মাইন্ডম্যাপ বানাচ্ছি", "study_vault": "স্টাডি ভল্ট দেখছি", "file_reader": "ফাইল পড়ছি",
    "file_processor": "ফাইল বিশ্লেষণ করছি", "smart_file_search": "ফাইল খুঁজছি", "code_helper": "কোড নিয়ে কাজ করছি",
    "dev_agent": "কোড নিয়ে কাজ করছি", "desktop_control": "ডেস্কটপ চালাচ্ছি", "screen_agent": "স্ক্রিন দেখছি",

}

def tool_text(name: str, args: Optional[dict] = None) -> str:
    """Friendly Bengali one-liner for a tool call."""
    args = args or {}
    base = _TOOL_BN.get(name) or f"{str(name).replace('_', ' ')} চালাচ্ছি"
    hint = ""
    for key in ("title", "query", "topic", "file_path"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            hint = " ".join(v.split())[:34]
            break
    return f"{base}: {hint}" if hint else base

class ActivityModel:
    """The Qt-free part: who is busy with what, and since when."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._items: Dict[str, Tuple[str, float, float]] = {}      # owner -> (text, started, last_update)

    def set(self, owner: str, text: str) -> None:
        now = self._clock()
        with self._lock:
            text = " ".join(str(text or "").split())
            if not text:
                self._items.pop(owner, None)
                return
            old = self._items.get(owner)
            self._items[owner] = (text, old[1] if old else now, now)

    def current(self) -> Optional[Tuple[str, float]]:
        """(text, seconds_elapsed) of the most important live owner, or None when idle."""
        now = self._clock()
        with self._lock:
            for o in [o for o, (_, _, last) in self._items.items() if now - last > STALE_AFTER_S]:
                del self._items[o]
            for owner in PRIORITY:
                if owner in self._items:
                    text, started, _ = self._items[owner]
                    return text, now - started
        return None

def render_text(cur: Optional[Tuple[str, float]], tick: int) -> str:
    if cur is None:
        return ""
    text, secs = cur
    dot = "●" if tick % 2 == 0 else "○"
    return f"{dot} {text}" + (f"  ·  {int(secs)}s" if secs >= 3 else "")

def install(ui) -> bool:
    """Attach the strip to the footer of `ui` (a JarvisUI). Returns True when installed."""
    try:
        if getattr(ui, "_activity_model", None) is not None:
            return True
        from PyQt6.QtCore import QObject, QTimer, pyqtSignal
        from PyQt6.QtWidgets import QLabel, QWidget

        win = ui._win
        footer = win.findChild(QWidget, "glassFooter")
        if footer is None or footer.layout() is None:
            print("[ActivityHUD] footer not found - strip not installed")
            return False
        model = ActivityModel()

        label = QLabel("", footer)
        base_font = label.font()
        base_font.setPointSize(9)
        base_font.setBold(True)
        label.setFont(base_font)
        label.setStyleSheet("color: #38BDF8; background: transparent; border: none;")
        label.hide()
        footer.layout().addWidget(label)

        class _Bridge(QObject):
            sig = pyqtSignal(str, str)

        bridge = _Bridge(footer)                      # lives in the Qt thread: emits from any thread are queued
        tick = [0]

        def refresh() -> None:
            tick[0] += 1
            text = render_text(model.current(), tick[0])
            if text:
                label.setText(text)
                if not label.isVisible():
                    label.show()
            elif label.isVisible():
                label.hide()

        def on_set(owner: str, text: str) -> None:
            model.set(owner, text)
            refresh()

        bridge.sig.connect(on_set)
        timer = QTimer(footer)
        timer.setInterval(450)
        timer.timeout.connect(refresh)
        timer.start()

        cls = type(ui)

        def set_activity(self, text: str, owner: str = "tool") -> None:
            """Thread-safe. Empty text clears this owner's line."""
            try:
                self._activity_bridge.sig.emit(str(owner), str(text or ""))
            except Exception:
                pass

        orig_set_state = cls.set_state

        def set_state(self, state: str):
            orig_set_state(self, state)
            try:
                self.set_activity("ভাবছি…" if state in ("THINKING", "PROCESSING") else "", owner="state")
            except Exception:
                pass

        cls.set_activity = set_activity
        cls.set_state = set_state
        ui._activity_bridge, ui._activity_model, ui._activity_timer, ui._activity_label = bridge, model, timer, label
        return True
    except Exception as e:                                           # noqa: BLE001
        print(f"[ActivityHUD] not installed: {type(e).__name__}: {e}")
        return False