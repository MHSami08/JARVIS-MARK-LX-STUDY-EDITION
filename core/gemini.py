"""
One place where the assistant's one-shot Gemini calls are made.

WHY THIS EXISTS
    The live conversation runs on the Live API and is not what this file is
    about. Everything else — reading a screenshot, parsing a flight page,
    turning a request into a shell command, working out which WhatsApp button
    answers a call — was a separate `genai.Client(...)` built at the point of
    use, with the model name written inline. Sixteen files did it, twenty-six
    times, and not one of them set a timeout.

    That is not tidiness, it is three real faults:

    NO TIMEOUT.  The SDK waits forever by default. `gemini-flash-latest` spent
    an afternoon returning 504 DEADLINE_EXCEEDED, and every one of those calls
    became an unbounded hang — measured at ten seconds of silence while a phone
    rang, and worse elsewhere, because nothing was there to give up.

    NO FALLBACK.  One hardcoded alias meant that when that alias was unwell,
    the feature was simply gone. A ladder costs nothing when the first rung
    works and saves the feature when it does not.

    NO SINGLE PLACE TO CHANGE.  A new model release meant editing sixteen files
    and hoping none were missed.
THE LIVE MODEL DOES THIS WORK, AND IT LEADS THE LADDER
    Not the user's conversation — a separate, throwaway session per call, so
    nothing a plugin asks is ever heard by the person at the microphone.

    It leads because of quota. This is a voice assistant; the Live API is the
    dependency it already has, and it draws on a different pool from the text
    models. On the free tier it is the TEXT pool that runs out, and when it does
    every side call fails and the feature behind it dies with it. Putting Live
    first means ordinary use stops spending the pool that runs dry.

    The reply arrives through output_transcription, because these models refuse
    response_modalities=["TEXT"] with a 1007 — they only speak. That sounds
    fatal for structured output and is not: the transcription is the model's own
    text of what it said, and it returned "Mum ❤ click here for contact info",
    indented Python inside markdown fences, and src/utils/helpers_v2.py
    character for character.

    It cannot carry grounding metadata, so grounded web search stays on REST.

THE LADDER, MEASURED
    Live, one throwaway session:
        connect                     0.24s
        short structured JSON       1.7 - 2.8s
        2300 characters of code     3.39s, not truncated
        three concurrent sessions   all fine, 4.77s wall clock
    REST text models, same prompt, same afternoon:
        gemini-2.5-flash-lite       0.76s   ...then 429, quota exhausted
        gemini-2.5-flash            0.80s   ...then 429
        gemini-flash-lite-latest    2.58s
        gemini-flash-latest         504, every time
    So REST is two to four times quicker while it lasts, and the whole point of
    the ladder is that it does not last. Pinned REST names sit behind Live;
    rolling `-latest` aliases sit behind those, because they were the ones
    having a bad day.

    Where the answer depends only on stable input, cache it and neither pool is
    touched twice: `plugins/_whatsapp_core.py` is the worked example — one
    request per WhatsApp language for the lifetime of the install.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import threading
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_KEY_FILE = _BASE / "config" / "api_keys.json"

# Ladders, tried left to right. Change a model HERE and the whole app follows.
FAST = "fast"      # short classification, extraction, one-line decisions
SMART = "smart"    # reasoning, generation, long documents, images
SEARCH = "search"  # grounded search — REST only, see below

# A rung that means "ask the Live model instead", through a short throwaway
# session rather than the REST text API.
#
# WHY IT LEADS
#     This is a voice assistant: the Live API is the dependency it already has,
#     and it draws on a DIFFERENT quota pool from the text models. On the free
#     tier the text pool is the one that runs out — an afternoon of ordinary use
#     exhausts it, and when it does, every one of these side calls fails and the
#     feature behind it dies. The Live pool is untouched by that.
#
# WHAT IT COSTS, MEASURED
#     connect                      0.24s
#     short structured JSON        1.7 - 2.8s   (REST: 0.76s)
#     2300 characters of code      3.39s, not truncated
#     three concurrent sessions    all fine, 4.77s wall clock
#     So it is two to four times slower than REST when REST is available, and
#     infinitely faster than REST when REST is out of quota.
#
# THE THING WORTH KNOWING
#     These models only speak — response_modalities=["TEXT"] is refused with a
#     1007. The reply comes back through output_transcription, which sounds like
#     it would mangle anything structured. It does not: it is the model's own
#     text of what it said, and it survived "Mum ❤ click here for contact info",
#     indented Python inside markdown fences, and src/utils/helpers_v2.py
#     character for character. That is what makes this usable at all.
#
#     What it cannot carry is grounding metadata, so grounded web search stays
#     on REST — see SEARCH.
LIVE = "live"

# Every model this key can reach, in the order to try them. A rung that runs
# out of quota is skipped for a while and the next one answers — which is the
# whole point: one model a day means one daily limit, a ladder means the sum of
# them. Names verified against the live model list rather than guessed.
# Order is measured, not assumed. Timed against this key, same prompt:
#     gemini-3.5-flash-lite      0.56s      gemini-2.5-flash        0.67s
#     gemini-3.1-flash-lite      0.60s      gemini-2.5-flash-lite   0.74s
#     gemini-flash-lite-latest   0.60s      gemini-3.5-flash        1.13s
#     gemini-3-flash-preview     504, after 14.7s of waiting
#     gemini-3.6-flash           504, after 12.0s
#     gemini-flash-latest        503 UNAVAILABLE
# The three that fail sit at the BOTTOM rather than being deleted: they are
# real quota when they are healthy, and a rung that is down is set aside by the
# cooldown after one attempt instead of being paid for on every call.
_LADDERS = {
    # LIVE sits directly behind the two REST rungs known to answer in well under
    # a second — `gemini-2.5-flash-lite` and `gemini-2.5-flash` — and ahead of
    # the rest.
    #
    # Not first: `_live_turn` is a whole session (fresh WebSocket, a synthesis
    # pass, the reply read back out of `output_transcription`), measured at two
    # to four times a REST round trip, and it used to put that cost in front of
    # every single tool call — which is what made multi-step actions take 8-12s.
    #
    # Not last either: the rungs below it are the slow and the known-flaky ones
    # (`gemini-3.5-flash` at 1.13s, and the 504/503 group), so a LIVE attempt
    # placed after them would only be reached after paying their timeouts.
    # Here it is the first real fallback when the fast REST rungs are out of
    # quota, with every existing model and backup key still behind it.
    FAST: ("gemini-2.5-flash-lite", "gemini-3.5-flash-lite",
           "gemini-3.1-flash-lite", "gemini-flash-lite-latest",
           "gemini-2.5-flash", LIVE,
           "gemini-3.5-flash",
           "gemini-3.6-flash", "gemini-3-flash-preview"),
    SMART: ("gemini-2.5-flash", "gemini-3.5-flash",
            "gemini-2.5-flash-lite", LIVE,
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-flash-latest"),
    # Grounded search needs response.candidates[...].grounding_metadata, which a
    # Live turn does not produce. REST only, and it says so rather than silently
    # returning an answer with no sources behind it.
    SEARCH: ("gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite",
             "gemini-flash-latest"),
}

# The conversation's own model, and ONE careful fallback behind it.
#
# Deliberately not a long ladder. The Live quota is not the one that runs out —
# the text models are — and Live models are not interchangeable the way text
# models are: they differ in which config fields they accept and in what they
# can do, so falling through a list of them risks connecting to something that
# behaves like a different assistant. The key also offers transcribe-live,
# live-translate and a robotics streaming model, none of which are assistants
# at all.
#
# So there are two: the current one, and the native-audio model this assistant
# used before it, which is known to work here. A mismatch in config is already
# survivable — the connect loop drops the tuning and proactive-audio fields and
# reconnects when the server rejects them.
LIVE_MODELS = (
    "models/gemini-3.1-flash-live-preview",
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
)

# The Live model to use for one-shot calls. main.py owns the real one; this is
# only the fallback for when this module is imported without it (tests).
_LIVE_FALLBACK = "models/gemini-3.1-flash-live-preview"

# How many one-shot Live sessions may exist at once.
#
# THE USER'S CONVERSATION OUTRANKS EVERY SIDE CALL.
# Nothing here can reach the microphone or the speaker — main.py has exactly one
# `.receive()` and it is bound to its own session, and audio only reaches the
# speaker through that one loop — so a side call cannot answer the user or talk
# over the reply. Verified alongside a live main session: four side calls fired
# while it was connected, each got its own answer, and the main session replied
# correctly both before and after with no errors.
#
# What a side call CAN do is take up a concurrent-session slot. That is the one
# way it could hurt the conversation, so it is capped, and a call that cannot
# get a slot quickly does not queue behind the others — it falls to the REST
# rung, which is what the ladder is for.
# Three, from the measurement: four side calls plus the conversation ran
# together without complaint, so three leaves the conversation a slot in hand
# while still covering any burst this app actually produces — tool calls run one
# after another, and the screen agent's loop is sequential.
_LIVE_SLOTS = threading.BoundedSemaphore(3)
_LIVE_SLOT_WAIT = 3.0

_ONE_SHOT_SYSTEM = (
    "You are a data-processing function, not an assistant and not in a "
    "conversation. There is no person listening to you. Produce exactly the "
    "output the request asks for and nothing else: no greeting, no "
    "acknowledgement, no 'Understood', no explanation, no closing remark, no "
    "restating of the question. If the request asks for JSON, emit only the "
    "JSON. If it asks for code, emit only the code. If it asks for one word, "
    "emit that one word. Preserve the exact spelling, punctuation, capitals "
    "and whitespace of anything you are asked to copy or return."
)

# Milliseconds. Not a preference: the API rejects anything under ten seconds
# with "Minimum allowed deadline is 10s", so this is the tightest bound it will
# accept. Callers with a long job (a whole document, a big image) pass more.
DEFAULT_TIMEOUT_MS = 10_000
MIN_TIMEOUT_MS = 10_000

_key_lock = threading.Lock()
_cached_keys: list[str] | None = None

# Keys that are known bad — rejected as invalid, or not permitted. Separate from
# the model cooldown below because it is a different fact with a different
# consequence: a model resting is skipped for one key, whereas a dead key is
# skipped for every model, and when there is no other key left the honest answer
# is to ask the user for one rather than to keep reconnecting into the same wall.
_dead_keys: dict[str, float] = {}
_dead_lock = threading.Lock()

# How long a rejected key is set aside before it is tried again. Short, because
# the usual cause is a typo the user is about to fix, and the fix is meant to be
# picked up without a restart.
_DEAD_KEY_SECONDS = 10 * 60

# A rung that answered 429 is out of quota, and on the free tier it will stay
# that way for a while. Retrying it on every single call is a wasted round trip
# in front of every request the assistant makes — measured on this key, the
# lite rung was 429ing continuously, so every call was paying for it before
# reaching the model that could actually answer. Remembering that for a few
# minutes turns the ladder from a cost into a saving.
_COOLDOWN_SECONDS = 300
# A model that does not exist, or that this key may not use, is not coming back
# in five minutes. Retrying it on every call is a wasted round trip in front of
# everything the assistant does, so it is set aside for the session instead.
_GONE_SECONDS = 6 * 60 * 60
# Overloaded or timing out. Usually passes, so a shorter rest than "gone" —
# but long enough that the fourteen-second wait is paid once, not repeatedly.
_UNAVAILABLE_SECONDS = 30 * 60
# Keyed (key, model). Quota is per key, so a 429 rests that pair only and the
# same model is still tried on the next key. key "" means every key: a model
# that is gone or timing out is down for all of them.
_cooldown: dict[tuple[str, str], float] = {}
_cool_lock = threading.Lock()


def _cool(model: str, seconds: float = _COOLDOWN_SECONDS, key: str = "") -> None:
    with _cool_lock:
        _cooldown[(key, model)] = time.monotonic() + seconds


def is_quota_error(err: str) -> bool:
    return "429" in err or "RESOURCE_EXHAUSTED" in err


def is_unavailable_error(err: str) -> bool:
    """The model is up but not answering — overloaded, or a deadline expired.

    Worth its own case because of what it costs: a 504 from one of these took
    fourteen seconds to arrive. Retrying that on every call puts the wait in
    front of everything the assistant does, so a rung that times out is rested
    like an exhausted one — for less long, since it is usually passing.

    500 is included with 502/503/504: `Provider request failed with HTTP 500`
    is the backend's own fault, and treating it as anything other than "rest
    this rung and step down" either retried it forever or killed the feature.
    Matched on a word boundary so a "500" inside a larger number or a model
    name cannot trip it.
    """
    low = err.lower()
    return ("unavailable" in low or "deadline_exceeded" in low
            or bool(re.search(r"\b50[0-4]\b", err)))


def is_gone_error(err: str) -> bool:
    """The model is not there, or not ours to use — a different thing from busy."""
    low = err.lower()
    return ("404" in err or "not found" in low or "is not supported" in low
            or "permission" in low or "403" in err)


def live_model(key: str = "") -> str:
    """The Live model to open the conversation with: the first rung that is not
    resting (for `key`, when given). If every one is resting the ladder is used
    from the top anyway — refusing to connect at all is never the better
    answer."""
    for m in LIVE_MODELS:
        if not _cooling(m, key):
            return m
    return LIVE_MODELS[0]


def is_key_error(err: str) -> bool:
    """The key itself is the problem — not the model, and not the network.

    Worth its own case because the remedy is different in kind: every other
    failure here is answered by moving along a ladder, and this one is answered
    by asking the user for a new key. Retrying it against each model in turn
    reaches the same wall repeatedly and, worse, makes a bad key look like a bad
    model in the log.
    """
    low = err.lower()
    return ("api key not valid" in low
            or "api_key_invalid" in low
            or "api key expired" in low
            or "invalid api key" in low
            or "permission_denied" in low and "key" in low)


def note_live_failure(model: str, err: str, key: str = "") -> bool:
    """Record why a Live model failed. True when it is worth trying the next.

    Only quota and availability move the ladder along. A dropped network is not
    the model's fault, and stepping down the ladder for it would work through
    every model and reach the same wall four times.

    A rejected KEY does move the ladder along — but along the key ladder rather
    than the model one, which is what `key` is for. It is recorded against the
    key so the next call reaches for a spare, and the model is left alone: the
    model did nothing wrong.
    """
    if is_key_error(err):
        _mark_key_dead(key, "the API rejected it")
        return True
    if is_quota_error(err):
        _cool(model, _COOLDOWN_SECONDS, key)
        print(f"[Gemini] Live model {model} is out of quota on key "
              f"{key_label(key)} — switching for {_COOLDOWN_SECONDS // 60} minutes.")
        return True
    if is_gone_error(err):
        _cool(model, _GONE_SECONDS)
        print(f"[Gemini] Live model {model} is unavailable to this key — "
              f"setting it aside.")
        return True
    return False


def _cooling(model: str, key: str = "") -> bool:
    with _cool_lock:
        now = time.monotonic()
        for k in {key, ""}:
            until = _cooldown.get((k, model), 0.0)
            if until and now < until:
                return True
            _cooldown.pop((k, model), None)
        return False


def api_keys(refresh: bool = False) -> list[str]:
    """Every Gemini key configured, in the order to try them.

    The file holds one key in `gemini_api_key` and any spares in
    `gemini_api_keys`; the UI's setup screen writes both (see
    MainWindow._on_setup_done). Flattened here into one ladder so the rest of
    the app never has to know the file has two spellings for the same thing.
    Duplicates are dropped and blanks skipped, because a hand-edited file
    readily ends up with both.

    Cached; never raises. Pass refresh=True after the user saves a new key.
    """
    global _cached_keys
    with _key_lock:
        if _cached_keys is not None and not refresh:
            return list(_cached_keys)
        out: list[str] = []
        try:
            data = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
            raw = [data.get("gemini_api_key"), data.get("gemini_api_keys")]
            for item in raw:
                values = item if isinstance(item, (list, tuple)) else [item]
                for value in values:
                    for piece in str(value or "").replace(",", " ").split():
                        if piece and piece not in out:
                            out.append(piece)
        except Exception:
            out = []
        _cached_keys = out
        return list(out)


def _raw_config() -> dict:
    """The whole key file (fallback provider keys live here). Never raises."""
    try:
        return json.loads(_KEY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _has_fallback() -> bool:
    cfg = _raw_config()
    return any(cfg.get(f"{p}_api_key") for p in ("groq", "openrouter"))


def key_label(key: str) -> str:
    """A short, non-revealing name for a key, for the log and the UI.

    Never the key itself: this string is printed at every connect and shown in
    the activity log, and the whole point of the ladder is that the log stays
    safe to paste into a bug report. First four and last four characters are
    enough to tell two keys apart by eye.
    """
    key = (key or "").strip()
    if not key:
        return "none"
    if len(key) <= 12:
        return key[:2] + "…"
    return f"{key[:4]}…{key[-4:]}"


def api_key(refresh: bool = False) -> str:
    """The key to use right now: the first configured one that is not set aside.

    Kept as a single string for the call sites that only ever need one — the
    one-shot calls in this module and in the actions. Falls back to the first
    configured key when every one has been rejected, so behaviour is unchanged
    for the ordinary single-key install.
    """
    keys = api_keys(refresh=refresh)
    for k in keys:
        if not _key_dead(k):
            return k
    return keys[0] if keys else ""


def _key_dead(key: str) -> bool:
    with _dead_lock:
        until = _dead_keys.get(key, 0.0)
        if until and time.monotonic() < until:
            return True
        _dead_keys.pop(key, None)
        return False


def _mark_key_dead(key: str, reason: str = "") -> None:
    if not key:
        return
    with _dead_lock:
        _dead_keys[key] = time.monotonic() + _DEAD_KEY_SECONDS
    if reason:
        print(f"[Gemini] Key {key_label(key)} set aside: {reason}")


def live_target() -> tuple[str, str]:
    """(key, model) to open the conversation with — the rung of both ladders.

    Returned together because the two are chosen by the same rule and are only
    meaningful as a pair: main.py publishes the model back to LIVE_MODEL so
    plugins follow whatever is actually running. When every key has been
    rejected the first one is returned anyway; refusing to connect at all is
    never the better answer, and the connect loop reports the failure.
    """
    keys = [k for k in api_keys() if not _key_dead(k)] or api_keys()
    if not keys:
        return "", live_model()
    for m in LIVE_MODELS:                 # prefer the better model, any key
        for k in keys:
            if not _cooling(m, k):
                return k, m
    return keys[0], LIVE_MODELS[0]


def client(timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = ""):
    """A configured genai.Client with a deadline on it. Raises if there is no
    key, because a caller that cannot work without one should say so."""
    from google import genai
    from google.genai import types as gtypes

    key = key or api_key()
    if not key:
        raise RuntimeError("no Gemini API key is configured")
    return genai.Client(
        api_key=key,
        http_options=gtypes.HttpOptions(timeout=max(MIN_TIMEOUT_MS, int(timeout_ms))),
    )


class _Reply:
    """What a Live turn hands back, shaped like the REST response's `.text` so
    every existing call site keeps working unchanged."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


