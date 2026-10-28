"""
tools/latency_guard.py - fails when the Live-facing budgets of the Artifact Studio are broken.

Run:  python tools/latency_guard.py          (exit code != 0 when a budget is exceeded)

Budgets (master prompt PART 3 / outcomes O1, O6):
  * study_visualizer.execute() returns in < 400 ms on every path (20 runs each, worst case judged);
  * the Live tool declaration is >= 40 % smaller than the original (4604 chars);
  * importing the tool does not import the heavy artifact package (startup unchanged).
No network: the model router is stubbed with a slow (3 s) fake. Dev tool only.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASELINE_DECL_CHARS = 4604
EXEC_BUDGET_MS = 400.0
RUNS = 20

t0 = time.perf_counter()
from actions import study_visualizer as sv  # noqa: E402
import_ms = (time.perf_counter() - t0) * 1000
sv.save_study_artifact = None
heavy = [m for m in ("core.artifact.jobs", "core.artifact.sanitize", "core.artifact.design") if m in sys.modules]

import core.model_router as model_router  # noqa: E402


def _slow_call(prompt, task="fast", system="", model=None, timeout=None, budget=None, accept=None):
    time.sleep(3.0)
    return ""


model_router.call = _slow_call


class _Player:
    study_mode_active = True

    def set_study_mode(self, on, notify_voice=False):
        self.study_mode_active = on

    def study_status(self, text):
        pass

    def study_solution(self, *a, **k):
        pass


def _measure(label, make_params):
    times = []
    for i in range(RUNS):
        player = _Player()
        params = make_params(i)
        t = time.perf_counter()
        sv.execute(params, player)
        times.append((time.perf_counter() - t) * 1000)
    return label, times


spec = json.dumps({"x_range": [-5, 5], "y_range": [-5, 5], "functions": [{"expr": "x^2-4"}]})
rows = [
    _measure("async draw (new topic each run)", lambda i: {"title": f"Figure {i}", "kind": "diagram", "brief": f"thing {i}"}),
    _measure("library template", lambda i: {"title": "mushroom", "template_id": "agaricus_mushroom"}),
    _measure("graph (coord spec)", lambda i: {"title": f"g{i}", "kind": "graph", "coord_spec_json": spec}),
    _measure("legacy svg_markup", lambda i: {"title": f"s{i}", "svg_markup": '<svg viewBox="0 0 10 10"><rect width="5" height="5"/></svg>'}),
]

print(f"import actions.study_visualizer: {import_ms:.1f} ms; heavy artifact modules imported at import time: {heavy or 'none'}")
print(f"{'path':36s} {'median':>8s} {'p95':>8s} {'worst':>8s}  (ms, {RUNS} runs)")
worst_all = 0.0
for label, ts in rows:
    ts_sorted = sorted(ts)
    p95 = ts_sorted[int(len(ts_sorted) * 0.95) - 1]
    worst_all = max(worst_all, max(ts))
    print(f"{label:36s} {statistics.median(ts):8.1f} {p95:8.1f} {max(ts):8.1f}")

d = {k: v for k, v in sv.TOOL.items() if k != "handler"}
size = len(json.dumps(d, ensure_ascii=False))
cut = 100 - size * 100 / BASELINE_DECL_CHARS
print(f"declaration: {BASELINE_DECL_CHARS} -> {size} chars ({cut:.0f}% smaller)")

problems = []
if worst_all >= EXEC_BUDGET_MS:
    problems.append(f"execute() worst case {worst_all:.0f} ms >= {EXEC_BUDGET_MS:.0f} ms")
if size > BASELINE_DECL_CHARS * 0.6:
    problems.append(f"declaration only {cut:.0f}% smaller (need >= 40%)")
if heavy:
    problems.append(f"heavy modules imported at startup: {heavy}")
print("RESULT:", "OK" if not problems else "FAIL - " + "; ".join(problems))
sys.exit(1 if problems else 0)
