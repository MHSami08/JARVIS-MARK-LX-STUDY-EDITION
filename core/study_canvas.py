"""
Study Station canvas: a QWebEngineView running runtime/study_board.html.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

try:
    from PyQt6.QtWebChannel import QWebChannel
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAVE_STUDY = True
except Exception as _e:
    QWebChannel = QWebEngineSettings = QWebEngineView = None
    HAVE_STUDY = False
    print(f"[Study] canvas unavailable ({_e}) - pip install PyQt6-WebEngine")

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent
BOARD_HTML = _BASE / "runtime" / "study_board.html"

_MAX_PENDING = 8


class StudyBridge(QObject):
    """The object the page sees as `bridge` (QWebChannel)."""
    toBoard = pyqtSignal(str)
    ready = pyqtSignal()
    event = pyqtSignal(str)

    @pyqtSlot()
    def boardReady(self) -> None:
        self.ready.emit()

    @pyqtSlot(str)
    def userEvent(self, payload: str) -> None:
        self.event.emit(payload)


class StudyCanvasWidget(QWidget):
    user_event = pyqtSignal(str, dict)
    _send_sig = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._ready = False
        self._loaded = False
        self._pending: list[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        if not HAVE_STUDY:
            self._view = None
            hint = QLabel("Study board needs the Qt web engine.\n\npip install PyQt6-WebEngine")
            hint.setStyleSheet("color:#6f93ad; background:#02060d;")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(hint)
            return

        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setStyleSheet("background:#02060d;")
        self._view = QWebEngineView(self)
        self._view.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._view.setStyleSheet("background:#02060d;")
        page = self._view.page()
        page.setBackgroundColor(QColor("#02060d"))
        s = page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        self._bridge = StudyBridge(self)
        self._channel = QWebChannel(page)
        self._channel.registerObject("bridge", self._bridge)
        page.setWebChannel(self._channel)
        self._bridge.ready.connect(self._on_ready)
        self._bridge.event.connect(self._on_event)
        page.loadFinished.connect(self._on_load_finished)
        self._send_sig.connect(self._deliver)
        lay.addWidget(self._view)

    def ensure_loaded(self) -> None:
        if self._view is None or self._loaded:
            return
        self._loaded = True
        if not BOARD_HTML.exists():
            self._view.setHtml(f"<body style='background:#02060d;color:#6f93ad;font:15px sans-serif;"
                               f"padding:40px'>Missing {BOARD_HTML}</body>")
            return
        self._view.load(QUrl.fromLocalFile(str(BOARD_HTML)))

    def _on_load_finished(self, ok: bool) -> None:
        if ok and not self._ready:
            self._on_ready()

    def _on_ready(self) -> None:
        self._ready = True
        for msg in self._pending:
            self._push_to_page(msg)
        self._pending.clear()

    def _push_to_page(self, msg: str) -> None:
        if self._view is None:
            return
        js_arg = json.dumps(msg, ensure_ascii=False)
        self._view.page().runJavaScript(
            f"try {{ const m = JSON.parse({js_arg}); if (typeof ops !== 'undefined' && ops[m.op]) ops[m.op](m); }} catch(e) {{ console.error(e); }}"
        )
        self._view.update()

    def _post(self, op: str, **kw) -> None:
        self._send_sig.emit(json.dumps({"op": op, **kw}, ensure_ascii=False))

    def _deliver(self, msg: str) -> None:
        if self._view is None:
            return
        self.ensure_loaded()
        print(f"[StudyCanvas] Delivering to board: {msg[:90]}...")
        if self._ready:
            self._push_to_page(msg)
        else:
            self._pending.append(msg)
            del self._pending[:-_MAX_PENDING]

    def show_solution(self, title: str, markdown: str, subject: str = "",
                      reveal: str = "steps") -> None:
        self._post("render", title=title, markdown=markdown, subject=subject,
                   reveal="all" if reveal == "all" else "steps")

    def show_quiz(self, title: str, questions: list, subject: str = "") -> None:
        self._post("quiz", title=title, questions=questions or [], subject=subject)

    def show_flashcards(self, title: str, cards: list, subject: str = "") -> None:
        self._post("flashcards", title=title, cards=cards or [], subject=subject)

    def show_mindmap(self, title: str, tree: dict, subject: str = "") -> None:
        self._post("mindmap", title=title, tree=tree or {}, subject=subject)

    def show_status(self, text: str) -> None:
        self._post("status", text=text)

    def next_step(self) -> None:
        self._post("next")

    def prev_step(self) -> None:
        self._post("prev")

    def restore(self, target: str = "") -> None:
        self._post("restore", target=target)

    def clear(self) -> None:
        self._post("clear")

    def _on_event(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            kind = str(data.pop("type", ""))
        except Exception:
            return
        if kind:
            self.user_event.emit(kind, data)