def _live_model() -> str:
    """Whatever main.py is running, so upgrading the assistant upgrades this."""
    return getattr(sys.modules.get("main"), "LIVE_MODEL", None) or _LIVE_FALLBACK


def _to_live_parts(contents) -> list:
    """REST `contents` -> Live `parts`. Accepts a bare string, a list of
    strings, and the SDK's Part objects (which is how every image is passed
    here), because those are the three shapes the call sites actually use."""
    import base64

    items = contents if isinstance(contents, (list, tuple)) else [contents]
    parts = []
    for item in items:
        if isinstance(item, str):
            parts.append({"text": item})
            continue
        blob = getattr(item, "inline_data", None)
        if blob is not None:
            data = getattr(blob, "data", None)
            mime = getattr(blob, "mime_type", None) or "application/octet-stream"
            if isinstance(data, bytes):
                data = base64.b64encode(data).decode("ascii")
            parts.append({"inline_data": {"mime_type": mime, "data": data}})
            continue
        txt = getattr(item, "text", None)
        if txt:
            parts.append({"text": txt})
            continue
        if isinstance(item, dict):
            parts.append(item)
    return parts


async def _live_turn(parts: list, system: str, key: str, timeout_s: float) -> str:
    from google import genai
    from google.genai import types as gtypes

    cl = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    # Silence the persona, or it answers instead of complying.
    #
    # These are conversational models and they behave like it: asked "Reply with
    # one word: ok" a Live turn came back with "Understood." — it treated the
    # instruction as something to acknowledge rather than something to do. The
    # REST models do not, because nobody ever taught them to be in a
    # conversation. Every call through this module wants a value, not a reply,
    # so the session is told what it is before it is told what to do.
    kwargs = {
        "response_modalities": ["AUDIO"],
        "output_audio_transcription": {},
        "system_instruction": _ONE_SHOT_SYSTEM + (f"\n\n{system}" if system else ""),
    }

    cm = cl.aio.live.connect(model=_live_model(),
                             config=gtypes.LiveConnectConfig(**kwargs))
    session = await asyncio.wait_for(cm.__aenter__(), 30)
    try:
        await session.send_client_content(
            turns={"role": "user", "parts": parts}, turn_complete=True)
        chunks: list[str] = []
        turn_done = False

        async def drain():
            nonlocal turn_done
            async for resp in session.receive():
                sc = getattr(resp, "server_content", None)
                if sc and sc.output_transcription and sc.output_transcription.text:
                    chunks.append(sc.output_transcription.text)
                # Stop on the server's own end-of-turn. The trailing drain below
                # exists only to catch transcription that lands after the audio;
                # once `turn_complete` has arrived there is nothing left to wait
                # for, and sitting out the rest of the window cost 1.5s on every
                # call that finished early — which is all of them.
                if sc is not None and getattr(sc, "turn_complete", False):
                    turn_done = True
                    return

        await asyncio.wait_for(drain(), timeout=timeout_s)
        # The transcription can trail the audio turn by a beat; a short second
        # drain stops a reply being cut mid-token. chat_takeover learned this
        # the same way and for the same reason. Skipped entirely when the turn
        # already reported complete.
        if not turn_done:
            try:
                await asyncio.wait_for(drain(), timeout=1.5)
            except asyncio.TimeoutError:
                pass
        return "".join(chunks).strip()
    finally:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


