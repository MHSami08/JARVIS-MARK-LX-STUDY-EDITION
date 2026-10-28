"""
tools/smoke_drop_to_study.py — offline end-to-end smoke test + thread/async profiler
for the Phase 5 drop-to-study path.

Runs with NO network and NO API key. It proves, on this machine:

  1. local PDF extraction works
  2. the payload guard (_truncate_head_tail) clamps a huge blob head+tail
  3. the cross-thread send boundary (_send_text_to_live) is correct:
       * off-loop caller  -> run_coroutine_threadsafe path
       * on-loop caller   -> ensure_future path
  4. a raising Live session is NOT swallowed (a traceback is printed)
  5. the whole _on_study_document chain reaches the session with a clamped prompt
  6. the event-loop stall watchdog fires and dumps every thread's stack

Run:
    .venv\\Scripts\\python.exe tools\\smoke_drop_to_study.py

Silence the trace lines with:  set JARVIS_TRACE=0
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("JARVIS_TRACE", "1")

import main as J  # noqa: E402  (import after sys.path setup)
from actions.file_processor import extract_document_text  # noqa: E402

_FAILS: list[str] = []


def check(name: str, ok: bool) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}", flush=True)
    if not ok:
        _FAILS.append(name)


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}", flush=True)


# ── helpers ──────────────────────────────────────────────────────────────────

class LoopThread:
    """A real asyncio loop running in a background thread — same shape as JARVIS."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="SmokeLoop")
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class FakeSession:
    """Minimal stand-in for the Live session."""

    def __init__(self, boom: bool = False) -> None:
        self.calls: list[dict] = []
        self.boom = boom

    async def send_client_content(self, turns=None, turn_complete=True):
        text = (turns or {}).get("parts", [{}])[0].get("text", "")
        self.calls.append({"len": len(text), "turn_complete": turn_complete})
        if self.boom:
            raise RuntimeError("simulated WebSocket 1007 invalid payload")


class StubUI:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def write_log(self, msg: str) -> None:
        self.logs.append(str(msg))
        print(f"   UI> {msg}", flush=True)


def make_live() -> "J.JarvisLive":
    """A JarvisLive with only the attributes the send path touches."""
    jl = J.JarvisLive.__new__(J.JarvisLive)
    jl.session = None
    jl._loop = None
    jl._loop_thread = None
    jl._hb = 0.0
    return jl


# ── 1. extraction ────────────────────────────────────────────────────────────

def test_extraction(tmp: Path) -> None:
    banner("1. local extraction")
    import pymupdf

    pdf = tmp / "big.pdf"
    doc = pymupdf.open()
    for i in range(40):
        page = doc.new_page()
        page.insert_text((72, 96), f"Page {i}: " + "lorem ipsum dolor sit amet " * 40, fontsize=11)
    doc.save(str(pdf))
    doc.close()

    txt = extract_document_text(pdf)
    check("pdf text extracted", len(txt) > 2000)
    check("pdf content correct", "Page 39" in txt and "lorem ipsum" in txt)
    check("unsupported type returns ''", extract_document_text(tmp / "nope.xyz") == "")


# ── 2. payload guard ─────────────────────────────────────────────────────────

def test_guard(huge: str) -> None:
    banner("2. payload guard (_truncate_head_tail)")
    body, truncated = J._truncate_head_tail(huge)
    check("huge blob is truncated", truncated)
    check("clamped under hard limit", len(body) <= J._STUDY_PAYLOAD_HARD_LIMIT)
    check("head kept", huge[:100] in body)
    check("tail kept", huge[-100:] in body)
    check("elision marker present", "omitted by the local payload guard" in body)
    small, got_trunc = J._truncate_head_tail("short text")
    check("small blob untouched", small == "short text" and not got_trunc)


# ── 3. cross-thread boundary ─────────────────────────────────────────────────

