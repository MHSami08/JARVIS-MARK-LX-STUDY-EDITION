"""Registry audit: what action_loader / plugin_loader actually register on this branch."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.action_loader import discover_actions
from core.plugin_loader import discover_plugins

FORBIDDEN_WORDS = ("whatsapp",)
UNSAFE_HINTS = (
    "open_app", "file_controller", "file_navigator",
    "manage_file_access", "computer_control", "computer_settings", "desktop_control",
    "send_message", "ghost_operator", "temp_cleaner", "background", "proactive",
    "game", "flight", "video_player", "youtube", "google_workspace", "dev_agent",
    "code_helper", "file_processor", "weather", "undo", "system_status",
)

print("── ACTIONS ──")
actions = discover_actions(ROOT / "actions", logger=lambda m: None)
for rec in actions._all_records:
    flag = ""
    low = rec.name.lower()
    if any(w in low for w in FORBIDDEN_WORDS):
        flag = "  <<< FORBIDDEN"
    elif any(h in low for h in UNSAFE_HINTS):
        flag = "  <<< UNSAFE HINT"
    print(f"  {'OK ' if rec.valid else 'REJ'} {rec.name} ({rec.file}){flag}")

print("── PLUGINS ──")
plugins = discover_plugins(ROOT / "plugins", core_tool_names=set(actions.names()),
                           logger=lambda m: None)
for p in plugins.list_for_ui():
    flag = ""
    low = str(p.get("name", "")).lower()
    if any(w in low for w in FORBIDDEN_WORDS):
        flag = "  <<< FORBIDDEN"
    elif any(h in low for h in UNSAFE_HINTS):
        flag = "  <<< UNSAFE HINT"
    print(f"  {'OK ' if p.get('valid') else 'REJ'} {p.get('name')}{flag}")

bad = [r.name for r in actions._all_records if r.valid and any(w in r.name.lower() for w in FORBIDDEN_WORDS)]
bad += [p["name"] for p in plugins.list_for_ui() if any(w in str(p.get("name")).lower() for w in FORBIDDEN_WORDS)]
print("RESULT:", "CLEAN" if not bad else f"FORBIDDEN REGISTERED: {bad}")
sys.exit(1 if bad else 0)