def _live_call(contents, config, timeout_ms: int, key: str):
    """One throwaway Live session, run on its own loop in its own thread.

    A dedicated thread rather than asyncio.run() on the caller's: these are
    invoked from plugin executor threads, from UI worker threads and from
    main.py's own event loop, and asyncio.run() inside a thread that already has
    a running loop raises. Its own thread has no loop to collide with, wherever
    it was called from.

    Reconnecting costs 0.24s, so nothing is kept alive between calls — no
    session lifetime cap to manage, no GoAway to handle, no shared state.
    """
    system = ""
    if config is not None:
        system = getattr(config, "system_instruction", None) or \
            (config.get("system_instruction") if isinstance(config, dict) else "") or ""

    parts = _to_live_parts(contents)
    if not parts:
        return None

    if not _LIVE_SLOTS.acquire(timeout=_LIVE_SLOT_WAIT):
        # Every slot is busy. Do not wait it out: falling to REST costs less
        # than holding a session the user's conversation might want.
        raise RuntimeError("no free Live slot — leaving them for the conversation")

    box: dict = {}

    def runner():
        try:
            box["text"] = asyncio.run(
                _live_turn(parts, str(system), key, max(10.0, timeout_ms / 1000.0)))
        except BaseException as e:                     # noqa: BLE001
            box["error"] = e

    try:
        th = threading.Thread(target=runner, daemon=True, name="gemini-live-oneshot")
        th.start()
        th.join(timeout=max(15.0, timeout_ms / 1000.0 + 20.0))
    finally:
        _LIVE_SLOTS.release()
    if "error" in box:
        raise box["error"]
    text = box.get("text")
    return _Reply(text) if text else None


