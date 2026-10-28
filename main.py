from core.security_policy import enforce_camera_killswitch
enforce_camera_killswitch()

import platform as _platform
import subprocess as _subprocess
import os
os.environ["QT_LOGGING_RULES"] = "qt.text.*=false;qt.text.font.*=false;qt.qpa.*=false;qt.multimedia.*=false;*.debug=false;*.info=false"

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen


# ── Console must survive non-UTF-8 code pages ────────────────────────────────
# Every status line in this file carries an emoji, and on a legacy Windows
# console the active code page is the system one — cp1254 in Turkey, cp1251 in
# Russia, cp932 in Japan. Printing an emoji there raises UnicodeEncodeError, and
# because most of these prints sit inside the receive loop it takes the session
# down on startup. Reconfiguring to UTF-8 with a replacement fallback costs
# nothing and makes the app launch the same way in every locale.
import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw / redirected pipes / anything exotic — never fatal

# ─────────────────────────────────────────────────────────────────────────────

# pythonw.exe (what the desktop shortcut and auto-start use) has NO console, so
# sys.stdout / sys.stderr are None. Libraries that assume a console then crash
# (uvicorn did: "Unable to configure formatter"), and every print() / traceback
# vanishes, which is why a problem could never be seen. Give them a log file.
if _sys.stdout is None or _sys.stderr is None:
    try:
        from pathlib import Path as _Path
        _cfg_dir = _Path(__file__).resolve().parent / "config"
        _cfg_dir.mkdir(parents=True, exist_ok=True)
        _log_path = _cfg_dir / "jarvis.log"
        if _log_path.exists() and _log_path.stat().st_size > 2_000_000:
            _log_path.replace(_cfg_dir / "jarvis.old.log")
        _log_fh = open(_log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        _log_fh.write("\n==== JARVIS started (no console) ====\n")
        if _sys.stdout is None:
            _sys.stdout = _log_fh
        if _sys.stderr is None:
            _sys.stderr = _log_fh
    except Exception:
        pass

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier,
)

# The file-backed tools … auto-discovered by core.action_loader at startup.
# Only tools that are tied to live-session state stay inline in this file
# (save_memory, shutdown_jarvis, system_status). Screen capture is manual-only
# (F9 / HUD button); the model never initiates it.
from actions.system_monitor    import SystemMonitor, get_system_status
from memory.config_manager     import (
    get_media_resolution, get_proactive_audio_enabled,
    get_push_to_talk_enabled, get_thinking_enabled, get_turn_tuning, get_voice,
    get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
    get_noise_gate, get_barge_in_enabled, get_barge_in_sensitivity,
    save_barge_in_enabled, save_barge_in_sensitivity,
    get_dynamic_ptt_enabled, get_plugin_enabled,
    get_brief_enabled,
)
from core                     import gemini as _gemini
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import study_guard


from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.action_loader        import discover_actions
from core.echo                 import EchoGuard
from core.barge_in             import BargeInDetector
from core.noise_gate           import NoiseGate
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

# How long the assistant stays awake with no user speech before it auto-sleeps
# again (wake-word mode only).
WAKE_SLEEP_TIMEOUT = 120.0   # seconds (2 minutes)

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
# The conversation's model. A NAME, not a decision: the ladder lives in
# core/gemini.py and this is only whichever rung is currently in use, kept here
# as a module attribute because plugins read it (chat_takeover asks main for it
# so that upgrading the assistant upgrades the plugin too).
#
# It is reassigned on every connect, so a model that runs out of quota is
# stepped over and the assistant keeps talking instead of failing to start.
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


# Extra time beyond the device's reported output latency before the microphone
# is trusted again: covers room decay and the speaker's own settling.
_TAIL_MARGIN = 0.25

def _describe_limits(has_vision: bool, has_mic: bool) -> str:
    """The other half of self-knowledge: what is out of reach, and why.

    Derived from how the program is actually built, not from a list of refusals.
    A model that knows its boundaries stops improvising around them, and stating
    them as architecture rather than as rules keeps the answer honest in any
    language.
    """
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\n".join(out)


def _render_prompt(template: str, values: dict) -> str:
    """Fill {tokens} in the prompt template.

    A plain replace rather than str.format: the file is meant to be edited by
    hand, and a stray brace in someone's own wording must never take the app
    down at startup.
    """
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _get_api_key() -> str:
    # The key ladder lives in core/gemini.py: the first configured key that is
    # not rejected and still has a Live model with quota.
    k = _gemini.api_key()
    if k:
        return k
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# Transcript chunks shorter than this may legitimately repeat ("evet, evet"),
# so only longer ones are treated as duplicates.
_REPEAT_MIN = 12

# How long a single tool call may run before the session gives up on it.
#
# A tool that never returns blocks `_receive_audio`, which is the only place the
# Live session is read — so while one hangs, nothing is received, no pong is
# sent, and the server eventually drops the connection with "1011 keepalive ping
# timeout". One wedged action (a browser that never comes back, a request with
# no timeout inside it) therefore takes the whole conversation with it.
#
# Generous on purpose: it is a backstop, not a deadline. The slowest legitimate
# action declares 90 s for itself, so this sits well clear of honest work and
# only catches the genuinely stuck. On expiry the tool reports that it timed out
# and the model answers around it — the session survives either way.
_TOOL_TIMEOUT_S = 180.0

# How much mic audio one socket write may carry, in bytes.
#
# `_send_realtime` used to await a separate write for every CHUNK_SIZE frame.
# At 16 kHz mono int16 that is one round trip per 64 ms of speech, and the
# queue only ever drains as fast as the socket accepts — so a small network
# hiccup left the upstream lagging real time and the model hearing the user
# late. Batching whatever is already waiting removes roughly half the round
# trips. 4096 bytes is ~128 ms: two frames, small enough that a batch is never
# audibly late, large enough to matter.
_REALTIME_BATCH_BYTES = 4096


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    """True if this transcript chunk has already been seen this turn.

    Guards against the API re-sending the tail of a response across the several
    turn_completes a tool-using turn produces.
    """
    if len(txt) < _REPEAT_MIN:
        return bool(buf) and txt == buf[-1]
    joined = " ".join(buf)
    return txt in joined

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

def _foreground_title() -> tuple[str, str]:
    """(window title, exe name) of the active Windows window, or ('', '').

    Read-only ctypes query — no pywin32 dependency, no window manipulation.
    Used by Smart Ears as a media-detection fallback; fails closed on any OS.
    """
    try:
        import ctypes
        if _platform.system() != "Windows":
            return "", ""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
        if h:
            try:
                pb = ctypes.create_unicode_buffer(512)
                sz = ctypes.c_ulong(512)
                if kernel32.QueryFullProcessImageNameW(h, 0, pb, ctypes.byref(sz)):
                    name = pb.value.rsplit("\\", 1)[-1]
            finally:
                kernel32.CloseHandle(h)
        return buf.value.strip(), name
    except Exception:
        return "", ""