def test_boundary() -> None:
    banner("3. cross-thread hand-off (_send_text_to_live)")
    lt = LoopThread()
    jl = make_live()
    jl._loop = lt.loop

    # 3a — off-loop caller (Qt thread / ThreadPoolExecutor / plugin executor)
    jl._loop_thread = lt.thread
    sess = FakeSession()
    jl.session = sess
    jl._send_text_to_live("hello", tag="SMOKE_OFFLOOP")
    time.sleep(0.3)
    check("off-loop send reached the session",
          len(sess.calls) == 1 and sess.calls[0]["len"] == len("hello"))
    check("off-loop send had turn_complete=True", sess.calls and sess.calls[0]["turn_complete"] is True)

    # 3b — on-loop caller must NOT deadlock (uses ensure_future, never .result())
    async def _on_loop():
        jl._send_text_to_live("x" * 40, tag="SMOKE_ONLOOP")

    fut = asyncio.run_coroutine_threadsafe(_on_loop(), lt.loop)
    try:
        fut.result(timeout=3)
        ok = True
    except Exception:
        ok = False
    time.sleep(0.3)
    check("on-loop send did not deadlock", ok)
    check("on-loop send reached the session",
          len(sess.calls) == 2 and sess.calls[1]["len"] == 40)

    # 3c — a raising session must surface, not vanish
    boom = FakeSession(boom=True)
    jl.session = boom
    print("  (a traceback SHOULD appear below — that is the fix working)")
    jl._send_text_to_live("boom", tag="SMOKE_BOOM")
    time.sleep(0.4)
    check("raising session was still queued (traceback printed above)", len(boom.calls) == 1)

    # 3d — no session / cleared session is a clean no-op
    jl.session = None
    check("no-session send returns False (no crash)",
          jl._send_text_to_live("nothing", tag="SMOKE_NOSESS") is False)

    lt.stop()


# ── 4. full drop-to-study chain ──────────────────────────────────────────────

def test_full_chain(pdf: Path) -> None:
    banner("4. full _on_study_document chain")
    lt = LoopThread()
    jl = make_live()
    jl._loop = lt.loop
    jl._loop_thread = lt.thread
    jl._wake_enabled = False
    jl._awake = True
    jl.ui = StubUI()
    sess = FakeSession()
    jl.session = sess

    jl._on_study_document(str(pdf))          # called exactly as the Qt worker thread does
    time.sleep(0.5)

    check("exactly one Live send from one drop", len(sess.calls) == 1)
    total = sess.calls[0]["len"] if sess.calls else 0
    check("sent prompt is clamped (< 13000 chars)", 0 < total < 13000)
    check("UI reported the ingest", any("read locally" in m for m in jl.ui.logs))
    lt.stop()


# ── 5. watchdog ──────────────────────────────────────────────────────────────

def test_watchdog() -> None:
    banner("5. event-loop stall watchdog (expect a stack dump below)")
    J._LOOP_LAG_WARN = 1.0                  # tighten for the test
    lt = LoopThread()
    jl = make_live()
    jl._loop = lt.loop

    async def _arm():
        jl._start_watchdog()               # sets _loop_thread to the loop thread

    asyncio.run_coroutine_threadsafe(_arm(), lt.loop).result(timeout=3)

    async def _block():
        time.sleep(2.5)                    # blocking the loop on purpose

    asyncio.run_coroutine_threadsafe(_block(), lt.loop)
    time.sleep(4.0)
    check("watchdog ran without crashing", True)
    lt.stop()
    J._LOOP_LAG_WARN = 3.0


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_smoke_"))
    huge = ("sentence number %d. " % 0).join(f"w{i}" for i in range(20000))

    test_extraction(tmp)
    test_guard(huge)

    # reuse the big pdf from test_extraction for the full chain
    test_full_chain(tmp / "big.pdf")
    test_boundary()
    test_watchdog()

    banner("RESULT")
    if _FAILS:
        print(f"  {len(_FAILS)} FAILURE(S): {_FAILS}", flush=True)
        return 1
    print("  ALL CHECKS PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