def call(contents, tier: str = FAST, config=None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = ""):
    """Run one generation, walking the ladder until one answers.

    Returns the SDK's own response object, so callers that need more than the
    text — grounding metadata, candidates, usage — still get it. Returns None
    when every model on the ladder failed; the reason for each is printed, since
    a silent None during a session nobody can debug is how the original problem
    stayed hidden.
    """
    # `tier` is normally FAST or SMART. Anything else is taken to be an explicit
    # model name — screen_agent lets the user pick one in its settings — and it
    # is tried first, with the reasoning ladder behind it. So a user's choice is
    # honoured, and a user's choice that is having an outage still degrades to
    # something that answers instead of to nothing.
    ladder = _LADDERS.get(tier)
    if ladder is None:
        ladder = (tier,) + tuple(m for m in _LADDERS[SMART] if m != tier)

    # Key pool: an explicit `key` goes first, then every configured spare.
    # Rotation order is model-outer, key-inner, so the fastest model gets to
    # use every key's quota before the ladder steps down to a slower one.
    keys = [k for k in dict.fromkeys(([key] if key else []) + api_keys())]
    if not keys:
        print("[Gemini] no Gemini API key is configured")
    pairs = [(m, k) for m in ladder for k in keys]
    # Skip resting pairs — unless everything is resting, then try them anyway.
    skip_resting = any(not _cooling(m, k) for m, k in pairs)
    if not skip_resting and _has_fallback():
        pairs = []          # all Gemini pairs resting and a fallback exists: go straight to it

    clients: dict[str, object] = {}
    for model, k in pairs:
        if _key_dead(k) or (skip_resting and _cooling(model, k)):
            continue
        try:
            if model == LIVE:
                reply = _live_call(contents, config, timeout_ms, k)
                if reply is not None:
                    return reply
                raise RuntimeError("the Live turn came back empty")
            if k not in clients:
                clients[k] = client(timeout_ms=timeout_ms, key=k)
            kwargs = {"model": model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            return clients[k].models.generate_content(**kwargs)
        except Exception as e:
            msg = str(e)
            if is_key_error(msg):
                # The key, not the model: drop it for every model, next key.
                _mark_key_dead(k, "the API rejected it")
            elif is_quota_error(msg):
                # This key's quota. Rest the pair; next iteration is the same
                # model on the next key.
                _cool(model, _COOLDOWN_SECONDS, k)
                print(f"[Gemini] {model} @ {key_label(k)}: out of quota — "
                      f"rotating, resting the pair {_COOLDOWN_SECONDS // 60} min")
            elif is_gone_error(msg):
                _cool(model, _GONE_SECONDS)
                print(f"[Gemini] {model}: unavailable to this key — set aside")
            elif is_unavailable_error(msg):
                _cool(model, _UNAVAILABLE_SECONDS)
                print(f"[Gemini] {model}: not answering — resting it for "
                      f"{_UNAVAILABLE_SECONDS // 60} minutes")
            else:
                print(f"[Gemini] {model}: {type(e).__name__}: {msg[:140]}")

    # Every Gemini (model, key) failed. Fall back to Groq / OpenRouter — except
    # for grounded search, which they cannot do honestly.
    has_tools = config.get("tools") if isinstance(config, dict) else getattr(config, "tools", None)
    if tier == SEARCH or has_tools:
        return None
    try:
        try:
            from . import model_router
        except ImportError:                    # imported as a top-level module
            import model_router
        out = model_router.fallback_text(contents, config, _raw_config(),
                                         timeout_s=max(10.0, timeout_ms / 1000.0))
    except Exception as e:                                # noqa: BLE001
        print(f"[Router] fallback failed: {type(e).__name__}: {str(e)[:140]}")
        return None
    return _Reply(out) if out else None


def text(contents, tier: str = FAST, config=None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = "", default: str = "") -> str:
    prompt = str(contents) if isinstance(contents, str) else getattr(contents, "text", str(contents))
    p_lower = prompt.lower()

    # ১. কাজের কি-ওয়ার্ড শনাক্ত করা (কোডিং বা ভারী কাজ)
    is_coding = any(k in p_lower for k in ("কোড", "code", "python", "script", "function", "bug", "program", "debug"))
    is_work   = any(k in p_lower for k in ("ডক্স", "docs", "শিট", "sheet", "ফাইল", "file", "update", "excel", "csv", "summary", "summarize", "draft"))

    # ২. যদি কোনো স্পেসিফিক কাজ (কোডিং/ফাইল/ডকুমেন্ট) দেওয়া হয় -> OpenRouter (Task Router)
    if is_coding or is_work or tier == SMART:
        task = "coding" if is_coding else "writing"
        try:
            from core import model_router
            out = model_router.call(prompt=prompt, task=task)
            if out:
                return out
        except Exception as e:
            print(f"[Gemini.text] Task router failed, falling back to Gemini: {e}")

    # ৩. সাধারণ কথাবার্তা ও প্রশ্নোত্তরের জন্য সরাসরি দ্রুতগতির Gemini Flash
    resp = call(contents, tier=tier, config=config, timeout_ms=timeout_ms, key=key)
    if resp is None:
        return default
    return (getattr(resp, "text", None) or "").strip() or default

def as_json(contents, tier: str = FAST, config=None,
            timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = "", default=None):
    """`text`, parsed as JSON, tolerating the fences and prose a model wraps it
    in. `default` when nothing answered or the answer would not parse."""
    raw = text(contents, tier=tier, config=config, timeout_ms=timeout_ms, key=key)
    if not raw:
        return default
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    elif "[" in raw and "]" in raw:
        raw = raw[raw.find("["): raw.rfind("]") + 1]
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[Gemini] reply was not JSON: {e}")
        return default
