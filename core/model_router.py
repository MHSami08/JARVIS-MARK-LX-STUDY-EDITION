"""
Model router + key health engine for JARVIS one-shot calls (actions/*.py & study_mentor.py).

Includes automatic fallback to `core.gemini` ladder (including throwaway Live
sessions) if all direct REST keys/models in the router chain are exhausted or
unconfigured, guaranteeing that Study Mode never fails when a Gemini key exists.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent
_KEY_FILE = _BASE / "config" / "api_keys.json"

# Ordered fastest-first, and *non-thinking* only.
#
# `deepseek/deepseek-r1:free` used to sit in `coding` and `study`. R1 is a
# reasoning model: it spends tokens thinking before it emits anything, so it
# was the slowest rung on the latency path these actions actually wait on. It
# was also a wasted round trip in the common case, because the rungs above it
# were already answering. Removed rather than demoted — a rung that is never
# reached still costs a failed connection every time it is, and a thinking
# model is the wrong tool for "open WhatsApp and send this".
#
# Duplicates are gone too: `study` listed `gemini-2.5-flash` twice, so a chain
# that reached the first one re-tried the same model later for nothing.
DEFAULT_RULES: dict[str, list[tuple[str, str]]] = {
    "coding": [
        ("groq", "llama-3.1-8b-instant"),
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-2.5-flash-lite"),
        ("gemini", "gemini-2.5-flash"),
        ("gemini", "gemini-3.1-flash-lite"),
    ],
    "writing": [
        ("groq", "llama-3.1-8b-instant"),
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-2.5-flash-lite"),
        ("gemini", "gemini-2.5-flash"),
        ("gemini", "gemini-3.1-flash-lite"),
    ],
    "fast": [
        ("groq", "llama-3.1-8b-instant"),
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-2.5-flash-lite"),
        ("gemini", "gemini-3.1-flash-lite"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ],
    "study": [
        ("gemini", "gemini-2.5-flash-lite"),
        ("groq", "llama-3.1-8b-instant"),
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-2.5-flash"),
        ("gemini", "gemini-3.1-flash-lite"),
    ],
    "study_fast": [
        ("gemini", "gemini-2.5-flash-lite"),
        ("groq", "llama-3.1-8b-instant"),
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-2.5-flash"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ],
    # Artifact Studio (core/artifact). Only model ids already used above.
    # `diagram_plan`: short, JSON-capable, cheap-first. `diagram_draw`: STRONGEST first -
    # a figure is drawn once, so quality beats the few seconds a lite model would save.
    "diagram_plan": [
        ("gemini", "gemini-2.5-flash-lite"),
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-2.5-flash"),
        ("gemini", "gemini-3.1-flash-lite"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ],
    "diagram_draw": [
        ("gemini", "gemini-2.5-flash"),
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-3.1-flash-lite"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
        ("gemini", "gemini-2.5-flash-lite"),
    ],
}

# Optional per-task generation limits. Tasks not listed here send exactly what they always
# sent. `thinking_budget` is dropped and the call retried if the model/SDK rejects it.
_GEN_CONFIG: dict[str, dict] = {
    "diagram_plan": {"max_output_tokens": 2048, "thinking_budget": 0},
    "diagram_draw": {"max_output_tokens": 16384, "thinking_budget": 1024},
}

_ENDPOINTS = {
    "openrouter": "https:" + "//openrouter.ai/api/v1",
    "groq": "https:" + "//api.groq.com/openai/v1",
}

_TIMEOUT = 30.0
_BASE_COOLDOWN = 60.0
_MAX_COOLDOWN = 3600.0
_AUTH_COOLDOWN = 600.0
_TRANSIENT_COOLDOWN = 30.0
_GONE_COOLDOWN = 1800.0

# ── transient-failure retry + payload guard ──────────────────────────────────
# A 500/502/503/504 is the provider's own hiccup, not our request: the rung is
# usually healthy seconds later, and the previous behaviour — rotate the key and
# move to the next provider on the first one — spent a whole provider's latency
# budget on a blip. Three attempts with exponential backoff (1s, 2s, 4s) ride it
# out; only then does the failure count against the key and the chain move on.
_RETRY_ATTEMPTS   = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY  = 8.0
_RETRYABLE_STATUS = (408, 500, 502, 503, 504)

# Payload guard. The context window of the smallest rung on every ladder is
# well above this; the point is that a caller handing us a whole document
# cannot produce the "malformed / too large" 400/500 that started this. ~4
# chars/token, so these are ~15k and ~5k tokens respectively.
_MAX_PROMPT_CHARS = 60_000
_MAX_SYSTEM_CHARS = 20_000


@dataclass
class KeyRecord:
    key: str
    fails: int = 0
    cooldown_until: float = 0.0
    last_used: float = 0.0
    seq: int = 0
    number: int = 0                 # 1-based position in the pool = Key #1, Key #2…

    @property
    def label(self) -> str:
        """'Key #2 (gsk_…4f1a)' — the number is what a person can act on.

        With several keys stacked, 'Key #2 is resting 300s' names something the
        user can find in the settings list; the bare masked fragment does not,
        because the same fragment appears in no other place they can see.
        """
        k = self.key
        masked = k if len(k) <= 12 else f"{k[:4]}...{k[-4:]}"
        return f"Key #{self.number} ({masked})" if self.number else masked


class _Pool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self.records: list[KeyRecord] = []

    def sync(self, keys: list[str]) -> None:
        with self._lock:
            # Keep each record bound to the key STRING, so reordering the list in
            # the settings UI renumbers the keys without ever handing one key's
            # cooldown to another.
            old = {r.key: r for r in self.records}
            self.records = []
            for i, k in enumerate(keys):
                rec = old.get(k) or KeyRecord(k)
                rec.number = i + 1
                self.records.append(rec)

    def acquire(self) -> KeyRecord | None:
        now = time.monotonic()
        with self._lock:
            ready = [r for r in self.records if r.cooldown_until <= now]
            if not ready:
                return None
            rec = min(ready, key=lambda r: r.seq)
            self._counter += 1
            rec.seq, rec.last_used = self._counter, now
            return rec

    def ok(self, rec: KeyRecord) -> None:
        with self._lock:
            rec.fails = 0
            rec.cooldown_until = 0.0

    def fail(self, rec: KeyRecord, kind: str) -> float:
        with self._lock:
            if kind == "quota":
                rec.fails += 1
                secs = min(_BASE_COOLDOWN * 2 ** (rec.fails - 1), _MAX_COOLDOWN)
            elif kind == "auth":
                rec.fails += 1
                secs = _AUTH_COOLDOWN
            elif kind == "transient":
                rec.fails += 1
                secs = _TRANSIENT_COOLDOWN
            else:
                return 0.0
            rec.cooldown_until = time.monotonic() + secs
            return secs

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            return [{"key": r.label, "fails": r.fails,
                     "cooling_s": max(0, round(r.cooldown_until - now))}
                    for r in self.records]


_pools = {"gemini": _Pool(), "openrouter": _Pool(), "groq": _Pool()}
_rules: dict[str, list[tuple[str, str]]] = dict(DEFAULT_RULES)
_cfg_lock = threading.Lock()
_cfg_mtime = -1.0
_state_lock = threading.Lock()
_resting_models: dict[tuple[str, str], float] = {}


def _as_list(*vals) -> list[str]:
    out: list[str] = []
    for v in vals:
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            for piece in str(item or "").replace(",", " ").split():
                if piece not in out:
                    out.append(piece)
    return out


def _norm_step(step) -> tuple[str, str] | None:
    if isinstance(step, str):
        prov, _, mdl = step.partition(":")
    elif isinstance(step, dict):
        prov, mdl = step.get("provider", ""), step.get("model", "")
    else:
        return None
    prov, mdl = str(prov).strip().lower(), str(mdl).strip()
    return (prov, mdl) if prov in _pools and mdl else None


def _refresh() -> None:
    global _cfg_mtime, _rules
    with _cfg_lock:
        try:
            mtime = _KEY_FILE.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime == _cfg_mtime:
            return
        try:
            data = json.loads(_KEY_FILE.read_text(encoding="utf-8")) if mtime else {}
        except Exception as e:
            print(f"[Router] cannot read {_KEY_FILE.name}: {e}")
            return
        _cfg_mtime = mtime
        _pools["gemini"].sync(_as_list(data.get("gemini_api_key"), data.get("gemini_api_keys")))
        _pools["openrouter"].sync(_as_list(data.get("openrouter_api_key"), data.get("openrouter_api_keys")))
        _pools["groq"].sync(_as_list(data.get("groq_api_key"), data.get("groq_api_keys")))
        rules = dict(DEFAULT_RULES)
        user = data.get("routing_rules")
        if isinstance(user, dict):
            for task, chain in user.items():
                # Keep study/study_fast on reliable defaults unless explicitly customized
                if task in ("study", "study_fast"):
                    steps = [s for s in map(_norm_step, chain if isinstance(chain, list) else []) if s]
                    if steps:
                        rules[str(task)] = steps
                else:
                    steps = [s for s in map(_norm_step, chain if isinstance(chain, list) else []) if s]
                    if steps:
                        rules[str(task)] = steps
        _rules = rules


class _HTTPFail(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"{status} {body[:200]}")
        self.status = status


def _classify(e: BaseException) -> str:
    s = str(e).lower()
    status = getattr(e, "status", None) or getattr(e, "code", None)
    if not isinstance(status, int):
        m = re.match(r"\s*(\d{3})\b", s)
        status = int(m.group(1)) if m else None
    if status in (429, 402) or any(t in s for t in ("resource_exhausted", "quota", "rate limit", "rate_limit")):
        return "quota"
    if status in (401, 403) or any(t in s for t in ("api key not valid", "api_key_invalid", "invalid api key", "permission_denied", "unauthorized")):
        return "auth"
    if status == 404 or any(t in s for t in ("not found", "no endpoints", "model_not_found", "not a valid model")):
        return "gone"
    if status in (408, 500, 502, 503, 504) or isinstance(e, (TimeoutError, OSError)) or any(t in s for t in ("unavailable", "deadline", "timed out", "timeout")):
        return "transient"
    return "other"


def _sanitize_text(text, limit: int) -> str:
    """Make a string safe to hand a provider: valid UTF-8, no NULs, bounded.

    Three separate failure shapes collapse into one pass here:
      * lone surrogates and bad byte sequences (json.dumps succeeds, then
        `.encode("utf-8")` raises mid-send) — dropped by a round trip;
      * embedded NUL / control bytes some APIs reject outright;
      * an oversized body — clamped head+tail, keeping the opening (where
        instructions and the start of a document live) and the ending.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    # A decode round trip is the cheapest way to strip surrogates and any
    # other sequence that would blow up at encode time.
    text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
    text = text.replace("\x00", "")
    if limit and len(text) > limit:
        head = limit * 3 // 4
        tail = limit - head
        text = text[:head] + "\n… [truncated] …\n" + text[-tail:]
    return text


