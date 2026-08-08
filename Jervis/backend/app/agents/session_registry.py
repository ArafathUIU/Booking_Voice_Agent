"""In-memory registry of live headless voice sessions.

The Twilio trial-native path is request/response (each <Gather> turn is a
separate HTTP POST), so the ConversationState for a call must live somewhere
between requests. This module keeps it in a process-local dict keyed by session
id. Safe for the single-worker compose deployment; entries are removed when the
call ends.
"""

import logging
import threading
from typing import Optional

from app.agents.turn_engine import TurnEngine

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_sessions: dict[str, TurnEngine] = {}


def put(session_id: str, engine: TurnEngine):
    with _lock:
        _sessions[session_id] = engine
    logger.info("Headless session registered: %s", session_id)


def get(session_id: str) -> Optional[TurnEngine]:
    with _lock:
        return _sessions.get(session_id)


def pop(session_id: str) -> Optional[TurnEngine]:
    with _lock:
        return _sessions.pop(session_id, None)


def clear():
    with _lock:
        _sessions.clear()
