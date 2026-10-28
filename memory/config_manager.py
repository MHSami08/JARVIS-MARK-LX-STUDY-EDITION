import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

DEFAULT_ROUTING_RULES = {
    "fast": [
        "gemini:gemini-3.1-flash-lite",
        "gemini:gemini-3.5-flash",
    ],
    "coding": [
        "gemini:gemini-3.5-flash",
        "gemini:gemini-3.1-flash-lite",
    ],
    "writing": [
        "gemini:gemini-3.5-flash",
        "gemini:gemini-3.1-flash-lite",
    ],
    "study": [
        "gemini:gemini-3.5-flash",
        "gemini:gemini-3.1-flash-lite",
    ],
    "study_fast": [
        "gemini:gemini-3.1-flash-lite",
        "gemini:gemini-3.5-flash",
    ],
}

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    ensure_config_dir()

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data["gemini_api_key"] = gemini_api_key.strip()

    if "routing_rules" not in data:
        data["routing_rules"] = DEFAULT_ROUTING_RULES

    CONFIG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'JARVIS' if not set."""
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "JARVIS"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Assistant voice ──────────────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = ["Charon", "Puck", "Algieba", "Achird", "Orus"]
DEFAULT_VOICE    = "Charon"


def get_voice() -> str:
    """Return the configured Live voice, falling back to the default if unset
    or if the stored value is not a voice we recognise."""
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    """Persist the chosen Live voice. Unknown names collapse to the default so a
    bad value can never reach the API and break the session."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    v = (voice_name or "").strip()
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_wake_word_enabled() -> bool:
    """Whether local wake-word gating is on (assistant sleeps until 'Hey Jarvis')."""
    return load_api_keys().get("wake_word_enabled", False)


def save_wake_word_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["wake_word_enabled"] = bool(enabled)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_push_to_talk_enabled() -> bool:
    """Hold-a-key-to-speak. When on, the mic is closed unless the chord is held."""
    return load_api_keys().get("push_to_talk_enabled", False)


def save_push_to_talk_enabled(enabled: bool) -> None:
    _save_flag("push_to_talk_enabled", enabled)


# ── Smart Ears (dynamic push-to-talk) ────────────────────────────────────────
# Hands-free by default; automatically switches to push-to-talk the moment media
# playback is detected (an in-app video or any Windows media session), and back
# to hands-free when it stops. OFF by default so an untouched install behaves
# exactly as before. See JarvisLive._run_smart_ears in main.py.

def get_dynamic_ptt_enabled() -> bool:
    """Whether Smart Ears auto-toggles hands-free / push-to-talk on media."""
    return bool(load_api_keys().get("dynamic_ptt_enabled", False))


def save_dynamic_ptt_enabled(enabled: bool) -> None:
    _save_flag("dynamic_ptt_enabled", enabled)


# ── Barge-in ─────────────────────────────────────────────────────────────────
# Interrupting JARVIS by speaking over it. OFF by default, and deliberately so:
# how well it works depends on the room, and an assistant that stops mid-sentence
# at the wrong moment is worse than one you have to wait out. See core/barge_in.py.

BARGE_SENSITIVITIES = ("low", "medium", "high")
DEFAULT_BARGE_SENSITIVITY = "medium"


def get_barge_in_enabled() -> bool:
    """Whether speaking over JARVIS cuts its reply short."""
    return load_api_keys().get("barge_in_enabled", False)


def save_barge_in_enabled(enabled: bool) -> None:
    _save_flag("barge_in_enabled", enabled)


def get_barge_in_sensitivity() -> str:
    """How much sustained evidence an interruption needs.

    'low'  — a noisy room or a house with other people in it: harder to trigger.
    'high' — headphones or a quiet desk: quicker to react.
    """
    v = load_api_keys().get("barge_in_sensitivity", DEFAULT_BARGE_SENSITIVITY)
    return v if v in BARGE_SENSITIVITIES else DEFAULT_BARGE_SENSITIVITY


def save_barge_in_sensitivity(value: str) -> None:
    v = str(value or "").strip().lower()
    _save_flag("barge_in_sensitivity",
               v if v in BARGE_SENSITIVITIES else DEFAULT_BARGE_SENSITIVITY)


HUD_STYLES = ("core",)


def get_hud_style() -> str:
    """The central HUD style: Arc Reactor Core is the exclusive permanent HUD."""
    return "core"