def _sanitize_payload(prompt: str, system: str) -> tuple[str, str]:
    """(prompt, system) clamped and cleaned before any provider sees them."""
    return (_sanitize_text(prompt, _MAX_PROMPT_CHARS),
            _sanitize_text(system, _MAX_SYSTEM_CHARS))


def _is_retryable(e: BaseException) -> bool:
    """True when re-sending the same request to the same rung is worth it.

    Only the provider's own transient faults qualify: 5xx, the request timeout
    codes, and the socket-level timeouts urllib raises as OSError. A 429 is NOT
    retried here — that is a quota fact, and the ladder's job is to move to the
    next key, not to wait on the same one.
    """
    if isinstance(e, _HTTPFail):
        return e.status in _RETRYABLE_STATUS
    if isinstance(e, (TimeoutError, OSError)):
        return True
    return _classify(e) == "transient"


def _retry_delay(attempt: int, deadline: float | None) -> float:
    """Exponential backoff, capped, and never allowed to overrun the budget."""
    delay = min(_RETRY_BASE_DELAY * (2 ** attempt), _RETRY_MAX_DELAY)
    if deadline is not None:
        room = deadline - time.monotonic() - 1.0
        if room <= 0:
            return 0.0
        delay = min(delay, room)
    return delay


def _attempt_with_retry(provider: str, model: str, send, *,
                        attempts: int = _RETRY_ATTEMPTS,
                        deadline: float | None = None) -> str:
    """Run `send()` with backoff on transient faults; raise the last failure.

    `send` is a zero-arg callable so the same helper serves both the
    OpenAI-compatible adapters and the Gemini SDK path. Non-retryable errors
    (auth, quota, gone) are re-raised immediately for the caller's classifier.
    """
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return send()
        except BaseException as e:                    # noqa: BLE001 - re-raised below
            last = e
            if not _is_retryable(e) or i == attempts - 1:
                raise
            delay = _retry_delay(i, deadline)
            if delay <= 0:
                raise
            status = getattr(e, "status", None) or type(e).__name__
            print(f"[Router] {provider}/{model}: transient ({status}) — "
                  f"retry {i + 1}/{attempts - 1} in {delay:.1f}s")
            time.sleep(delay)
    if last is not None:
        raise last
    return ""


