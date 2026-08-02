import json
import logging
from typing import Dict
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._active: Dict[str, WebSocket] = {}

    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self._active[session_id] = websocket
        logger.info("WebSocket connected for session %s", session_id[:8])

    def disconnect(self, session_id: str):
        if session_id in self._active:
            del self._active[session_id]
            logger.info("WebSocket disconnected for session %s", session_id[:8])

    async def send_transcript(self, session_id: str, speaker: str, text: str):
        ws = self._active.get(session_id)
        if ws is None:
            return
        try:
            await ws.send_json({
                "type": "transcript",
                "speaker": speaker,
                "text": text,
            })
        except Exception:
            logger.exception("Failed to send transcript via WebSocket")

    async def send_agent_text(self, session_id: str, text: str):
        ws = self._active.get(session_id)
        if ws is None:
            return
        try:
            await ws.send_json({
                "type": "agent_text",
                "text": text,
            })
        except Exception:
            logger.exception("Failed to send agent text via WebSocket")


manager = ConnectionManager()