def save_hud_style(style: str = "core") -> None:
    _save_flag("hud_style", "core")


# ── Live-session tuning ──────────────────────────────────────────────────────
# Everything here is optional and has a working default, so an untouched
# config behaves exactly like a configured one. Each value is also a way out:
# if a future model dislikes one of these, set it back and nothing else changes.

def get_thinking_enabled() -> bool:
    """Whether the Live model may spend tokens thinking before it answers.

    Off by default. A voice assistant is judged on how fast it starts talking,
    and the reasoning that actually needs deliberation in this app is delegated
    to the planning tools, which run on a separate non-Live model.
    """
    return bool(load_api_keys().get("thinking_enabled", False))


def save_thinking_enabled(enabled: bool) -> None:
    _save_flag("thinking_enabled", enabled)


def get_turn_tuning() -> dict:
    """How eagerly the server decides you have stopped speaking.

    OFF by default, and that default was earned. Cutting turns shorter looks
    like a free speed win and is not: proactive audio has to judge whether an
    utterance was even addressed to the assistant, and a turn clipped early
    gives it less to judge, so it stays quiet — and the reply to your first
    sentence only arrives once your second one has given it enough context.
    That reads as the assistant being a turn behind, which is far worse than
    the fraction of a second the tuning saves.

    Turn it on with "turn_tuning": {"enabled": true} if your own microphone and
    speaking pace suit it. `silence_ms` is the one that is felt: the pause the
    server sits through before accepting your turn is over.
    """
    cfg = load_api_keys().get("turn_tuning")
    cfg = cfg if isinstance(cfg, dict) else {}

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled":    bool(cfg.get("enabled", False)),
        "silence_ms": _int("silence_ms", 550, 200, 3000),
        "prefix_ms":  _int("prefix_ms", 150, 0, 1000),
        # "high" = quicker to decide speech has ended.
        "end_sensitivity":   str(cfg.get("end_sensitivity", "high")).lower(),
        "start_sensitivity": str(cfg.get("start_sensitivity", "default")).lower(),
    }


def save_turn_tuning(values: dict) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cur = data.get("turn_tuning")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    data["turn_tuning"] = cur
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_proactive_audio_enabled() -> bool:
    """Whether the model gets to decide an utterance was not aimed at it and
    stay quiet.

    On by default — it is what stops the assistant answering the room. But it
    is also the first thing to switch off if replies ever seem to arrive a turn
    late: what looks like lag is usually the model having judged your previous
    sentence as not addressed to it, and only changing its mind once the next
    one arrives.
    """
    return bool(load_api_keys().get("proactive_audio", True))


def save_proactive_audio_enabled(enabled: bool) -> None:
    _save_flag("proactive_audio", enabled)


MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")


def get_media_resolution() -> str:
    """How finely the model tokenises the screenshots and camera frames it is
    sent. 'medium' keeps on-screen text readable at a fraction of the tokens a
    full-resolution frame costs; 'low' is cheaper still but starts losing small
    text, which is most of what screen captures are for."""
    v = str(load_api_keys().get("media_resolution", "medium")).strip().lower()
    return v if v in MEDIA_RESOLUTIONS else "medium"


def save_media_resolution(value: str) -> None:
    v = str(value or "").strip().lower()
    _save_flag("media_resolution", v if v in MEDIA_RESOLUTIONS else "medium")


# ── Screen targeting ─────────────────────────────────────────────────────────
# How JARVIS turns "click the Save button" into a pixel. The defaults are the
# ones that make a click land exactly, and each can be turned off by someone who
# would rather trade accuracy for speed or privacy.

GROUNDING_LAYERS = ("uia", "ocr", "vision")


def get_grounding_layers() -> tuple[str, ...]:
    """Which targeting layers may be used, cheapest first.

    "uia" reads the Windows accessibility tree — no model call, ~10-40 ms, and
    pixel-exact because Windows computed the rectangle itself. "ocr" reads the
    visible text, which covers Electron, Chrome and games that the tree cannot
    see. "vision" asks a model, and is the slow, approximate last resort.

    An empty or invalid setting falls back to all three; a layer that is not
    installed is skipped at run time regardless of what is listed here.
    """
    raw = load_api_keys().get("grounding_layers", None)
    if not isinstance(raw, (list, tuple)) or not raw:
        return GROUNDING_LAYERS
    keep = tuple(str(v).strip().lower() for v in raw
                 if str(v).strip().lower() in GROUNDING_LAYERS)
    return keep or GROUNDING_LAYERS