def _raw_config() -> dict:
    """The key file as-is — where a configured fallback provider lives. Never raises."""
    try:
        return json.loads(_KEY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _configured_fallback() -> list[tuple[str, str]]:
    """An explicit last-resort provider from config, if the user set one.

    Keys: "fallback_provider" (groq | openrouter | gemini) and "fallback_model".
    Empty when unset or invalid — the built-in chains already provide fallback,
    this only lets an install pin a specific rung after them.
    """
    cfg = _raw_config()
    provider = str(cfg.get("fallback_provider") or "").strip().lower()
    model = str(cfg.get("fallback_model") or "").strip()
    if provider in _pools and model:
        return [(provider, model)]
    return []


def _model_resting(provider: str, model: str) -> bool:
    with _state_lock:
        until = _resting_models.get((provider, model), 0.0)
        if until and time.monotonic() < until:
            return True
        _resting_models.pop((provider, model), None)
        return False


def _rest_model(provider: str, model: str) -> None:
    with _state_lock:
        _resting_models[(provider, model)] = time.monotonic() + _GONE_COOLDOWN


def _gemini(key: str, model: str, prompt: str, system: str, json_mode: bool,
            timeout: float | None = None, gen: dict | None = None) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=int(max(10.0, timeout or _TIMEOUT) * 1000)))
    extra: dict = {}
    if gen:
        if gen.get("max_output_tokens"):
            extra["max_output_tokens"] = int(gen["max_output_tokens"])
        if gen.get("thinking_budget") is not None:
            try:
                extra["thinking_config"] = types.ThinkingConfig(thinking_budget=int(gen["thinking_budget"]))
            except Exception:
                pass   # older SDK: just skip it

    def _generate(**more):
        cfg = types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json" if json_mode else None,
            **more)
        return client.models.generate_content(model=model, contents=prompt, config=cfg)

    try:
        res = _generate(**extra)
    except Exception as e:
        # A model that rejects thinking_config gets ONE retry without it. Any other error
        # (429, 5xx ...) is raised untouched, so key rotation behaves exactly as before.
        if "thinking_config" in extra and "think" in str(e).lower():
            extra.pop("thinking_config", None)
            res = _generate(**extra)
        else:
            raise
    try:
        return res.text or ""
    except Exception:
        if getattr(res, "candidates", None) and res.candidates:
            parts = getattr(res.candidates[0].content, "parts", [])
            texts = [getattr(p, "text", "") for p in parts if getattr(p, "text", None)]
            return "".join(texts).strip()
        return ""


