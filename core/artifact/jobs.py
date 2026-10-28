"""
core/artifact/jobs.py - background drawing jobs (WP2).

The voice model must never wait for a figure. `study_visualizer.execute()` calls
`RUNNER.submit(...)`, gets an answer in microseconds, and returns to the Live session.
The drawing runs on a daemon thread and reaches the board through the same thread-safe
UI hooks the old synchronous path used:  player.study_status(text)  and
player.study_solution(title, markdown, subject, reveal="all")  (both are Qt signals).

Rules enforced here
  * one job runs at a time (a lock); a new submit supersedes the old job -> the old
    result is dropped at its next stage check;
  * stale = superseded OR cancelled OR Study Mode turned off OR older than 120 s;
  * identical (title, kind) while a job is running -> "duplicate", no second job;
  * status lines are >= 400 ms apart; failures are a calm Bengali line, never a traceback;
  * nothing here touches Qt widgets directly.

PHASE A pipeline = DRAW (strong model) -> SANITIZE -> DELIVER.
Phase B grows it to PLAN -> DRAW -> COMPILE -> LINT -> DELIVER (see master prompt PART 4).
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

JOB_BUDGET_S = 120.0
STATUS_MIN_GAP_S = 0.4

STAGE_LABELS = {
    1: "১/৪ পরিকল্পনা করছি…",
    2: "২/৪ আঁকছি…",
    3: "৩/৪ বিন্যাস ও রং…",
    4: "৪/৪ যাচাই করছি…",
}
FAIL_STATUS = "ছবিটি আঁকা গেল না — একটু পরে আবার বলো।"
TIMEOUT_STATUS = "ছবি আঁকতে বেশি সময় লাগছে — একটু পরে আবার চেষ্টা করো।"

_PRIMITIVE_RX = re.compile(r"<(?:path|ellipse|circle|rect|polyline|polygon|line)\b")
_MIN_PRIMITIVES_PHASE_A = 12


def norm_key(title: Any, kind: Any = "") -> str:
    """Normalised (title, kind) used to spot duplicate requests."""
    t = re.sub(r"[\W_]+", " ", str(title or "").lower(), flags=re.U).strip()
    k = re.sub(r"[\W_]+", " ", str(kind or "").lower(), flags=re.U).strip()
    return f"{k}|{t}"


class Job:
    def __init__(self, job_id: int, generation: int, spec: Dict[str, Any], player: Any,
                 speak: Optional[Callable[[str], Any]], runner: "JobRunner"):
        self.id = job_id
        self.generation = generation
        self.spec = spec
        self.player = player
        self.speak = speak
        self.runner = runner
        self.key = norm_key(spec.get("title"), spec.get("kind"))
        self.started = time.monotonic()
        self.cancelled = False
        self.running = True
        self.announced = False
        self._last_status = 0.0

    # ---- staleness ---------------------------------------------------------------------
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float:
        return max(0.0, JOB_BUDGET_S - self.elapsed())

    def timed_out(self) -> bool:
        return self.elapsed() > JOB_BUDGET_S

    def superseded(self) -> bool:
        """Dropped by the user's next request, a cancel, or Study Mode being switched off."""
        if self.cancelled or self.runner.generation != self.generation:
            return True
        try:
            if getattr(self.player, "study_mode_active", True) is False:
                return True
        except Exception:
            pass
        return False

    def stale(self) -> bool:
        return self.superseded() or self.timed_out()

    # ---- UI (signal based, thread-safe) --------------------------------------------------
    def status(self, text: str) -> None:
        """Post a status line to the board; >= 400 ms after the previous one."""
        if self.superseded():
            return
        wait = STATUS_MIN_GAP_S - (time.monotonic() - self._last_status)
        if wait > 0:
            time.sleep(wait)
            if self.superseded():
                return
        self._last_status = time.monotonic()
        try:
            self.player.study_status(text)
        except Exception as e:
            print(f"[Artifact] status failed: {type(e).__name__}")

    def stage(self, n: int) -> None:
        self.status(STAGE_LABELS.get(n, ""))

    def deliver(self, title: str, markdown: str, subject: str = "") -> bool:
        """Send the finished figure to the board unless the job went stale. True if sent."""
        if self.stale():
            return False
        try:
            self.player.study_solution(title, markdown, subject, reveal="all")
            return True
        except Exception as e:
            print(f"[Artifact] deliver failed: {type(e).__name__}: {str(e)[:120]}")
            return False


