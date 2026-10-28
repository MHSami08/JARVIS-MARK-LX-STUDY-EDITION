"""
tools/test_artifact_studio.py - offline tests for the Artifact Studio (pytest-free, plain asserts).

Run:  python tools/test_artifact_studio.py        (exit code != 0 on any FAIL)

No network, no API keys: the model router is replaced by a stub. Dev tool only - not loaded by the app.
Phase A covers: sanitizer, router (chains / accept / generation config), job runner, the lean tool,
prompt cleanup, declaration size.  Later phases append their tests here.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASELINE_DECL_CHARS = 4604          # json.dumps of the ORIGINAL study_visualizer declaration (measured)
_results: list[tuple[str, bool, str]] = []

# Never touch the student's real vault (data/study_vault.db) from tests.
import core.study_database as _sdb  # noqa: E402
_sdb.save_study_artifact = lambda *a, **k: None


def check(name: str):
    def deco(fn):
        try:
            fn()
            _results.append((name, True, ""))
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            _results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"FAIL  {name}  -> {type(e).__name__}: {e}")
        return fn
    return deco


def wait_until(cond, timeout=8.0, step=0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class StubPlayer:
    """What the job runner / tool need from the UI (all signal-based in the real app)."""

    def __init__(self):
        self.study_mode_active = True
        self.statuses: list[tuple[float, str]] = []
        self.renders: list[tuple[str, str, str, str]] = []

    def set_study_mode(self, on, notify_voice=False):
        self.study_mode_active = bool(on)

    def study_status(self, text):
        self.statuses.append((time.monotonic(), text))

    def study_solution(self, title, markdown, subject="", reveal="steps"):
        self.renders.append((title, markdown, subject, reveal))


def _good_svg(n=30) -> str:
    shapes = "".join(f'<circle cx="{40 + i * 20}" cy="{100 + (i % 5) * 30}" r="8" fill="#38BDF8"/>' for i in range(n))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 660">'
            '<rect width="1000" height="660" rx="14" fill="#07111D"/>' + shapes +
            '<text x="40" y="60" font-size="16" fill="#F8FAFC">Title</text></svg>')


def _install_stub_router(delay=0.0, raises=False, reply=None, calls=None):
    import core.model_router as model_router

    def stub_call(prompt, task="fast", system="", model=None, timeout=None, budget=None, accept=None):
        if calls is not None:
            calls.append(task)
        time.sleep(delay)
        if raises:
            raise RuntimeError("stub provider exploded")
        out = reply if reply is not None else _good_svg()
        if accept is not None and not accept(out):
            return ""
        return out

    model_router.call = stub_call
    return model_router


# ---------------------------------------------------------------------------------------------
# sanitizer
# ---------------------------------------------------------------------------------------------
from core.artifact.sanitize import sanitize_svg, sanitize_svg_ex  # noqa: E402

_WRAP = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 660">{}</svg>'


@check("sanitize: hostile payloads never survive")
def _():
    hostile = [
        "<script>window.__pwned=1</script>",
        '<rect width="5" height="5" onload="window.__pwned=1" onclick="x()"/>',
        '<foreignObject><img src=x onerror="window.__pwned=1"/></foreignObject>',
        '<image href="http://evil.example/a.png"/>',
        '<a xlink:href="javascript:alert(1)"><rect width="5" height="5"/></a>',
        '<use href="http://evil.example/x.svg#a"/>',
        '<rect style="fill:url(http://evil.example/x)" width="5" height="5"/>',
        "<style>@import url(http://evil.example/x.css);</style>",
        '<rect fill="url(https://evil.example/p)" width="5" height="5"/>',
        '<animate attributeName="x" to="9"/>',
        '<feImage href="http://evil.example/i.png"/>',
    ]
    for frag in hostile:
        out = sanitize_svg(_WRAP.format(frag))
        low = out.lower()
        for bad in ("<script", "onload", "onclick", "onerror", "foreignobject", "<image", "evil.example",
                    "javascript:", "<style", "<animate", "<a ", "feimage", "@import"):
            assert bad not in low, f"{bad!r} survived {frag!r}"
    out = sanitize_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="5" height="5"/></svg>'
                       '<img src=x onerror="window.__pwned=1">')
    assert "<img" not in out and "onerror" not in out


@check("sanitize: DOCTYPE / ENTITY bombs and oversize are rejected")
def _():
    bomb = ('<!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            + _WRAP.format("<text>&b;</text>"))
    svg, why = sanitize_svg_ex(bomb)
    assert svg == "" and why
    assert sanitize_svg("<!ENTITY x 'y'>" + _WRAP.format("")) == ""
    big = _WRAP.format("<rect width='1' height='1'/>" * 20000)           # > 300 000 chars
    assert sanitize_svg(big) == ""
    assert sanitize_svg("") == "" and sanitize_svg("no svg here") == ""


@check("sanitize: safe features are kept (filters, gradients, local refs, bare &)")
def _():
    frag = ('<defs><filter id="f"><feGaussianBlur stdDeviation="3"/><feOffset dx="1"/><feDropShadow dx="1" dy="1"/></filter>'
            '<radialGradient id="g"><stop offset="0" stop-color="#fff"/></radialGradient></defs>'
            '<ellipse cx="5" cy="5" rx="4" ry="2" fill="url(#g)" filter="url(#f)"/>'
            '<use href="#g"/><text x="1" y="1">Tom & Jerry</text>')
    out = sanitize_svg(_WRAP.format(frag), generated="ai")
    for want in ("<filter", "<feGaussianBlur", "<feOffset", "<feDropShadow", "<radialGradient", 'url(#g)',
                 'href="#g"', "Tom &amp; Jerry", 'data-jv-generated="ai"', 'role="img"'):
        assert want in out, f"missing {want!r}"
    assert "width=" not in out.split(">")[0] and "height=" not in out.split(">")[0]


@check("sanitize: viewBox guaranteed, width/height removed, deterministic")
def _():
    out = sanitize_svg('<svg xmlns="http://www.w3.org/2000/svg" width="760" height="420"><rect width="3" height="3"/></svg>')
    assert 'viewBox="0 0 760 420"' in out
    a = sanitize_svg(_good_svg()); b = sanitize_svg(_good_svg())
    assert a == b and a


# ---------------------------------------------------------------------------------------------
# router (WP3)
# ---------------------------------------------------------------------------------------------
import core.model_router as mr  # noqa: E402
_ORIG_CALL = mr.call


@check("router: diagram chains exist, strongest first, only known model ids")
def _():
    known = {m for chain in ("coding", "writing", "fast", "study", "study_fast") for _p, m in mr.DEFAULT_RULES[chain]}
    for name in ("diagram_plan", "diagram_draw"):
        assert mr.DEFAULT_RULES[name], name
        for _p, m in mr.DEFAULT_RULES[name]:
            assert m in known, (name, m)
    assert mr.DEFAULT_RULES["diagram_draw"][0] == ("gemini", "gemini-2.5-flash")
    assert "lite" not in mr.DEFAULT_RULES["diagram_draw"][0][1]
    assert mr.DEFAULT_RULES["diagram_plan"][0] == ("gemini", "gemini-2.5-flash-lite")


@check("router: existing tasks send no generation config; diagram tasks do")
def _():
    assert set(mr._GEN_CONFIG) == {"diagram_plan", "diagram_draw"}
    for t in ("fast", "coding", "writing", "study", "study_fast"):
        assert t not in mr._GEN_CONFIG
    assert mr._openai_body("m", [], None) == {"model": "m", "messages": []}
    assert mr._openai_body("m", [], {"max_output_tokens": 99})["max_tokens"] == 99


@check("router: accept= skips a garbage rung and uses the next one")
def _():
    mr._refresh()
    mr._pools["gemini"].sync(["test-gemini-key"])
    mr._pools["groq"].sync(["test-groq-key"])
    mr._pools["openrouter"].sync([])
    seen = []

    def gem(key, model, prompt, system, json_mode, timeout=None, gen=None):
        seen.append(("gemini", model, gen))
        return "sorry, I cannot draw that"                      # not an SVG -> must be skipped

    def groq(key, model, prompt, system, json_mode, timeout=None, gen=None):
        seen.append(("groq", model, gen))
        return _good_svg()

    saved = dict(mr._ADAPTERS)
    mr._ADAPTERS.update({"gemini": gem, "groq": groq})
    try:
        out = _ORIG_CALL("draw", task="diagram_draw", system="s", accept=lambda t: "<svg" in t)
        assert out.startswith("<svg"), out[:40]
        assert [s[0] for s in seen] == ["gemini", "groq"], seen
        assert seen[0][2] == {"max_output_tokens": 16384, "thinking_budget": 1024}      # gen config reached the adapter
        seen.clear()
        out2 = _ORIG_CALL("hi", task="fast")                      # default behaviour untouched
        assert out2 and seen[0][2] is None
    finally:
        mr._ADAPTERS.clear(); mr._ADAPTERS.update(saved)
        for p in ("gemini", "groq"):
            mr._pools[p].sync([])


@check("router: payload guard still bigger than the illustrator prompt")
def _():
    import core.artifact.design as design
    for kind in ("diagram", "circuit", "flow", "widget", "graph", "unknown"):
        s = design.illustrator_system(kind)
        assert len(s) < design.MAX_SYSTEM_CHARS < mr._MAX_SYSTEM_CHARS
    t = design.TOKENS
    assert t["text"] == "#F8FAFC" and t["text_bn"] == "#FBBF24" and t["muted"] == "#94A3B8" and t["accent"] == "#38BDF8"
    assert "Hind Siliguri" in t["font"]


# ---------------------------------------------------------------------------------------------
# job runner (WP2)
# ---------------------------------------------------------------------------------------------
import core.artifact.jobs as jobs  # noqa: E402
from actions import study_visualizer as sv  # noqa: E402
sv.save_study_artifact = None          # tool-side vault logging off in tests


@check("tool: async path returns < 400 ms; board gets status lines then exactly ONE render")
def _():
    player = StubPlayer()
    _install_stub_router(delay=3.0)
    t0 = time.perf_counter()
    msg = sv.execute({"title": "Heart structure", "kind": "diagram", "brief": "human heart, 4 chambers"}, player)
    dt = (time.perf_counter() - t0) * 1000
    assert dt < 400, f"execute took {dt:.0f} ms"
    assert "STARTED" in msg and "NOT displayed" in msg and "study_visual_canvas" not in msg
    assert not player.renders                                   # honesty: nothing shown yet
    assert wait_until(lambda: len(player.renders) == 1, 10)
    time.sleep(0.5)
    assert len(player.renders) == 1
    title, md, subject, reveal = player.renders[0]
    assert reveal == "all" and "```svg" in md and 'data-jv-generated="ai"' in md
    assert player.statuses and all("Traceback" not in s for _t, s in player.statuses)
    assert "২/৪" in player.statuses[0][1]


@check("jobs: a second submit mid-job drops the first result")
def _():
    player = StubPlayer()
    _install_stub_router(delay=1.2)
    runner = jobs.JobRunner()
    assert runner.submit({"title": "A figure", "kind": "diagram"}, player)[0] == "started"
    time.sleep(0.3)
    assert runner.submit({"title": "B figure", "kind": "diagram"}, player)[0] == "started"
    assert wait_until(lambda: any(r[0] == "B figure" for r in player.renders), 12)
    time.sleep(0.5)
    assert [r[0] for r in player.renders] == ["B figure"], [r[0] for r in player.renders]


@check("jobs: duplicate request while running starts no second job")
def _():
    player = StubPlayer()
    calls: list[str] = []
    _install_stub_router(delay=1.0, calls=calls)
    runner = jobs.JobRunner()
    assert runner.submit({"title": "Cell  structure!", "kind": "diagram"}, player)[0] == "started"
    assert runner.submit({"title": "cell structure", "kind": "diagram"}, player)[0] == "duplicate"
    assert wait_until(lambda: len(player.renders) == 1, 8)
    time.sleep(0.4)
    assert calls == ["diagram_draw"], calls


@check("jobs: Study Mode turned off drops the result")
def _():
    player = StubPlayer()
    _install_stub_router(delay=1.0)
    runner = jobs.JobRunner()
    runner.submit({"title": "Neuron", "kind": "diagram"}, player)
    time.sleep(0.3)
    player.study_mode_active = False
    time.sleep(1.6)
    assert player.renders == []
    assert not any("আঁকা গেল না" in s for _t, s in player.statuses)       # silent: the student left Study Mode


@check("jobs: cancel drops the result")
def _():
    player = StubPlayer()
    _install_stub_router(delay=1.0)
    runner = jobs.JobRunner()
    runner.submit({"title": "DNA", "kind": "diagram"}, player)
    time.sleep(0.2)
    assert runner.cancel() is True
    time.sleep(1.5)
    assert player.renders == []


@check("jobs: a failing provider gives a calm status line, never a traceback")
def _():
    player = StubPlayer()
    _install_stub_router(raises=True)
    runner = jobs.JobRunner()
    runner.submit({"title": "Boom", "kind": "diagram"}, player)
    assert wait_until(lambda: any(s == jobs.FAIL_STATUS for _t, s in player.statuses), 6)
    assert player.renders == []
    assert all("Traceback" not in s and "RuntimeError" not in s for _t, s in player.statuses)


@check("jobs: 120 s budget -> calm timeout status (budget shortened for the test)")
def _():
    player = StubPlayer()
    _install_stub_router(delay=1.0)
    old = jobs.JOB_BUDGET_S
    jobs.JOB_BUDGET_S = 0.5
    try:
        runner = jobs.JobRunner()
        runner.submit({"title": "Slow figure", "kind": "diagram"}, player)
        assert wait_until(lambda: any(s == jobs.TIMEOUT_STATUS for _t, s in player.statuses), 6)
        assert player.renders == []
    finally:
        jobs.JOB_BUDGET_S = old


@check("jobs: status lines are >= 400 ms apart")
def _():
    player = StubPlayer()
    job = jobs.Job(1, 0, {"title": "t"}, player, None, jobs.JobRunner())
    for n in (1, 2, 3, 4):
        job.stage(n)
    gaps = [b[0] - a[0] for a, b in zip(player.statuses, player.statuses[1:])]
    assert len(player.statuses) == 4 and all(g >= 0.39 for g in gaps), gaps


@check("jobs: spoken notice is off by default, once per job, never while a tool is in flight")
def _():
    class Owner:
        _tool_in_flight = False
        _is_speaking = False
        _last_user_speech = 0.0

        def __init__(self):
            self.said = []

        def speak(self, text):
            self.said.append(text)

    import memory.config_manager as config_manager
    orig = config_manager.get_visual_announce_done
    try:
        o = Owner()
        job = jobs.Job(1, 0, {"title": "t"}, StubPlayer(), o.speak, jobs.JobRunner())
        config_manager.get_visual_announce_done = lambda: False
        jobs._maybe_announce(job); assert o.said == []
        config_manager.get_visual_announce_done = lambda: True
        o._tool_in_flight = True
        jobs._maybe_announce(job); assert o.said == []
        o._tool_in_flight = False; o._is_speaking = True
        jobs._maybe_announce(job); assert o.said == []
        o._is_speaking = False
        jobs._maybe_announce(job); jobs._maybe_announce(job)
        assert len(o.said) == 1 and o.said[0].startswith("[SYS]")
    finally:
        config_manager.get_visual_announce_done = orig
    assert config_manager.get_visual_announce_done() is False             # default


# ---------------------------------------------------------------------------------------------
# the lean tool (WP1)
# ---------------------------------------------------------------------------------------------
@check("tool: ready-made template renders instantly with zero LLM calls")
def _():
    player = StubPlayer()
    calls: list[str] = []
    _install_stub_router(calls=calls)
    t0 = time.perf_counter()
    msg = sv.execute({"title": "ছত্রাক / mushroom structure", "template_id": "agaricus_mushroom"}, player)
    assert (time.perf_counter() - t0) < 0.4
    assert "is now displayed" in msg and calls == [] and len(player.renders) == 1
    assert "```svg" in player.renders[0][1]


@check("tool: graph spec renders instantly; bad JSON is an honest error")
def _():
    player = StubPlayer()
    spec = json.dumps({"x_range": [-5, 5], "y_range": [-5, 5], "functions": [{"expr": "x^2-4", "label": "y=x^2-4"}]})
    msg = sv.execute({"title": "y = x^2 - 4", "kind": "graph", "coord_spec_json": spec}, player)
    assert "```coord" in player.renders[0][1] and "is now displayed" in msg
    p2 = StubPlayer()
    bad = sv.execute({"title": "g", "kind": "graph", "coord_spec_json": "{nope"}, p2)
    assert "NOTHING was drawn" in bad and p2.renders == []


@check("tool: legacy svg_markup is sanitized and shown; hostile parts removed")
def _():
    player = StubPlayer()
    svg = _WRAP.format('<rect width="9" height="9" fill="red" onclick="x()"/><script>alert(1)</script>')
    sv.execute({"title": "legacy", "svg_markup": svg}, player)
    md = player.renders[0][1]
    assert "<rect" in md and "<script" not in md and "onclick" not in md


@check("tool: unknown template id without a brief is an honest error")
def _():
    player = StubPlayer()
    msg = sv.execute({"title": "x", "template_id": "does_not_exist"}, player)
    assert "does not exist" in msg and "NOTHING was drawn" in msg and player.renders == []


@check("tool: widget / revise are honest 'not available yet' errors; cancel works")
def _():
    p = StubPlayer()
    assert "NOTHING was drawn" in sv.execute({"title": "slider", "kind": "widget", "brief": "ohm slider"}, p)
    assert "NOTHING was drawn" in sv.execute({"title": "x", "action": "revise", "instruction": "make R2 10"}, p)
    cancel_msg = sv.execute({"title": "x", "action": "cancel"}, p).lower()
    assert "cancel" in cancel_msg or "nothing" in cancel_msg
    assert p.renders == []


@check("tool: no board -> honest error, nothing started")
def _():
    msg = sv.execute({"title": "heart", "brief": "heart"}, None)
    assert "NOT displayed" in msg or "nothing can be drawn" in msg


@check("tool: registry contract (TOOL dict, OBJECT params, handler, execute/run)")
def _():
    T = sv.TOOL
    assert T["name"] == "study_visualizer" and T["parameters"]["type"] == "OBJECT" and callable(T["handler"])
    assert sv.run is sv.execute and T["parameters"]["required"] == ["title"]


@check("declaration: size down >= 40 % vs the original")
def _():
    d = {k: v for k, v in sv.TOOL.items() if k != "handler"}
    size = len(json.dumps(d, ensure_ascii=False))
    assert size <= BASELINE_DECL_CHARS * 0.6, f"{size} chars vs baseline {BASELINE_DECL_CHARS}"
    print(f"      declaration {BASELINE_DECL_CHARS} -> {size} chars ({100 - size * 100 // BASELINE_DECL_CHARS}% smaller)")
    assert "svg_markup" not in json.dumps(d) and "DRAWING STANDARD" not in json.dumps(d)
    listed = re.findall(r"(\w+)=[^;.]+", d["description"].split("Ready-made figures", 1)[-1]) if "Ready-made" in d["description"] else []
    assert len(listed) <= 12


@check("prompt cleanup: no stale path or 760x420 viewBox anywhere; Appendix A present")
def _():
    prompt = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
    assert "VISUALS (`study_visualizer`)" in prompt and "Do NOT write SVG or HTML yourself" in prompt
    hits = []
    for p in ROOT.rglob("*"):
        if p.suffix.lower() not in (".py", ".txt", ".html", ".md", ".json") or "__pycache__" in p.parts:
            continue
        if p.name == "test_artifact_studio.py" or "data" in p.relative_to(ROOT).parts[:1]:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if "study_visual_canvas" in txt:
            hits.append(f"{p.relative_to(ROOT)}: study_visual_canvas")
        if p.name in ("prompt.txt", "study_visualizer.py") and "0 0 760 420" in txt:
            hits.append(f"{p.relative_to(ROOT)}: 760x420")
    assert not hits, hits


@check("board: allow-list mirrors the Python sanitizer; forbidden tags absent")
def _():
    from core.artifact.sanitize import ALLOWED_TAGS
    html = (ROOT / "runtime" / "study_board.html").read_text(encoding="utf-8")
    block = html.split("const SVG_TAGS = new Set([", 1)[1].split("]);", 1)[0]
    js_tags = set(re.findall(r'"([a-z]+)"', block))
    assert js_tags == set(ALLOWED_TAGS), (js_tags ^ set(ALLOWED_TAGS))
    for bad in ("feimage", "image", "foreignobject", "script", "style", "a", "animate", "set"):
        assert bad not in js_tags
    assert "max-width: 1000px" in html and "gen-chip" in html


@check("line endings: edited files keep their original style")
def _():
    expect = {"core/model_router.py": "crlf", "core/prompt.txt": "crlf", "memory/config_manager.py": "crlf",
              "runtime/study_board.html": "crlf", "actions/study_visualizer.py": "lf"}
    for rel, style in expect.items():
        b = (ROOT / rel).read_bytes()
        crlf, lf = b.count(b"\r\n"), b.count(b"\n") - b.count(b"\r\n")
        assert (lf == 0) if style == "crlf" else (crlf == 0), (rel, crlf, lf)


# ---------------------------------------------------------------------------------------------
failed = [r for r in _results if not r[1]]
print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
sys.exit(1 if failed else 0)