TOOL_DECLARATIONS = [
    # ── Inline tools ─────────────────────────────────────────────────────────
    # These stay here (rather than in an actions/*.py TOOL dict) because their
    # handling is woven into live-session state — memory writes, the passive
    # health-check engine, and shutdown. All other tools live in their own
    # action file and are auto-discovered by core.action_loader.
    # Screen capture is manual-only (F9 / HUD button): the image is injected
    # into the session by _on_manual_screen_captured, never by a model tool call.
    {
        "name": "system_status",
        "description": (
            "Returns lightweight PC health metrics: CPU usage, RAM, GPU load, "
            "CPU temperature, and — when an NVIDIA card is present — GPU "
            "temperature. Use ONLY when the user asks about computer health or "
            "performance. Continuous polling is not permitted in the Study "
            "Edition; this is an on-request snapshot only."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Sam, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


def _contains_cancel(exc: BaseException) -> bool:
    """True when `exc` is a CancelledError, or a group wrapping one.

    A cancellation means asyncio is tearing the loop down (process exit, or
    `asyncio.run` finishing), not that the network failed. It must be re-raised:
    catching it here turned a shutdown into an endless reconnect loop on a loop
    whose default executor had already been closed, where every retry died with
    "cannot schedule new futures after shutdown" and the UI showed "Connection
    failed" forever.
    """
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_cancel(sub) for sub in exc.exceptions)
    return False


# ═════════════════════════════════════════════════════════════════════════════
# DIAGNOSTIC WATCHDOG / TRACE MONITOR
# ─────────────────────────────────────────────────────────────────────────────
# Purpose: make a silent failure impossible. Every boundary crossing between the
# Qt thread, the asyncio loop thread, PortAudio threads and ThreadPoolExecutor
# workers prints who it is and where it went. A background watchdog notices when
# the event loop stops beating and dumps every thread's stack, so a deadlock or
# a blocking sync call is shown at the exact line instead of a frozen HUD.
#
# Silence it with:  set JARVIS_TRACE=0
# ═════════════════════════════════════════════════════════════════════════════
_TRACE_ON = os.environ.get("JARVIS_TRACE", "0").strip().lower() in ("1", "true", "yes", "on")
_TRACE_T0 = time.monotonic()
_LOOP_LAG_WARN = 3.0                 # seconds a late heartbeat is tolerated
_STUDY_PAYLOAD_HARD_LIMIT = 12000    # chars — the Live WS chokes on big blobs
_STUDY_PROMPT_OVERHEAD = 600         # room reserved for the prompt wrapper itself
_STUDY_HEAD = 3000
_STUDY_TAIL = 3000


def _tid() -> str:
    """Human-readable thread identity — name plus OS id."""
    t = threading.current_thread()
    return f"{t.name}#{threading.get_ident()}"


def _trace(tag: str, msg: str = "") -> None:
    """Timestamped, thread-aware trace line. Says whether the caller is ON the
    asyncio loop or off it — the single most useful fact when a send deadlocks."""
    if not _TRACE_ON:
        return
    try:
        on_loop = asyncio.get_running_loop() is not None
    except Exception:
        on_loop = False
    where = "ON-LOOP " if on_loop else "off-loop"
    print(f"[TRACE {time.monotonic() - _TRACE_T0:8.3f}] {where} [{tag}] {_tid()}  {msg}",
          flush=True)


def _truncate_head_tail(text: str, head: int = _STUDY_HEAD, tail: int = _STUDY_TAIL,
                        hard: int = _STUDY_PAYLOAD_HARD_LIMIT) -> tuple[str, bool]:
    """Clamp a text blob so a giant PDF cannot blow the Live WebSocket.

    Returns (clamped_text, was_truncated). Keeps the head and the tail — for a
    document the middle is usually the least useful part — with an explicit
    elision marker, and never exceeds `hard` characters.
    """
    if not text:
        return "", False
    if len(text) <= hard:
        return text, False
    dropped = len(text) - head - tail
    marker = (f"\n\n[... {dropped:,} characters omitted by the local payload guard "
              f"({len(text):,} total) ...]\n\n")
    out = text[:head] + marker + text[-tail:]
    if len(out) > hard:                       # pathological input — still clamp
        out = out[:hard]
    return out, True


def _dump_all_thread_stacks(reason: str = "") -> None:
    """Print every thread's Python stack — the exact frame a stall is stuck in."""
    print(f"\n[WATCHDOG] ===== thread dump ({reason}) =====", flush=True)
    try:
        names = {t.ident: t.name for t in threading.enumerate()}
        for tid, frame in sys._current_frames().items():
            print(f"\n[WATCHDOG] --- thread {names.get(tid, '?')}#{tid} ---", flush=True)
            traceback.print_stack(frame)
    except Exception:
        traceback.print_exc()
    print("[WATCHDOG] ===== end thread dump =====\n", flush=True)


class JarvisLive:
    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._asst_name     = "JARVI    S"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._loop_thread              = None    # set in run(); used to detect on-loop callers
        self._hb                      = 0.0     # event-loop heartbeat for the watchdog
        self._last_lag_note           = 0.0     # throttles PLAY_LAG log lines
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._phone_last          = 0.0     # monotonic time of the last phone audio chunk
        self._interrupted          = False   # True while draining audio after user interrupt
        self._last_out_logged      = ""      # de-dupes a re-sent transcript tail
        # Push-to-talk
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None    # core.hotkey.PushToTalk
        # Smart Ears (Ep 12): auto hands-free <-> push-to-talk on media playback.
        self._dynamic_ptt_enabled  = get_dynamic_ptt_enabled()
        self._media_ptt_on         = False   # True while Smart Ears holds PTT on
        self._ptt_before_media     = False   # user's PTT state before media took over
        self._out_level            = 0.0     # level of the audio being played right now
        self._echo                 = EchoGuard()
        # Barge-in: speaking over JARVIS cuts the reply short. Built always (it
        # is inert until enabled) so the settings toggle is a flag flip rather
        # than something that has to be constructed on a live audio thread.
        self._barge_enabled        = get_barge_in_enabled()
        self._barge                = BargeInDetector(
            self._echo,
            sensitivity=get_barge_in_sensitivity(),
            logger=lambda m: print(f"[BargeIn] {m}"),
        )
        _ng = get_noise_gate()
        self._noise_gate           = NoiseGate(_ng["strength"]) if _ng["enabled"] else None
        # `stream.write()` returns when the buffer accepts the audio, not when the
        # speaker has finished with it, so sound is still in the room after the
        # speaking flag drops. Streaming the microphone during that gap is how an
        # assistant ends up answering itself. Measured from the device rather than
        # guessed; see _play_audio.
        self._out_latency          = 0.20    # seconds, replaced with the real value
        self._tail_until           = 0.0     # monotonic time the echo tail expires
        self.ui.on_push_to_talk   = self.set_push_to_talk
        self.ui.ptt_hold          = self._on_ptt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_manual_screen_captured = self._on_manual_screen_captured
        self.ui.on_study_document = self._on_study_document   # Phase 5: drop-to-study
        self.ui.on_study_documents = self._on_study_documents  # Phase 5: batch (files/folder)
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        # Last prompt size the server reported, so the token line above prints
        # once per change rather than on every message of a session.
        self._last_prompt_tokens: int | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []
        self._last_mode_state: bool = study_guard.study_active()          # conversation turns for end-of-session summary
        self._tool_in_flight: bool = False  # True while a tool call is being executed

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it
        self._tuned_live    = True  # turn-taking / media / thinking knobs; same fallback

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            # Console gets the full boot transcript; the activity log gets only
            # what the user has to know about. Every plugin loading correctly is
            # the expected case and does not belong in their conversation.
            logger=lambda msg: print(f"[Plugins] {msg}"),
            notify=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ settings tab
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel

        # ── Wake word ────────────────────────────────────────────────────────
        # _awake gates the mic (see _listen_audio) and the background speakers.
        # It is True whenever wake word is OFF, so default behaviour is unchanged.
        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT

        # Restore the saved push-to-talk preference. Doing it here rather than
        # in __init__ means the hotkey thread only exists once there is a
        # session to talk to.
        if get_push_to_talk_enabled():
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] ⚠ Push-to-talk unavailable: {e}")
        # UI control surface for the Voice Interrupt settings row. Read through
        # the live object rather than the saved config so the row always shows
        # what the audio path is actually doing.
        self.ui.get_barge_state   = self._barge_state     # () -> dict
        self.ui.on_barge_toggle   = self.set_barge_in     # (enable) -> str
        # UI control surface for the Wake Word settings section.
        self.ui.wake_is_ready    = wake_is_ready          # () -> bool
        self.ui.wake_get_state   = self._wake_state       # () -> dict
        self.ui.on_wake_toggle   = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_wake_manual   = self._ui_wake_manual   # () -> toggle awake/asleep
        self.ui.on_wake_install  = self._ui_wake_install  # () -> (ok, msg)

    # ── Barge-in ─────────────────────────────────────────────────────────────

    def _barge_state(self) -> dict:
        """What the Voice Interrupt row should display.

        Read from the detector rather than from config, so the row reflects what
        the microphone callback is actually doing. `ready` is the honest part:
        the guard needs to hear a sentence or two of JARVIS before it knows this
        room's echo, and until then interruptions are held back.
        """
        try:
            s = self._barge.stats
            return {
                "enabled":     self._barge_enabled,
                "sensitivity": s.get("sensitivity", "medium"),
                "calibrated":  bool(s.get("calibrated")),
                "reliable":    bool(s.get("reliable", True)),
                "echo_floor":  s.get("echo_floor", 0.0),
            }
        except Exception:
            return {"enabled": self._barge_enabled, "sensitivity": "medium",
                    "calibrated": False, "reliable": True, "echo_floor": 0.0}

    # ── Wake word: state machine ─────────────────────────────────────────────

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: print(f"[Wake] {m}"),
                notify=lambda m: self.ui.write_log(f"SYS: {m}"),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        # Asleep means the microphone is not being listened to at all, so any
        # half-gathered interruption is meaningless — and the wake gate returns
        # before the falling edge would otherwise clear it, which would leave the
        # state stale across the sleep.
        self._barge.reset()
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")

    # ── Wake word: UI callbacks (called from the Qt thread) ──────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        # Triggered by the user pressing the button, so its progress is exactly
        # what they are waiting to see.
        return wake_install(logger=lambda m: print(f"[Wake] {m}"),
                            notify=lambda m: self.ui.write_log(f"SYS: {m}"))

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return
        _trace("PLUGIN_SAY", f"{len(instruction):,} chars")

        async def _say():
            try:
                is_silent_mode_switch = instruction.lstrip().startswith("[MODE_SWITCH:")
                self._last_mode_state = study_guard.study_active()
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=not is_silent_mode_switch,
                )
                if is_silent_mode_switch:
                    print(f"[ModeSync] 🔄 F8 / UI Mode switch synced to Live session -> Study={self._last_mode_state}")
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        if getattr(self._dashboard, "port_conflict", False):
            self.ui.write_log(
                "ERR: Port 8000 is already used - JARVIS is probably open twice "
                "(VS Code + desktop shortcut). Close the other one, then press Remote Control again."
            )
            return None
        if not getattr(self._dashboard, "ready", True):
            self.ui.write_log(
                "ERR: The phone dashboard is not running. Details: config/jarvis.log "
                "(or the console)."
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        # Respect wake-word sleep: a typed command must not be answered while
        # asleep either (the sleep gate is not just for the mic). Wake first with
        # "Hey Jarvis" or the WAKE NOW button.
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW first.")
            return
        _is_st = study_guard.study_active()
        self._last_mode_state = _is_st
        _live_mode = (
            "[LIVE_STATE: STUDY_MODE = ON (You are currently in Study Mode; act as Dedicated Teacher)]\n"
            if _is_st else
            "[LIVE_STATE: NORMAL_MODE = ON (Study Mode is OFF; you are in Normal Assistant HUD Mode, NOT Study Mode)]\n"
        )
        self._send_text_to_live(_live_mode + text, tag="TEXT_CMD", turn_complete=True)

    def _start_watchdog(self) -> None:
        """Arm the event-loop heartbeat + lag watchdog.

        The loop bumps `self._hb` twice a second. A daemon thread checks it; if
        the loop goes quiet for more than _LOOP_LAG_WARN seconds — a deadlock, a
        blocking sync call inside a coroutine, a tool that forgot to await — every
        thread's stack is dumped once, so the freeze is pinpointed rather than
        guessed at. Prints nothing while the loop is healthy.
        """
        self._loop_thread = threading.current_thread()
        self._hb = time.monotonic()
        _trace("WATCHDOG", f"armed on loop thread {_tid()} (lag limit {_LOOP_LAG_WARN}s)")

        async def _beat():
            while True:
                self._hb = time.monotonic()
                await asyncio.sleep(0.5)

        self._hb_task = asyncio.create_task(_beat())

        def _watch():
            warned = False
            while True:
                time.sleep(0.5)
                try:
                    age = time.monotonic() - (self._hb or time.monotonic())
                    if age > _LOOP_LAG_WARN and not warned:
                        warned = True
                        _trace("WATCHDOG", f"⛔ event loop STALLED ~{age:.1f}s — dumping stacks")
                        _dump_all_thread_stacks(f"event loop stalled ~{age:.1f}s")
                    elif age < 1.0:
                        warned = False
                except Exception:
                    pass

        threading.Thread(target=_watch, daemon=True, name="JarvisWatchdog").start()

    def _send_text_to_live(self, text: str, tag: str = "TEXT",
                           turn_complete: bool = True) -> bool:
        """The ONE audited path for pushing a text turn into the Live session.

        Thread-safe from anywhere. On the asyncio loop it schedules a task; from
        any other thread — Qt GUI, PortAudio callback, ThreadPoolExecutor, plugin
        executor — it crosses via `asyncio.run_coroutine_threadsafe(..., _loop)`,
        which is the only correct way to do it. Crucially the returned future is
        ALWAYS observed, so an exception from the WebSocket write cannot die
        silently in a background thread.
        """
        if self._loop is None or self.session is None:
            _trace(tag, "no live session/loop → dropped")
            return False
        try:
            coro = self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=turn_complete,
            )
        except Exception:
            print(f"[{tag}] ❌ building send_client_content raised:")
            traceback.print_exc()
            return False

        on_loop = threading.current_thread() is getattr(self, "_loop_thread", None)
        try:
            if on_loop:
                task = asyncio.ensure_future(coro)
                task.add_done_callback(self._log_send_result(tag))
            else:
                fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
                fut.add_done_callback(self._log_send_result(tag))
        except Exception:
            print(f"[{tag}] ❌ hand-off to the Live loop failed:")
            traceback.print_exc()
            return False
        _trace(tag, f"queued {len(text):,} chars → Live (turn_complete={turn_complete})")
        return True

    @staticmethod
    def _log_send_result(tag: str):
        """Future callback that surfaces a swallowed WebSocket error."""
        def _cb(fut):
            try:
                fut.result()
                _trace(tag, "send_client_content OK")
            except Exception:
                print(f"[{tag}] ❌ send_client_content raised:")
                traceback.print_exc()
        return _cb

    def _on_manual_screen_captured(self, shot_path: str) -> bool:
        """Push an F9 / HUD screenshot straight into the live conversation.

        Called on the Qt thread by the header buttons. The PNG is wrapped as a
        Google GenAI ``types.Part.from_bytes`` image part and sent in the same
        turn as a short instruction, so the model can answer follow-up
        questions about whatever was on screen without a manual upload.
        """
        if self._loop is None or self.session is None:
            self.ui.write_log(
                f"SYS: 📸 Screen capture saved: {shot_path} "
                "(JARVIS is offline — start a session, then ask about it.)")
            return False

        try:
            image_bytes = Path(shot_path).read_bytes()
        except Exception as e:
            self.ui.write_log(f"ERR: Could not read screen capture: {e}")
            return False

        mime_type = "image/jpeg" if str(shot_path).lower().endswith((".jpg", ".jpeg")) else "image/png"
        try:
            image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        except Exception:
            # Older google-genai builds: send the raw inline_data shape instead.
            import base64
            image_part = {"inline_data": {
                "mime_type": mime_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }}

        prompt = (
            "[MANUAL SCREEN CAPTURE]\n"
            "The user pressed F9 and loaded a fresh screenshot of their screen "
            "into this conversation. Treat it as the visual context for their "
            "next questions. Acknowledge in one short sentence that you can see "
            "the screen and are ready, then wait for the question."
        )

        try:
            turn = types.Content(role="user", parts=[image_part, types.Part(text=prompt)]) \
                if not isinstance(image_part, dict) else \
                {"role": "user", "parts": [image_part, {"text": prompt}]}
        except Exception:
            turn = {"role": "user", "parts": [image_part, {"text": prompt}]}

        async def _send():
            await self.session.send_client_content(turns=turn, turn_complete=True)

        on_loop = threading.current_thread() is getattr(self, "_loop_thread", None)
        try:
            if on_loop:
                task = asyncio.ensure_future(_send())
                task.add_done_callback(self._log_send_result("CAPTURE"))
            else:
                fut = asyncio.run_coroutine_threadsafe(_send(), self._loop)
                fut.add_done_callback(self._log_send_result("CAPTURE"))
        except Exception:
            print("[CAPTURE] ❌ hand-off to the Live loop failed:")
            traceback.print_exc()
            self.ui.write_log("ERR: Screen capture could not reach the live session.")
            return False

        print(f"[CAPTURE] 📤 {len(image_bytes):,} bytes → Live conversation")
        self.ui.write_log(
            "📸 Screen capture loaded into context. What would you like to know about it?")
        return True

    def _on_study_document(self, path: str):
        """Phase 5 — single-document drop-to-study (legacy entry point).

        A thin wrapper over the batch handler, so the dashboard, the phone
        upload path and the smoke test keep calling it exactly as before.
        """
        self._on_study_documents([path])

    def _on_study_documents(self, paths):
        """Phase 5 — Study Mode batch ingest for one OR many dropped documents.

        CALLED FROM A WORKER THREAD (the Qt thread spawns it in
        ui._on_files_selected), so the whole body is off the asyncio loop:
        extraction may be slow — a folder of PDFs especially — but it can never
        freeze the HUD or the Live session. Each file is read locally, the texts
        are concatenated with explicit `--- FILE: name ---` delimiters, and the
        aggregate is clamped by the payload guard before the one audited
        cross-thread handoff (`_send_text_to_live`). Every failure is printed
        with a full traceback — nothing dies silently here.
        """
        _trace("STUDY", f"enter paths={list(paths or [])!r}")
        try:
            paths = [str(p) for p in (paths or []) if p]
            if not paths:
                return
            if self._loop is None or self.session is None:
                _trace("STUDY", "no live session yet → ignored")
                return
            if self._wake_enabled and not self._awake:
                self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW, then drop the file again.")
                return

            from actions.file_processor import extract_documents
            try:
                from actions.study_kb_lookup import ingest_files_or_folders
                kb_res = ingest_files_or_folders(paths)
                if kb_res.get("chunks_saved", 0) > 0:
                    self.ui.write_log(
                        f"📚 Vault Indexed: {kb_res['chunks_saved']} chunks from "
                        f"{kb_res['files_indexed']} file(s) saved permanently to {kb_res['db_path']}"
                    )
            except Exception as _kb_err:
                print(f"[STUDY] Vault auto-index warning: {_kb_err}")

            # ── extraction + aggregation, with per-file delimiters ─────────
            # A folder of documents is read one by one and concatenated as
            # `--- FILE: name ---` blocks by the actions layer, which owns file
            # parsing. Any failure is reported, never silently dropped.
            try:
                aggregate, names = extract_documents(paths)
            except Exception:
                print("[STUDY] ❌ local extraction failed:")
                traceback.print_exc()
                aggregate, names = "", []
            _trace("STUDY", f"extracted {len(aggregate):,} chars from {len(names)} file(s)")

            if not names:
                self.ui.write_log(
                    "STUDY: no extractable text in "
                    + ", ".join(os.path.basename(p) for p in paths)
                    + " (scanned/unsupported).")
                return

            # Payload guard: the Live WebSocket rejects / drops on very large
            # blobs, so clamp to head+tail before anything touches the socket.
            body, truncated = _truncate_head_tail(
                aggregate, hard=_STUDY_PAYLOAD_HARD_LIMIT - _STUDY_PROMPT_OVERHEAD)
            if truncated:
                self.ui.write_log(
                    f"STUDY: {len(names)} document(s) are large — sending head+tail only "
                    f"({len(body):,} of {len(aggregate):,} chars) to protect the Live socket.")
                _trace("STUDY", f"payload clamped {len(aggregate):,} → {len(body):,} chars")

            label = names[0] if len(names) == 1 else f"{len(names)} documents"
            prompt = (
                "[STUDY_DOCUMENT]\n"
                f"file={label}\n"
                f"documents={len(names)}\n"
                "The student dropped these document(s). Their text was extracted locally (no "
                "images, no OCR) and is reproduced below; each document is delimited by a "
                "'--- FILE: name ---' marker. Treat them as the source of truth for the rest "
                "of this conversation.\n"
                "First say, in the student's language (Banglish/Bengali with English tech "
                "terms), that you have read the document(s), then ask whether they want a "
                "summary, a mindmap, or a quiz on them. Do not summarise yet — just ask.\n"
                "----- DOCUMENT TEXT -----\n"
                f"{body}\n"
                "----- END DOCUMENT -----"
            )
            self.ui.write_log(
                f"STUDY: {len(names)} doc(s) read locally ({len(body):,} chars → model).")
            self._send_text_to_live(prompt, tag="STUDY_SEND", turn_complete=True)
        except Exception:
            print("[STUDY] ❌ drop-to-study failed:")
            traceback.print_exc()

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
        else:
            # Hold the guard open across the device's own output latency plus a
            # margin for the room. The microphone is NOT muted during it — the
            # guard still lets a genuine reply through, so answering instantly
            # still works. Only our own echo is dropped.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            # The echo history is deliberately NOT cleared here: the tail above
            # still needs it to recognise our own voice. It is dropped when the
            # tail expires. What the guard learned about the room always stays.
            self._out_level = 0.0
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        # A window-scoped chord is a real limitation, not a detail — say it once
        # in the log so nobody wonders why it does nothing while another app is
        # focused. Reporting it must never be able to undo the thing it reports.
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def set_barge_in(self, enabled: bool, sensitivity: str | None = None) -> str:
        """Turn voice interruption on or off. Called from the Qt thread.

        Nothing is constructed here — the detector already exists and this only
        flips the flag the microphone callback reads. It is deliberately that
        cheap: the callback is a real-time audio thread, and anything that could
        block there would drop samples.
        """
        self._barge_enabled = bool(enabled)
        if sensitivity is not None:
            self._barge.sensitivity = sensitivity
        save_barge_in_enabled(self._barge_enabled)
        if sensitivity is not None:
            save_barge_in_sensitivity(self._barge.sensitivity)

        if not self._barge_enabled:
            self._barge.reset()
            return "off"

        # Say what it will actually do, including the honest part. The room this
        # is running in decides how well it works, and the guard only knows that
        # after it has heard a sentence or two — so report the state at the
        # moment of switching on rather than implying it is settled.
        try:
            _s = self._barge.stats
            _room = ("still calibrating" if not _s.get("calibrated")
                     else "quiet room" if _s.get("reliable")
                     else "difficult room — needs sustained speech")
            self.ui.write_log(
                f"SYS: Voice interrupt on — sensitivity {self._barge.sensitivity}, "
                f"{_room}.")
        except Exception:
            pass
        return "on"

    def _on_ptt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        if held:
            # Holding the key is also a way to wake it, so push-to-talk works
            # without having to say the wake word first.
            if self._wake_enabled and not self._awake:
                self._awake = True
                self._last_user_speech = time.monotonic()
        try:
            self.ui.set_state("LISTENING" if held else "SLEEPING")
        except Exception:
            pass

    # ── Smart Ears: dynamic push-to-talk (Ep 12) ─────────────────────────────

    async def _system_media_playing(self) -> bool:
        """True when any Windows media session (Spotify, browser, VLC, …) plays.

        Primary signal is the Global System Media Transport Controls (the API
        behind the volume-flyout media keys) — read-only, and it reports the
        PLAYING state (playback_status == 4) exactly as Ep 12 does. Falls back to
        the current session or, failing that, the first open session. A final
        heuristic reads the foreground window title for the common browser-media
        case, which GSMTC sometimes misses. Any failure is False, so Smart Ears
        degrades to the in-app video signal alone.
        """
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as _Mgr,
            )
            mgr = await _Mgr.request_async()
            session = mgr.get_current_session() if mgr else None
            if session is None and mgr is not None:
                try:
                    sessions = mgr.get_sessions()
                    session = sessions[0] if sessions else None
                except Exception:
                    session = None
            if session is not None:
                info = session.get_playback_info()
                status = info.playback_status if info else None
                try:
                    return int(status) == 4          # PLAYING
                except Exception:
                    return "PLAYING" in str(status).upper()
        except Exception:
            pass

        # Heuristic fallback: obvious media marker in the active window title.
        try:
            title, _proc = _foreground_title()
            low = (title or "").lower()
            if any(m in low for m in
                   (" - youtube", "youtube.com", "netflix", "prime video",
                    "spotify", "soundcloud", "- vlc", " - mpv")):
                return True
        except Exception:
            pass
        return False

    async def _media_active(self) -> bool:
        """Media is 'active' when Windows reports something playing."""
        return await self._system_media_playing()

    async def _run_smart_ears(self) -> None:
        """Background task: keep PTT in step with media playback.

        While media plays the microphone is noise (a soundtrack, a call, a
        video), so hands-free listening would stream all of it to the model.
        Smart Ears closes that door automatically by holding push-to-talk ON,
        and opens it again — back to ordinary hands-free — when media stops.
        It never touches a state the user set: the pre-media PTT preference is
        remembered and restored.
        """
        while True:
            await asyncio.sleep(3)
            if not self._dynamic_ptt_enabled:
                continue
            try:
                media = await self._media_active()
            except Exception:
                continue

            if media and not self._media_ptt_on:
                self._ptt_before_media = self._ptt_enabled
                self._media_ptt_on     = True
                try:
                    self.set_push_to_talk(True)
                    self.ui.write_log(
                        "SYS: Smart Ears — media detected, push-to-talk engaged. "
                        "Hold the talk key to speak.")
                except Exception as e:
                    print(f"[SmartEars] ⚠️ Could not engage PTT: {e}")
                    self._media_ptt_on = False
            elif not media and self._media_ptt_on:
                self._media_ptt_on = False
                try:
                    self.set_push_to_talk(self._ptt_before_media)
                    self.ui.write_log(
                        "SYS: Smart Ears — media stopped, hands-free listening restored.")
                except Exception as e:
                    print(f"[SmartEars] ⚠️ Could not restore hands-free: {e}")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        _trace("INTERRUPT", "interrupt() — draining playback, mic reopened")
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        # The echo tail opened by set_speaking(False) is deliberately LEFT ALONE.
        #
        # Draining the queue does not silence the speakers: the device buffer
        # still holds a couple of hundred milliseconds that are about to be
        # heard. Streaming the microphone through that without a guard is how the
        # assistant hears the end of the sentence it just abandoned and answers
        # it. The tail already handles the genuine case correctly — it passes a
        # real voice through and ends itself on the first block that is not our
        # echo — so a user who is mid-sentence loses at most one block (~64 ms)
        # and a cough that triggered a false positive does not become input.
        self._barge.reset()
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = f"[CURRENT DATE & TIME]\nRight now it is: {time_str}\n"

        # Identity injection — overrides any hardcoded name in prompt.txt
        # Address form is a property of the language being spoken, so it is
        # stated as a principle rather than a two-language lookup — the model
        # already knows the respectful register of whatever language it is in.
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else 'ADDRESS: Address the user with the ordinary respectful form '
                      'for a superior in the language you are currently speaking — '
                      '"sir" in English, its everyday equivalent in any other '
                      'language. Never an archaic or aristocratic form, and never '
                      'the form from a different language than the one you are '
                      'speaking in this sentence.')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n"
            "LANGUAGE: Speak natural conversational Bengali mixed with English "
            "technical terms (Banglish). Understand native Bangladeshi accents. "
            "Never speak Hindi or Hinglish.\n\n"
        )

        # Everything the model is told about *itself* is derived here, not
        # written into prompt.txt: the name comes from config, the platform from
        # the host, the capability list from the registries that were just
        # discovered. Rename the assistant, add a plugin or move to another OS
        # and this follows without anyone editing a prompt.
        _all_decls = (TOOL_DECLARATIONS
                      + self._action_registry.get_tool_declarations()
                      + self._plugin_registry.get_tool_declarations())
        _names = {(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        sys_prompt = _render_prompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "limits": _describe_limits(
                has_vision=False,        # manual screen capture only — never model-driven
                has_mic=True,
            ),
        })

        _is_study = study_guard.study_active()
        mode_ctx = (
            "[CURRENT ACTIVE MODE]\n"
            + (
                "ACTIVE MODE: STUDY MODE (STUDY_MODE = ON).\n"
                "You are currently INSIDE the Universal Learning Station (Study Mode). "
                "The Study Board is open on screen. "
                "Act as a dedicated, warm, encouraging Professor and Mentor.\n\n"
                if _is_study else
                "ACTIVE MODE: NORMAL MODE (STUDY_MODE = OFF).\n"
                "You are currently in Normal Assistant HUD Mode (Study Mode is OFF). "
                "Act as a sharp, helpful study-first assistant in the Secure Study Edition.\n\n"
            )
        )

        # [STUDY_PROGRESS] — a concise summary injected from the local SQLite
        # vault (data/study_vault.db) so the mentor knows how far the student
        # has progressed without any file upload or external storage.
        try:
            from core.study_database import get_study_progress_summary, get_textbook_catalog, report_storage_path
            try:
                from actions.study_kb_lookup import INBOX_DIR, ingest_files_or_folders
                if any(INBOX_DIR.iterdir()):
                    ingest_files_or_folders([str(INBOX_DIR)])
            except Exception:
                pass
            _cat = get_textbook_catalog()
            if _cat:
                _cat_lines = [f"- {c['subject']} > {c['chapter']} ({c['chunk_count']} chunks)" for c in _cat[:15]]
                mode_ctx += (
                    "[UPLOADED TEXTBOOK VAULT CATALOG — stored permanently in data/study_vault.db]\n"
                    + "\n".join(_cat_lines)
                    + "\nWhenever the student asks about any of these subjects/chapters, you MUST call `study_kb_lookup` "
                      "(action='search') to retrieve their exact textbook definitions, formulas, and examples!\n\n"
                )
            _prog = get_study_progress_summary()
            _lines = []
            _sess = (_prog.get("recent_sessions") or [])[:5]
            if _sess:
                _lines.append("Last study sessions:")
                for _s in _sess:
                    _lines.append(f"- {_s['created_at'][:10]} {_s['subject']} — '{_s['topic']}'"
                                  + (f" ({_s['duration_min']:.0f} min)" if _s.get('duration_min') else ""))
            _tps = (_prog.get("topics") or [])[:8]
            if _tps:
                _lines.append("Chapter mastery (target 5):")
                for _t in _tps:
                    _lines.append(f"- {_t['subject']} / {_t['chapter']}: {_t['mastery_level']} "
                                  f"(last reviewed {_t['last_reviewed'][:10]})")
            _ctxs = (_prog.get("recent_context") or [])[:5]
            if _ctxs:
                _lines.append("Recently recalled concepts:")
                for _c in _ctxs:
                    _lines.append(f"- {_c['subject']} / {_c['key_concept']}")
            if _lines:
                mode_ctx += (
                    "[STUDY_PROGRESS — read locally from data/study_vault.db]\n"
                    + "\n".join(_lines)
                    + "\nUse this to adapt difficulty, encourage revision of weak chapters, "
                      "and never re-teach a topic the student has already mastered.\n\n"
                )
                report_storage_path("progress injected into mentor context")
        except Exception as e:
            print(f"[JARVIS] Study progress injection skipped: {e}")

        # [STORAGE_DISCLOSURE] — the owner-administrator transparency rule: the
        # assistant never hides where its data lives. These lines read from
        # core.security_policy.storage_inventory() at session start.
        try:
            from core.security_policy import storage_inventory
            _inv = storage_inventory()
            mode_ctx += (
                f"[STORAGE PATHS] DB: {_inv.get('study_vault_db', 'data/study_vault.db')} | "
                f"Board: {_inv.get('study_board_html', 'runtime/study_board.html')}. Disclose openly if asked.\n\n"
            )
        except Exception as e:
            print(f"[JARVIS] Storage disclosure skipped: {e}")

        parts = [time_ctx, identity_ctx, mode_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": _all_decls}],
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — JARVIS can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: JARVIS stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            if get_proactive_audio_enabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self._tuning_config())

        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        # How long the server waits through a pause before deciding your turn is
        # over. This — not the size of the prompt — is what most of the delay
        # before a reply actually is, and the default has to suit everybody, so
        # it is necessarily cautious.
        turn = get_turn_tuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        # Screenshots and camera frames are tokenised at this resolution and then
        # stay in the session's context. 'medium' keeps on-screen text legible
        # for a fraction of a full-resolution frame.
        res = get_media_resolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        # Thinking is left at the server default deliberately. Forcing the budget
        # to zero was measured on gemini-3.1-flash-live over interleaved trials
        # and did not make the first word arrive sooner — this model does not
        # appear to deliberate on the Live path, so pinning the field only adds a
        # way for a future release to behave differently. Set "thinking_enabled"
        # in config/api_keys.json to true to let it reason instead.
        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        try:
            from core.activity_hud import tool_text as _tool_text
            self.ui.set_activity(_tool_text(name, args), owner="tool")
        except Exception:
            pass
        self._tool_in_flight = True
        # Only mark speaking=False if playback queue is actually empty; otherwise let current sentence finish smoothly!
        if not self.audio_in_queue or self.audio_in_queue.empty():
            self.set_speaking(False)
            if self._turn_done_event:
                self._turn_done_event.set()
        self.ui.set_state("THINKING")

        # Smart Mode-Gating Guard: Restrict OS-level desktop modification tools during Study Mode
        if study_guard.study_active():
            _restricted_in_study = {"code_helper", "dev_agent", "desktop_control"}
            if name in _restricted_in_study:
                print(f"[StudyGuard] 🛑 Blocked restricted desktop tool '{name}' during active Study Mode")
                if not self.ui.muted:
                    self.ui.set_state("LISTENING")
                return types.FunctionResponse(
                    id=fc.id,
                    name=name,
                    response={
                        "result": (
                            "[MODE_REFUSAL] Study Mode is currently active. Use 'study_solve_problem' to explain "
                            "code or concepts on the Study Board. Desktop file operations are restricted during study sessions."
                        )
                    }
                )


        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutting down JARVIS...")
                async def _do_shutdown():
                    try:
                        await asyncio.wait_for(self._save_session_summary(), timeout=2.0)
                    except Exception:
                        pass
                    await asyncio.sleep(1.2)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())
                result = "Shutting down now. Say a 2-word goodbye." 

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                # Bounded: a tool that never returns would block the receive
                # loop and cost the whole session (see _TOOL_TIMEOUT_S).
                r = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx)),
                    timeout=_TOOL_TIMEOUT_S,
                )
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    # Bounded for the same reason as the action path above.
                    r = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                        ),
                        timeout=_TOOL_TIMEOUT_S,
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.ui.write_log(f"ERR: {name} — {str(e)[:120]}")
        finally:
            self._tool_in_flight = False
            try:
                self.ui.set_activity("", owner="tool")
            except Exception:
                pass

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")

        # A tool that declared itself NON_BLOCKING also says when its answer may
        # re-enter the conversation. Without this the model finishes whatever it
        # was saying and then reads the result out on top of it — which, for
        # something like a phone call already ringing, is exactly the noise the
        # non-blocking call was meant to avoid. Tools that declared nothing get
        # the API default and behave as they always have.
        _sched = (self._action_registry.scheduling(name)
                  or self._plugin_registry.scheduling(name))
        _extra = {"scheduling": _sched} if _sched else {}
        _now_study = study_guard.study_active()
        self._last_mode_state = _now_study
        _mode_tag = "[ACTIVE_MODE: STUDY_MODE (ON)] " if _now_study else "[ACTIVE_MODE: NORMAL_MODE (Study Mode is OFF)] "
        # Record exactly what the tool returned, failure token and all.
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": f"{_mode_tag}{result}"},
            **_extra
        )

    async def _send_realtime(self):
        carry = None
        _rt_seen = False
        _trace("SEND_RT", "realtime send loop started")
        while True:
            if carry is not None:
                msg, carry = carry, None
            else:
                msg = await self.out_queue.get()

            # Coalesce one burst into one write. The first frame still goes out
            # the instant the queue is empty — that is the steady state, so a
            # live stream pays no added latency; the batching only ever helps
            # when the socket is already behind, which is exactly when it needs
            # to catch up.
            buf = bytearray(msg["data"])
            mime = msg.get("mime_type", "audio/pcm")
            while len(buf) < _REALTIME_BATCH_BYTES:
                try:
                    nxt = self.out_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                # Never mix formats in one payload. Kept for the next write
                # rather than dropped — the queue has no way to push it back.
                if nxt.get("mime_type", "audio/pcm") != mime:
                    carry = nxt
                    break
                buf.extend(nxt["data"])

            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            try:
                await self.session.send_realtime_input(
                    audio=types.Blob(
                        data=bytes(buf),
                        mime_type=mime,
                    )
                )
                if not _rt_seen:
                    _rt_seen = True
                    _trace("SEND_RT", f"first realtime frame out ({len(buf):,} bytes {mime})")
            except Exception:
                print(f"[SEND_RT] ❌ send_realtime_input raised ({_tid()}):")
                traceback.print_exc()
                raise

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        _trace("LISTEN", "mic loop started")
        loop = asyncio.get_event_loop()

        _cb_seen = {"n": 0}

        def callback(indata, frames, time_info, status):
            if _cb_seen["n"] == 0:
                _cb_seen["n"] = 1
                _trace("LISTEN_CB", "first mic frame received (PortAudio thread)")
            # ── Wake-word gate ───────────────────────────────────────────────
            # While asleep, the mic audio NEVER goes to Gemini (nothing is
            # streamed, so JARVIS can't respond to speech not addressed to it and
            # nothing leaves the machine). Frames are instead handed to the local
            # detector, which runs its model in ITS OWN thread — the cost here is
            # only a queue push, so the audio path is never slowed. When wake word
            # is off (default) or we're awake, this is a single boolean check.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                # Phone is the microphone: the PC mic must not wake JARVIS either.
                if det is not None and not self._phone_blocks_pc_mic():
                    det.feed(indata)
                return
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            # ── Barge-in ─────────────────────────────────────────────────────
            # While JARVIS talks the mic is not streamed, but it is still worth
            # listening to locally: if the user starts speaking, cut the answer
            # short the way a person would stop when interrupted.
            #
            # The whole difficulty is echo — on speakers the mic hears JARVIS.
            # So the test is not "is the mic loud" but "is the mic louder than
            # the echo of what we are playing right now", sustained long enough
            # that a cough or a keystroke cannot trigger it.
            if jarvis_speaking:
                # Nothing is streamed while JARVIS talks. The microphone is still
                # *listened* to locally, though, so the user can talk over it.
                #
                # Note the speaking state first, whatever we do next: this is
                # where the detector sees the rising edge that starts a new reply,
                # and a missed edge would let evidence from the previous sentence
                # count towards cutting off this one.
                self._barge.note_speaking(True)

                # The phone is the microphone: the PC mic hears the room from
                # wherever the desktop is, which is not where the user is. Letting
                # it interrupt would have a conversation on the phone cut short by
                # a television in the next room. The phone relay does its own
                # interruption handling against the stream it actually receives.
                if not self._barge_enabled or self._phone_blocks_pc_mic():
                    return

                # Interrupting by voice. The hard part is echo — on speakers the
                # mic hears JARVIS — so the test is not "is the mic loud" but
                # "is this a different voice from the one we are playing", held
                # for long enough that a cough or a keystroke cannot trigger it.
                # EchoGuard answers the first; BargeInDetector answers the second.
                #
                # The decision is made here on the audio thread, but stopping
                # playback touches an asyncio queue, which is not thread-safe, so
                # the interrupt itself is marshalled onto the event loop. This
                # callback must return fast — it is a real-time audio thread, and
                # blocking it drops microphone samples.
                try:
                    if self._barge.consider(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        _loop = self._loop
                        if _loop is not None and not _loop.is_closed():
                            _loop.call_soon_threadsafe(self.interrupt)
                except Exception:
                    pass   # never let a barge-in decision kill the mic stream
                return

            # ── Echo tail ────────────────────────────────────────────────────
            # The speaking flag has dropped but the speakers have not finished.
            # Sending this to the model is how an assistant hears itself, decides
            # it was addressed, and answers its own last sentence. The microphone
            # stays OPEN — the guard only drops blocks that are our own voice, so
            # replying the instant it stops still works.
            #
            # The speaking flag dropping is also the falling edge for barge-in.
            # The reset itself happens on the *next* rising edge rather than
            # here, which is deliberate: clearing on the way down would leave a
            # window in which the state is already blank while the old reply is
            # still being classified. Tying it to the start of a reply means the
            # slate is clean at exactly the moment it needs to be.
            self._barge.note_speaking(False)
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        return
                    self._tail_until = 0.0      # a real voice ends the tail early
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            # ── Push-to-talk ─────────────────────────────────────────────────
            # When it is on the microphone is closed by default and the chord
            # opens it, which is the whole point: nothing leaves the machine
            # unless you are holding the key.
            if self._ptt_enabled and not self._ptt_held:
                return
            
            if not self.ui.muted and not self._phone_blocks_pc_mic():
                # Room noise is replaced with silence so the server's speech
                # detector hears a clean gap where you stopped talking.
                block = indata
                if self._noise_gate is not None:
                    try:
                        block = self._noise_gate.process(indata)
                    except Exception:
                        block = indata
                data = block.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(block))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                    extra_settings=audio_devices.extra_settings(dev, "input"),
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        _trace("RECV", "receive loop started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    # ── GoAway ───────────────────────────────────────────────
                    # The Live API does not let a session run forever. It sends
                    # this to warn that the connection is about to close, then
                    # force-closes with 1008 if the client has not already left.
                    #
                    # Not reading it is why a long conversation ended with
                    # "1008 None. Connection aborted because the client failed to
                    # close the connection after receiving a GoAway signal" and an
                    # ExceptionGroup on the next launch — the session was killed
                    # rather than cycled. Leaving first is the whole fix.
                    #
                    # Immediately, not after `time_left`: that field is a hint,
                    # and anything slow in this loop (a tool call, a long
                    # generation) lets the 1008 land first. Rebuilding now costs
                    # one reconnect and keeps the conversation, because the
                    # resumption handle captured below is replayed into the next
                    # session — so this is a hiccup, not a reset.
                    _goaway = getattr(response, "go_away", None)
                    if _goaway is not None:
                        _left = getattr(_goaway, "time_left", None)
                        print(f"[JARVIS] 🔄 Server is cycling the session "
                              f"(time_left={_left!r}) — reconnecting.")
                        self.ui.write_log(
                            "SYS: Server is cycling the connection — reconnecting..."
                        )
                        raise _ReconnectSignal(keep_context=True)

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    # ── Token accounting ─────────────────────────────────────
                    # The server reports the real prompt size here, which is the
                    # only honest number for the fixed per-session cost: the
                    # system prompt, every tool declaration and the memory block
                    # are all resent on each connect. Without it, trimming the
                    # payload is guesswork. Logged on change only — the server
                    # repeats the same totals on many messages.
                    _um = getattr(response, "usage_metadata", None)
                    if _um is not None:
                        _p = getattr(_um, "prompt_token_count", None)
                        _t = getattr(_um, "total_token_count", None)
                        if _p is not None and _p != self._last_prompt_tokens:
                            self._last_prompt_tokens = _p
                            print(f"[JARVIS] 📊 Prompt tokens {_p}"
                                  + (f", total {_t}" if _t is not None else ""))

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            if self._dashboard and getattr(self._dashboard, "_audio_out_clients", None):
                                asyncio.create_task(self._dashboard.broadcast_audio(_audio_data))
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            # A turn that involves a tool call passes through
                            # several turn_completes, and the API re-sends the
                            # tail of the transcript across them. Comparing only
                            # against the previous chunk missed that — once
                            # out_buf had been flushed and emptied, the repeat
                            # sailed straight back in, which logged the answer
                            # twice AND made the avatar mouth it twice.
                            if txt and not _is_repeat_chunk(txt, out_buf):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self._last_out_logged = ""   # new exchange
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            # Second line of defence: even if a repeat slips
                            # into a *fresh* buffer after a flush, never log the
                            # same answer (or a tail of it) twice in a row.
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                self._last_out_logged = full_out
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            _trace("TOOL", f"dispatch {fc.name} args={dict(fc.args or {})}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except _ReconnectSignal:
            # A deliberate rebuild — a GoAway, or a settings change routed here.
            # Not an error, so it must not be logged as one: without this it is
            # caught by the `except Exception` below and printed with a full
            # traceback, which reads like a crash in the log.
            raise
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            try:
                _api = sd.query_hostapis(sd.query_devices(_spk_dev)["hostapi"])["name"]
            except Exception:
                _api = "?"
            print(f"[JARVIS] 🔊 Output device: {_spk_name} [{_api}]")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
                extra_settings=audio_devices.extra_settings(dev, "output"),
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        # Ask the device how far behind the speakers actually are, rather than
        # assuming. This is what the echo tail is sized from, so a machine with a
        # large audio buffer gets a correspondingly longer guard — and one with a
        # tiny buffer is not penalised with a delay it does not need.
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[JARVIS] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Drive the HUD Arc Reactor Core and waveform from JARVIS's own voice
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    lvl = _pcm_level(pcm)
                    self.ui.set_audio_level(lvl)
                    self._out_level = lvl
                    self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                # Phone-only mode: the app asked for JARVIS's voice on the phone,
                # so skip the PC speaker write. The frame was already sent to the
                # phone by `broadcast_audio` above; the HUD level still animates.
                if self._phone_audio_only():
                    continue

                try:
                    _q_before = self.audio_in_queue.qsize()
                    _t_write  = time.monotonic()
                    await asyncio.to_thread(stream.write, bytes(batch))
                    _write_ms = (time.monotonic() - _t_write) * 1000
                    # "Sometimes it lags" is a queue-depth story: if the speaker
                    # write cannot keep up with the model's audio, the backlog is
                    # the lag the user hears. Log it (throttled) instead of
                    # guessing.
                    if (_q_before > 6 or _write_ms > 150) \
                            and (time.monotonic() - self._last_lag_note > 2.0):
                        self._last_lag_note = time.monotonic()
                        _trace("PLAY_LAG", f"backlog={_q_before} chunks, "
                                           f"write={_write_ms:.0f} ms, batch={len(batch)} B")
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning greeting ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """Study Edition greeting — 100% local, no news fetch.

        One short greeting: time, language, and (when they studied before) a
        recall hook showing where the last study session left off. Study
        progress comes from the local SQLite vault, never the network.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Instant greeting (no tools) ──────────────────────────────────────
        # The greeting fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        # Local study-vault recall — where the student left off, zero network.
        study_clause = ""
        try:
            from core.study_database import get_study_progress_summary
            progress = await asyncio.to_thread(get_study_progress_summary)
            recent = (progress or {}).get("recent_sessions") or []
            if recent:
                first = recent[0]
                subj = first.get("subject") or ""
                topic = first.get("topic") or ""
                if subj or topic:
                    study_clause = (
                        " Also offer to continue their study session"
                        + (f" on {subj}" if subj else "")
                        + (f", topic '{topic}'" if topic else "")
                        + " — ask if they want a quiz, flashcards or explanations to resume."
                    )
        except Exception as e:
            print(f"[JARVIS] Study recall skipped: {e}")

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and offer to help with "
            f"today's study plan.{session_clause}{study_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p1}]},
            turn_complete=True,
        )
        print("[JARVIS] Morning greeting (study edition) sent.")

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from core import gemini
            summary = await asyncio.to_thread(
                gemini.text, prompt, gemini.SMART, None, 30_000,
            )
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Plugin background monitors ───────────────────────────────────────────────

    async def _run_plugin_backgrounds(self) -> None:
        """Start one timer per plugin that declared a BACKGROUND spec.

        The plugin owns the work and the interval; this only supplies the
        schedule and the player handle, so a drop-in watcher needs no core edit.
        Each monitor runs in its own task and its exceptions are contained.
        """
        try:
            plugins = self._plugin_registry.background_plugins()
        except Exception as e:
            print(f"[Plugins] ⚠️ Background discovery failed: {e}")
            return
        for p in plugins:
            print(f"[Plugins] ⚙ Background monitor: {p['name']} every {p['interval']}s")
            asyncio.create_task(
                self._plugin_background_loop(p["name"], p["interval"], p["tick"]))

    async def _plugin_background_loop(self, name: str, interval: int, tick) -> None:
        """Fire one plugin's tick on its own cadence, off-thread, until stopped."""
        await asyncio.sleep(interval)          # first tick after one interval
        while True:
            try:
                if get_plugin_enabled(name):
                    await asyncio.to_thread(tick, self.ui, None)
            except Exception as e:
                print(f"[Plugins] ⚠️ {name} background tick failed: {e}")
            await asyncio.sleep(interval)

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            self._phone_last = time.monotonic()
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _phone_blocks_pc_mic(self) -> bool:
        """True while the PHONE is the microphone, so the PC mic sends nothing.

        Two sources hearing at once confuse the model, and a cough or a keyboard
        next to the PC could "speak" for you. The old check only looked at whether
        phone audio had arrived in the last second, so every pause in speech
        handed the PC mic back. Now it stays shut for as long as the phone's
        microphone connection is OPEN, plus a short hold after it closes.
        """
        d = self._dashboard
        if d is not None and getattr(d, "phone_mic_open", 0) > 0:
            return True
        return self._phone_active or (time.monotonic() - self._phone_last) < 1.5

    def _phone_audio_only(self) -> bool:
        """True while the phone has claimed JARVIS's voice (`POST /api/audio-mode`).

        The app's speaker button sets this. The audio is still pushed to the
        phone over `/ws/jarvis-audio`; only the PC speaker write is skipped, so
        the room does not hear the reply twice. The dashboard clears the flag
        when the last phone listener disconnects.

        Requires a live listener: if the flag is set while no phone is actually
        attached, muting the PC would leave JARVIS silent everywhere — speech
        cut off with nothing to hear it.
        """
        d = self._dashboard
        if d is None:
            return False
        if not getattr(d, "_audio_out_clients", None):
            return False
        return bool(getattr(d, "phone_audio_only", False))

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    def _on_phone_file_uploaded(self, file_path: str) -> None:
        from pathlib import Path as _P
        self.ui.current_file = file_path
        fname = _P(file_path).name
        self.ui.write_log(f"SYS: 📱 File received from phone: {fname}")
        if self._loop and self.session:
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"role": "user", "parts": [{
                        "text": f"[SYSTEM: The user just uploaded a file from their phone: '{file_path}'. You can analyze or process it using `file_processor` if they ask.]"
                    }]},
                    turn_complete=False,
                ),
                self._loop,
            )

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    # A remote command is deliberate control and the phone user
                    # has no desktop WAKE button — so it wakes JARVIS if asleep.
                    if self._wake_enabled and not self._awake:
                        self.wake(reason="remote command")
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        # Declared at the top of the function rather than at the assignment
        # below: `global` must precede any use of the name in the same scope,
        # and the connect loop reads LIVE_MODEL (to seed its failure handler)
        # before it writes it.
        global LIVE_MODEL

        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()
        self._start_watchdog()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            self._dashboard.set_file_upload_callback(self._on_phone_file_uploaded)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            # Pre-seed the two names the failure handler below reads. They are
            # assigned a few lines into the try, but the handler runs when that
            # try fails BEFORE reaching them — and `except BaseException` catches
            # everything, including an AttributeError from a bad import or a typo
            # in a helper this loop calls. Reading them unset then raised
            # UnboundLocalError inside the handler, which escaped the while-loop
            # and killed the assistant's thread: the window stayed open on
            # "THINKING" with no microphone and no reply, and the real error was
            # never reported. Seeding them costs nothing and makes every startup
            # failure reach the ladder logic below, which is where it can be
            # reported and retried.
            live_key, live_model = "", ""
            _resumed_with = False
            # True when this iteration is rebuilding a session that is still
            # mid-conversation — a GoAway cycle, a voice change, a device
            # switch — as opposed to the session genuinely ending. The `finally`
            # below summarises and CLEARS `_session_log` when it is long enough,
            # which is right at the end of a conversation and wrong in the
            # middle of one: a GoAway cycle every few minutes would summarise
            # the same conversation repeatedly, rewrite long_term.json each
            # time, and wipe the transcript the ongoing session is still
            # building. Set just before each `continue` that keeps the context.
            _mid_session_rebuild = False
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                # Pick the rung to open the conversation on. A model resting
                # off a quota limit is skipped; the name is published back to
                # LIVE_MODEL so plugins follow whatever is actually in use.
                live_key, live_model = _gemini.live_target()
                LIVE_MODEL = live_model
                print(f"[JARVIS] Live model: {live_model} "
                      f"(API key {_gemini.key_label(live_key)}, "
                      f"{len(_gemini.api_keys())} configured)")

                _resumed_with = self._resume_handle is not None
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries proactive audio; if it gets rejected we fall
                # back to v1beta.
                client = genai.Client(
                    api_key=live_key or _get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=live_model, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._interrupted          = False
                    # A rebuild must not inherit a half-gathered interruption
                    # from the session that just ended.
                    self._barge.reset()

                    print("[JARVIS] Connected.")
                    # A connection that succeeds clears the network backoff: it is
                    # now known that the machine can reach the API, so the next
                    # drop must retry in 3s, not in the 60s a long outage grew it
                    # to. Without this the delay stayed at the peak forever, and
                    # the log showed 6s → 12s → 24s with no way back down.
                    self._conn_backoff = 3
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    # Wake word: if enabled, come up ASLEEP (mic gated, silent)
                    # until the user says "Hey Jarvis" or taps wake in the UI.
                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log("SYS: JARVIS online — sleeping. Say 'Hey Jarvis' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: JARVIS online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_smart_ears())
                    tg.create_task(self._run_plugin_backgrounds())
                    tg.create_task(self._run_sleep_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                #
                # A cancellation — bare, or wrapped in the TaskGroup's group — is
                # that shutdown already in progress: the loop is being torn down,
                # not the network failing. It used to fall through to the network
                # branch below (which listed CancelledError) and be swallowed into
                # a retry. `asyncio.run` then finished closing the default
                # executor while this loop kept spinning, so every retry died
                # inside `loop.getaddrinfo` with "cannot schedule new futures
                # after shutdown" and the UI sat on "Connection failed" forever.
                if _contains_cancel(e):
                    raise

                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    # Keeping the context means the user's conversation is still
                    # going: the rebuild is a hiccup, not an ending, so the
                    # summary-and-clear in `finally` must not run. Dropping it
                    # (a voice change) really is the end of that conversation,
                    # so the summary is still wanted there — hence the flag
                    # simply mirrors whether the context is kept.
                    _mid_session_rebuild = _keep_context_of(e)
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[JARVIS] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Out of quota, or this model is not available to this key —
                # step down the ladder and reconnect straight away. This is the
                # difference between "JARVIS is quieter today" and "JARVIS does
                # not start today": one model means one daily limit, and the
                # limit always arrives mid-conversation.
                if _gemini.note_live_failure(live_model, err_str, key=live_key):
                    nxt_key, nxt = _gemini.live_target()
                    moved = (nxt_key, nxt) != (live_key, live_model)

                    # Every key rejected. Reconnecting would reach the same wall
                    # with the same dead key, so ask for a new one instead. This
                    # is the one failure a ladder cannot climb out of by itself:
                    # there is nothing below it to step onto.
                    if _gemini.is_key_error(err_str) and not moved:
                        self.ui.write_log(
                            "ERR: The API key was rejected and no other key is "
                            "configured — please enter a valid key."
                        )
                        self.ui.set_state("SLEEPING")
                        self.ui.prompt_reconfig()
                        while not self.ui._win._ready:
                            await asyncio.sleep(1)
                        print("[JARVIS] New API key saved — reconnecting...")
                        self._conn_backoff = 3
                        continue

                    if moved and nxt_key != live_key:
                        _ks = _gemini.api_keys()
                        _n  = _ks.index(nxt_key) + 1 if nxt_key in _ks else "?"
                        _msg = (f"SYS: Switching to backup API key {_n} — the "
                                f"previous key is out of quota or was rejected.")
                    elif moved:
                        _msg = (f"SYS: Switching to {nxt.split('/')[-1]} — the "
                                f"previous model is out of quota.")
                    else:
                        _msg = "SYS: Every live model is rate-limited — retrying."
                    self.ui.write_log(_msg)
                    self._conn_backoff = 0 if moved else 15
                    if not moved:
                        await asyncio.sleep(self._conn_backoff)
                    continue

                # Turn-taking / media / thinking knobs rejected by the server
                # (preview API drift) — drop them first, because they are the
                # newest fields and the cheapest to lose. Proactive audio is
                # tried again on the next pass if the error persists.
                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "media_resolution" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[JARVIS] Live tuning rejected — reconnecting without it.")
                    continue

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    self._conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off. CancelledError
                # is deliberately NOT in this list: a cancellation is a shutdown,
                # and retrying it left the loop alive on a dead executor.
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "gaierror",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    self._conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    _dns = ("gaierror" in err_str or "getaddrinfo" in err_str)
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {self._conn_backoff}s. "
                        + ("(the API host could not be resolved: DNS or the "
                           "internet connection is down)" if _dns
                           else "(a VPN may be required)")
                    )
                else:
                    # Something none of the cases above recognised. It used to
                    # be logged to the console and nowhere else, which under
                    # pythonw means nowhere at all — the app simply sat there
                    # looking broken. Say it in the activity log too, in one
                    # line, so an unexpected failure is visible where the user
                    # is actually looking. The traceback still goes to
                    # config/jarvis.log for the detail.
                    self._conn_backoff = 3
                    self.ui.write_log(
                        f"ERR: Could not start the session — "
                        f"{type(e).__name__}: {err_str[:140]}"
                    )
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns) that has
                # actually ENDED. A mid-conversation rebuild — a GoAway cycle,
                # an audio-device switch — reaches this `finally` too, and
                # saving there would summarise and clear a conversation that is
                # still in progress (see `_mid_session_rebuild` above).
                if len(self._session_log) >= 3 and not _mid_session_rebuild:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    # Declare DPI awareness before anything else exists.
    #
    # Windows defaults a process to DPI-UNAWARE, which means the OS lies about
    # the screen: a 1920x1080 display at 150% scaling reports 1280x720 and every
    # capture comes back upscaled and blurry. Worse, the mode can be changed
    # under us — mss asks for PerMonitor-v2 on its first grab, pyautogui asks for
    # the older v1 at import — so a screen size cached before the switch is
    # wrong, which is how a click ends up somewhere other than where it was
    # aimed.
    #
    # This has to run before QApplication is constructed (inside JarvisUI below),
    # because Qt sets its own awareness at that moment and will not upgrade
    # later. Setting it here is what makes the whole targeting chain agree on
    # what a pixel is.
    try:
        from core.screen_geometry import ensure_dpi_awareness, describe
        ensure_dpi_awareness()
        print(f"[Screen] {describe()}")
    except Exception as e:
        print(f"[Screen] DPI setup failed (clicks may miss on a scaled display): {e}")

    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

_INSTANCE_LOCK = None      # held for the life of the process


def _single_instance_or_exit() -> None:
    """Refuse to start a second copy of JARVIS.

    config/jarvis.log showed two copies starting in the same second (the desktop
    shortcut and auto-start): both connected to Gemini, both opened the mic, and
    the second could not bind port 8000, so the phone reached the wrong copy and
    said "Link Expired". The OS frees this lock by itself if JARVIS crashes.
    Set JARVIS_ALLOW_MULTIPLE=1 to run two on purpose (e.g. while developing).
    """
    global _INSTANCE_LOCK
    import os
    if os.environ.get("JARVIS_ALLOW_MULTIPLE") == "1":
        return
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", 47831))
        s.listen(1)
    except OSError:
        msg = ("JARVIS is already running.\n\nLook for it in the taskbar or the system tray. "
               "Only one copy can run at a time.")
        print("[JARVIS] " + msg.replace("\n", " "))
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, "J.A.R.V.I.S", 0x40)
            except Exception:
                pass
        sys.exit(0)
    _INSTANCE_LOCK = s


if __name__ == "__main__":
    _single_instance_or_exit()
    main()