class JobRunner:
    def __init__(self, pipeline: Optional[Callable[[Job], bool]] = None):
        self._state = threading.Lock()      # guards _gen / _current
        self._run_lock = threading.Lock()   # only one pipeline runs at a time
        self._gen = 0
        self._ids = itertools.count(1)
        self._current: Optional[Job] = None
        self._pipeline = pipeline

    @property
    def generation(self) -> int:
        return self._gen

    def submit(self, spec: Dict[str, Any], player: Any, speak: Optional[Callable[[str], Any]] = None,
               pipeline: Optional[Callable[[Job], bool]] = None) -> Tuple[str, int]:
        """Start a job. Returns ("started" | "duplicate", job_id). Never blocks."""
        key = norm_key(spec.get("title"), spec.get("kind"))
        with self._state:
            cur = self._current
            if cur is not None and cur.running and cur.key == key and not cur.stale():
                return "duplicate", cur.id
            self._gen += 1
            job = Job(next(self._ids), self._gen, dict(spec), player, speak, self)
            self._current = job
        fn = pipeline or self._pipeline or basic_pipeline
        threading.Thread(target=self._worker, args=(job, fn), daemon=True,
                         name=f"artifact-job-{job.id}").start()
        return "started", job.id

    def cancel(self) -> bool:
        """Drop the running job's result. True if something was running."""
        with self._state:
            self._gen += 1
            cur = self._current
            if cur is not None:
                cur.cancelled = True
            return bool(cur is not None and cur.running)

    def is_running(self) -> bool:
        cur = self._current
        return bool(cur is not None and cur.running)

    def _worker(self, job: Job, pipeline: Callable[[Job], bool]) -> None:
        # Wait for our turn; leave quietly if we were superseded while waiting.
        while not self._run_lock.acquire(timeout=0.25):
            if job.stale():
                job.running = False
                return
        try:
            delivered = False
            if not job.stale():
                try:
                    delivered = bool(pipeline(job))
                except Exception as e:  # calm line for the student, detail on the console only
                    print(f"[Artifact] job {job.id} failed: {type(e).__name__}: {str(e)[:160]}")
            if delivered:
                _maybe_announce(job)
            elif not job.superseded():
                job.status(TIMEOUT_STATUS if job.timed_out() else FAIL_STATUS)
        finally:
            job.running = False
            self._run_lock.release()


def _maybe_announce(job: Job) -> None:
    """Optional spoken "diagram is ready" notice (config flag visual_announce_done, default off).

    G9: main.speak() sends a USER-role turn with turn_complete=True. Called while the model is
    talking or a tool is running it makes the model answer as if the student had spoken. So we
    only speak when no tool is in flight, JARVIS is not mid-sentence, the student has not just
    spoken, and at most once per job.
    """
    try:
        import memory.config_manager as config_manager
        if not config_manager.get_visual_announce_done():
            return
    except Exception:
        return
    speak = job.speak
    if not callable(speak) or job.announced:
        return
    owner = getattr(speak, "__self__", None)
    if getattr(owner, "_tool_in_flight", False) or getattr(owner, "_is_speaking", False):
        return
    if time.monotonic() - float(getattr(owner, "_last_user_speech", 0.0) or 0.0) < 3.0:
        return
    job.announced = True
    try:
        speak("[SYS] The diagram is now on the board. Say one short sentence.")
    except Exception:
        pass


def basic_pipeline(job: Job) -> bool:
    """PHASE A: one strong-model DRAW call, sanitized, then delivered. True if delivered."""
    import core.model_router as model_router  # lazy: keep startup unchanged
    import core.artifact.design as design
    import core.artifact.sanitize as sanitize

    spec = job.spec
    title = str(spec.get("title") or "Diagram")
    kind = str(spec.get("kind") or "diagram")
    subject = str(spec.get("subject") or "")
    brief = str(spec.get("brief") or title)

    job.stage(2)
    prompt = (f"Figure title: {title}\nSubject: {subject or 'general'}\nKind: {kind}\n"
              f"What to draw and which parts must be labelled: {brief}\n"
              "Return the complete <svg> now.")
    found: Dict[str, str] = {}

    def _accept(text: str) -> bool:
        svg, _w = sanitize.sanitize_svg_ex(text, max_chars=design.MAX_MODEL_SVG_CHARS, generated="ai")
        if svg and len(_PRIMITIVE_RX.findall(svg)) >= _MIN_PRIMITIVES_PHASE_A:
            found["svg"] = svg
            return True
        return False

    raw = model_router.call(prompt, task="diagram_draw", system=design.illustrator_system(kind),
                            timeout=70.0, budget=max(10.0, job.remaining() - 5.0), accept=_accept)
    if job.stale():
        return False
    svg = found.get("svg") or ""
    if not svg and raw:
        svg = sanitize.sanitize_svg(raw, generated="ai")
    if not svg:
        return False

    job.stage(4)
    markdown = f"## চিত্র ও ডায়াগ্রাম: {title}\n\n```svg\n{svg.replace('```', '')}\n```"
    if not job.deliver(title, markdown, subject):
        return False
    try:  # vault log is best effort and never blocks delivery
        from core.study_database import save_study_artifact
        save_study_artifact(artifact_type=f"visual_{kind}", subject=subject or "General",
                            topic=title, payload={"visual_type": kind, "title": title, "markdown": markdown})
    except Exception as e:
        print(f"[Artifact] vault log warning: {type(e).__name__}")
    return True


RUNNER = JobRunner()
