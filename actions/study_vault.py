# -*- coding: utf-8 -*-
"""
Local Study Progress & Context Vault Action (`actions/study_vault.py`)
Allows JARVIS to log and retrieve what the user has studied locally in SQLite.
"""

from core.study_database import (
    log_study_session,
    save_study_context,
    get_study_progress_summary
)

TOOL_SCHEMA = {
    "name": "study_vault",
    "description": "Save or retrieve the user's local study progress, completed chapters, and study context from the local SQLite database.",
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["get_progress", "log_session", "save_context"]
            },
            "subject": {"type": "string"},
            "topic": {"type": "string"},
            "summary_or_detail": {"type": "string"},
            "duration_min": {"type": "number"}
        },
        "required": ["operation"]
    }
}

def execute(
    operation: str = "get_progress",
    subject: str = "General",
    topic: str = "",
    summary_or_detail: str = "",
    duration_min: float = 15.0,
    **kwargs
) -> dict:
    if operation == "log_session":
        log_study_session(subject, topic or "General Topic", summary_or_detail, duration_min)
        return {"status": "ok", "message": f"Logged study session for {subject} - {topic}."}
    elif operation == "save_context":
        save_study_context(subject, topic or "Concept", summary_or_detail)
        return {"status": "ok", "message": f"Saved study context for {subject}."}
    else:
        return {"status": "ok", "data": get_study_progress_summary()}
