"""
actions/study_controller.py — Controls Study Station state by voice or tool call.
Handles entering/exiting Study Mode, advancing steps, checking notes, and exporting.
"""
from __future__ import annotations
from typing import Any, Dict

TOOL: Dict[str, Any] = {
    "name": "study_station_control",
    "description": (
        "Controls Study Station state: 'enter' to turn Study Mode ON, 'exit' to leave Study Mode "
        "and return to Normal HUD Mode, 'next' to advance cards/steps, 'prev' or 'back' to return to "
        "the previous step/question/card, 'restore' to restore active quiz/solution view, 'clear' to reset board, "
        "'check_notes' to review notepad, 'export' to save notes, or 'review_mistakes'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["enter", "exit", "next", "prev", "back", "restore", "clear", "check_notes", "export", "review_mistakes"],
                "description": "The control action to execute.",
            },
        },
        "required": ["action"],
    },
}


def study_station_control(parameters: Dict[str, Any], player: Any = None, **kwargs) -> str:
    action = str((parameters or {}).get("action") or "enter").strip().lower()
    if player is None:
        return "UI player unavailable."

    if action == "enter":
        player.set_study_mode(True)
        return (
            "[CURRENT_MODE: STUDY_MODE = ON] Study Mode is now ACTIVE. "
            "Adopt your Dedicated Professor/Mentor persona and confirm in one short sentence."
        )
    if action == "exit":
        try:
            from actions import study_quiz
            study_quiz.set_quiz_active(False)
        except Exception:
            pass
        player.set_study_mode(False)
        return (
            "[CURRENT_MODE: NORMAL_MODE = ON (STUDY_MODE = OFF)] Exited Study Mode and returned to "
            "Normal Assistant HUD Mode. You are NO LONGER in Study Mode. Confirm in one short sentence."
        )
    if action == "next":
        if not getattr(player, "study_mode_active", False):
            return "[CURRENT_MODE: NORMAL_MODE] Study Mode is currently OFF."
        player.study_next()
        return "[CURRENT_MODE: STUDY_MODE = ON] Advanced to the next step/card on the Study Board."
    if action in ("prev", "back"):
        if not getattr(player, "study_mode_active", False):
            return "[CURRENT_MODE: NORMAL_MODE] Study Mode is currently OFF."
        if hasattr(player, "study_prev"):
            player.study_prev()
        return "[CURRENT_MODE: STUDY_MODE = ON] Returned to the previous card/step/question on the Study Board."
    if action == "restore":
        if hasattr(player, "study_restore"):
            player.study_restore()
        return "[CURRENT_MODE: STUDY_MODE = ON] Restored the active view on the Study Board."
    if action == "clear":
        try:
            from actions import study_quiz
            study_quiz.set_quiz_active(False)
        except Exception:
            pass
        player.study_clear()
        return "[CURRENT_MODE: STUDY_MODE = ON] Cleared the Study Board."

    # Delegate check_notes / export / review_mistakes to study_mentor
    try:
        from plugins import study_mentor
        return study_mentor.run({"action": action}, player=player)
    except Exception as e:
        return f"Action '{action}' failed: {e}"


TOOL["handler"] = study_station_control