def _openai_body(model: str, messages: list, gen: dict | None) -> dict:
    body: dict = {"model": model, "messages": messages}
    if gen and gen.get("max_output_tokens"):
        body["max_tokens"] = int(gen["max_output_tokens"])
    return body


def _openai_compat(provider: str, key: str, model: str, prompt: str,
                   system: str, json_mode: bool, timeout: float | None = None,
                   gen: dict | None = None) -> str:
    if json_mode:
        system = (system + "\n" if system else "") + \
            "Reply with valid JSON only. No markdown fences, no commentary."
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    req = urllib.request.Request(
        _ENDPOINTS[provider] + "/chat/completions",
        data=json.dumps(_openai_body(model, messages, gen)).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": "jarvis-router/1.0",
                 "X-Title": "JARVIS"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _HTTPFail(e.code, e.read().decode("utf-8", "ignore")) from None
    if not data.get("choices"):
        err = data.get("error") or {}
        raise _HTTPFail(int(err.get("code") or 502), str(err.get("message") or data)[:200])
    text = data["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


_ADAPTERS = {
    "gemini": _gemini,
    "openrouter": lambda *a: _openai_compat("openrouter", *a),
    "groq": lambda *a: _openai_compat("groq", *a),
}


def _chain(task: str, model: str | None, prompt: str = "") -> list[tuple[str, str]]:
    steps = list(_rules.get(task) or DEFAULT_RULES.get(task) or _rules["fast"])
    if model:
        prov = "gemini" if model.startswith(("gemini", "models/")) \
            else "openrouter" if "/" in model else "groq"
        steps = [(prov, model)] + [s for s in steps if s != (prov, model)]
    return steps


def _gemini_ladder_fallback(prompt: str, system: str, json_mode: bool, timeout: float | None) -> str:
    """Ultimate safety net: use core.gemini's full ladder (which includes the
    Live API one-shot fallback whose quota never runs dry on the free tier)."""
    try:
        from core import gemini as _g
        from google.genai import types
        full_prompt = prompt
        if json_mode:
            system = (system + "\n" if system else "") + "Output ONLY valid JSON. Do not include markdown fences or extra text."
        cfg = types.GenerateContentConfig(system_instruction=system or None)
        ms = int(max(15.0, timeout or 35.0) * 1000)
        resp = _g.call(full_prompt, tier=_g.FAST, config=cfg, timeout_ms=ms)
        if resp and getattr(resp, "text", None):
            return resp.text.strip()
    except Exception as e:
        print(f"[Router] Gemini ladder fallback failed: {e}")
    return ""


QUIZ_INTEGRITY_WARNING = "এটি একটি পরীক্ষা চলছে। পরীক্ষার সময় আমি সরাসরি উত্তর বা সমাধান বলে দিতে পারব না।"

QUIZ_INTEGRITY_SYSTEM_INSTRUCTION = (
    "\n[STRICT QUIZ INTEGRITY & ANTI-CHEATING GUARD]\n"
    "If an active quiz session or examination is in progress and the user asks for direct answers, "
    "solutions, choices, or hints to solve active quiz questions, you MUST strictly refuse with "
    "this exact academic warning:\n"
    f'"{QUIZ_INTEGRITY_WARNING}"\n'
    "Never leak or confirm the correct option while the quiz is active."
)


def _run(prompt: str, task: str, system: str, model: str | None,
         json_mode: bool = False, accept=None,
         timeout: float | None = None, budget: float | None = None) -> str:
    try:
        _refresh()

        # Quiz integrity check
        try:
            from actions import study_quiz
            quiz_on = hasattr(study_quiz, "is_quiz_active") and study_quiz.is_quiz_active()
        except Exception:
            quiz_on = False

        if quiz_on:
            p_lower = (prompt or "").lower()
            if any(w in p_lower for w in ["উত্তর কি", "সঠিক উত্তর", "উত্তর বলে দাও", "answer of", "solve this quiz", "tell me the answer"]):
                return QUIZ_INTEGRITY_WARNING

        if (quiz_on or str(task or "").startswith("study")) and QUIZ_INTEGRITY_WARNING not in (system or ""):
            system = (system + "\n" if system else "") + QUIZ_INTEGRITY_SYSTEM_INSTRUCTION

        # Payload guard — clean and clamp before anything is serialised. A
        # malformed body (lone surrogates, NULs) or one over the rung's limit
        # is itself a cause of a 4xx/5xx, so this runs before the chain, not
        # after a failure.
        prompt, system = _sanitize_payload(prompt, system)

        deadline = time.monotonic() + budget if budget else None
        gen = _GEN_CONFIG.get(task) if isinstance(task, str) else None
        for provider, mdl in _chain(task if isinstance(task, str) else "fast", model, prompt=prompt):
            pool = _pools[provider]
            if not pool.records or _model_resting(provider, mdl):
                continue
            for _ in range(max(1, len(pool.records))):
                if deadline is not None and deadline - time.monotonic() < 5:
                    break
                rec = pool.acquire()
                if rec is None:
                    break
                t = timeout
                if deadline is not None:
                    t = min(timeout or _TIMEOUT, deadline - time.monotonic())
                try:
                    out = _attempt_with_retry(
                        provider, mdl,
                        lambda p=provider, m=mdl, k=rec.key, to=t:
                            (_ADAPTERS[p](k, m, prompt, system, json_mode, to, gen) or "").strip(),
                        deadline=deadline)
                except Exception as e:
                    kind = _classify(e)
                    secs = pool.fail(rec, kind)
                    print(f"[Router] {provider}/{mdl} @ {rec.label}: {kind}"
                          + (f", key rests {secs:.0f}s" if secs else "")
                          + f" ({str(e)[:100]})")
                    if kind == "gone":
                        _rest_model(provider, mdl)
                        break
                    if kind == "other":
                        break
                    continue
                pool.ok(rec)
                if out and (accept is None or accept(out)):
                    return out
                print(f"[Router] {provider}/{mdl}: empty or unusable reply, moving on")
                break

        # Provider fallback: an explicit last-resort rung from config, if the
        # user pinned one. Tried before the Gemini ladder so a configured
        # provider is honoured even when Gemini still has quota.
        for provider, mdl in _configured_fallback():
            pool = _pools[provider]
            if not pool.records or _model_resting(provider, mdl):
                continue
            rec = pool.acquire()
            if rec is None:
                continue
            try:
                out = _attempt_with_retry(
                    provider, mdl,
                    lambda p=provider, m=mdl, k=rec.key:
                        (_ADAPTERS[p](k, m, prompt, system, json_mode, timeout, gen) or "").strip(),
                    deadline=deadline)
            except Exception as e:
                secs = pool.fail(rec, _classify(e))
                print(f"[Router] configured fallback {provider}/{mdl} @ {rec.label}: "
                      f"{_classify(e)}" + (f", key rests {secs:.0f}s" if secs else ""))
                continue
            pool.ok(rec)
            if out and (accept is None or accept(out)):
                return out

        # If all direct router steps failed/exhausted, fall back to core.gemini ladder (Live + all flash models)
        print("[Router] Direct chain exhausted -> trying core.gemini ladder fallback...")
        out = _gemini_ladder_fallback(prompt, system, json_mode, timeout)
        if out and (accept is None or accept(out)):
            return out
    except Exception as e:
        print(f"[Router] unexpected: {type(e).__name__}: {str(e)[:140]}")
    return ""


_LATEX_ESC_RX = re.compile(r'(?<!\\)\\(frac|tan|theta|tau|text|times|to|beta|bar|binom|begin|bf|rho|right|sqrt|sin|sec|sum|sigma|alpha|approx|Delta|delta|varepsilon|epsilon|pi|phi|omega|Omega|infty|int|left|lim|ln|log|cdot|pm|neq|nu|nabla|geq|leq|vec|hat)')

def _parse(raw: str):
    if not raw:
        return None
    # Protect common LaTeX commands from being eaten as JSON \f, \t, \b, \r, \n escapes
    cleaned = _LATEX_ESC_RX.sub(r'\\\\\1', raw)
    opens = sorted((cleaned.find(a), a, b) for a, b in (("{", "}"), ("[", "]")) if a in cleaned)
    for i, _a, b in opens:
        j = cleaned.rfind(b)
        if i < j:
            chunk = cleaned[i:j + 1]
            for candidate in (chunk, raw[raw.find(_a):raw.rfind(b) + 1]):
                try:
                    return json.loads(candidate)
                except ValueError:
                    continue
    return None


def call(prompt: str, task: str = "fast", system: str = "", model: str = None,
         timeout: float = None, budget: float = None, accept=None) -> str:
    # `accept(text) -> bool`: a rung whose reply fails it is skipped and the next rung is
    # tried (default None = old behaviour).
    return _run(prompt, task, system, model, timeout=timeout, budget=budget, accept=accept)


def as_json(prompt: str, task: str = "fast", system: str = "") -> dict:
    raw = _run(prompt, task, system, None, json_mode=True,
               accept=lambda t: _parse(t) is not None)
    val = _parse(raw) if raw else None
    if isinstance(val, dict):
        return val
    return {"items": val} if isinstance(val, list) else {}


def status() -> dict:
    _refresh()
    return {name: pool.snapshot() for name, pool in _pools.items()}


def key_status(provider: str = "groq") -> str:
    """One line naming every key of a provider by number and state.

    Reads the very records the router rotates on, so the settings screen and the
    log can never disagree about which key number is in use. The key that the
    next call will pick is marked 'active'; resting keys carry their seconds.
    """
    _refresh()
    pool = _pools.get(provider)
    rows = pool.snapshot() if pool else []
    if not rows:
        return f"no {provider} keys configured"
    first_ready = next((r["key"] for r in rows if not r["cooling_s"]), None)
    return " | ".join(
        f"{r['key']} cooling {r['cooling_s']}s" if r["cooling_s"]
        else f"{r['key']} {'active' if r['key'] == first_ready else 'idle'}"
        for r in rows
    )


def fallback_text(contents, config=None, raw_cfg=None, timeout_s: float = 30.0) -> str:
    system = ""
    if config is not None:
        system = getattr(config, "system_instruction", None) or \
            (config.get("system_instruction") if isinstance(config, dict) else "") or ""

    prompt = ""
    if isinstance(contents, str):
        prompt = contents
    elif isinstance(contents, (list, tuple)):
        parts = []
        for c in contents:
            if isinstance(c, str):
                parts.append(c)
            elif hasattr(c, "text") and c.text:
                parts.append(c.text)
        prompt = "\n".join(parts)
    else:
        prompt = str(contents)

    _refresh()
    # Same rule as the rules table: no thinking models. R1 was the last rung
    # here, and a reasoning model that only runs when everything else is down is
    # the worst place to put one — that is exactly the moment a 40-second
    # think-before-answer is least affordable.
    fallback_steps = [
        ("groq", "llama-3.1-8b-instant"),
        ("groq", "llama-3.3-70b-versatile"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ] + _configured_fallback()
    # Same payload guard as the main path: this is the last line of defence, so
    # a malformed or oversized body would fail here too and take the feature out.
    prompt, system = _sanitize_payload(prompt, system)
    for provider, mdl in fallback_steps:
        pool = _pools[provider]
        if not pool.records or _model_resting(provider, mdl):
            continue
        rec = pool.acquire()
        if rec is None:
            continue
        try:
            out = _attempt_with_retry(
                provider, mdl,
                lambda p=provider, m=mdl, k=rec.key:
                    (_ADAPTERS[p](k, m, prompt, str(system), False) or "").strip(),
                attempts=2)
            if out:
                pool.ok(rec)
                return out
        except Exception as e:
            pool.fail(rec, _classify(e))
            continue

    return ""