def save_grounding_layers(layers) -> None:
    if not isinstance(layers, (list, tuple)):
        layers = GROUNDING_LAYERS
    keep = [str(v).strip().lower() for v in layers
            if str(v).strip().lower() in GROUNDING_LAYERS]
    _patch_config(grounding_layers=keep or list(GROUNDING_LAYERS))


def get_overlay_enabled() -> bool:
    """Whether the targeting reticle is drawn before a click. On by default:
    an assistant that moves the mouse should show you where first."""
    return bool(load_api_keys().get("overlay_enabled", True))


def save_overlay_enabled(enabled: bool) -> None:
    _save_flag("overlay_enabled", enabled)


def get_overlay_dwell_ms() -> int:
    """How long the reticle stays up when a caller does not specify, in ms."""
    try:
        v = int(load_api_keys().get("overlay_dwell_ms", 1400))
    except (TypeError, ValueError):
        v = 1400
    return max(0, min(v, 10_000))


def save_overlay_dwell_ms(ms: int) -> None:
    try:
        v = max(0, min(int(ms), 10_000))
    except (TypeError, ValueError):
        v = 1400
    _patch_config(overlay_dwell_ms=v)


def get_cursor_in_capture() -> bool:
    """Whether screenshots include the mouse pointer. On by default: without it
    the model cannot see where the cursor is, which makes any instruction
    relative to it impossible to act on."""
    return bool(load_api_keys().get("cursor_in_capture", True))


def save_cursor_in_capture(enabled: bool) -> None:
    _save_flag("cursor_in_capture", enabled)


def get_click_verify() -> bool:
    """Whether a click is checked by comparing the screen before and after.
    On by default: it is what turns a silent miss into a reported failure."""
    return bool(load_api_keys().get("click_verify", True))


def save_click_verify(enabled: bool) -> None:
    _save_flag("click_verify", enabled)


def get_agent_max_steps() -> int:
    """Ceiling on how many actions an autonomous run may take."""
    try:
        v = int(load_api_keys().get("agent_max_steps", 12))
    except (TypeError, ValueError):
        v = 12
    return max(1, min(v, 40))


def save_agent_max_steps(steps: int) -> None:
    try:
        v = max(1, min(int(steps), 40))
    except (TypeError, ValueError):
        v = 12
    _patch_config(agent_max_steps=v)


def _save_flag(key: str, value) -> None:
    """Read-modify-write one key without disturbing the rest of the config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = bool(value) if isinstance(value, bool) else value
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_visual_announce_done() -> bool:
    """Speak one short "diagram is ready" line when a background figure lands. Default off."""
    return bool(load_api_keys().get("visual_announce_done", False))


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def _patch_config(**fields) -> None:
    """Read-modify-write one or more keys in api_keys.json.

    Every setter in this file open-coded this. Collapsing it here means a new
    setting is one line, and there is one place where a corrupt config file is
    handled instead of nine."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_input_device() -> str:
    """Microphone device name, or '' for the system default."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Speaker device name, or '' for the system default."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Per-plugin settings ("tokens" / connection details) ───────────────────────
# Generic store so a plugin can declare its own config fields (PLUGIN_SETTINGS)
# and the settings UI renders + persists them WITHOUT any core edit — keeping the
# drop-in model intact. Values live under plugin_config[<namespace>][<key>].
# A namespace defaults to the plugin name, but a suite of plugins (e.g. the
# several printer plugins) can share ONE namespace.
def get_plugin_config(namespace: str) -> dict:
    """All stored values for a namespace (empty dict if none set yet)."""
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    """A single value from a namespace, or `default` if unset."""
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    """Merge `values` into a namespace's stored config (read-modify-write, like
    every other helper here). Only the provided keys are touched."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_noise_gate() -> dict:
    """Microphone noise gate (core/noise_gate.py). On by default: in a quiet room
    it never closes on speech, and in a noisy one it is what lets the server hear
    where your sentence ends. Turn off with "noise_gate": {"enabled": false}."""
    cfg = load_api_keys().get("noise_gate")
    cfg = cfg if isinstance(cfg, dict) else {}
    strength = str(cfg.get("strength", "medium")).lower()
    return {
        "enabled":  bool(cfg.get("enabled", True)),
        "strength": strength if strength in ("mild", "medium", "strong") else "medium",
    }
