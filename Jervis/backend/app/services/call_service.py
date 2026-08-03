import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_session import CallSession


class CallService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_session(
        self,
        tenant_id: str,
        room_name: str,
        session_type: str = "web",
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
        lead_id: Optional[str] = None,
    ) -> CallSession:
        session = CallSession(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID(tenant_id),
            lead_id=uuid.UUID(lead_id) if lead_id else None,
            session_type=session_type,
            livekit_room=room_name,
            status="ringing",
            fsm_state="idle",
            fsm_stack=[],
            intent_context={
                "current_intent": "idle",
                "mentioned_slots": [],
                "preferred_service": None,
                "customer_name": customer_name,
                "customer_phone": customer_phone,
                "pending_confirmation": False,
                "last_question": None,
                "user_answered_yes_no": None,
                "conversation_turns": 0,
            },
            agent_config_snapshot=await self._get_agent_snapshot(tenant_id),
            transcript=[],
             started_at=datetime.now(timezone.utc),
        )
        self.db.add(session)
        await self.db.flush()
        return session

    async def _get_agent_snapshot(self, tenant_id: str) -> dict:
        from app.models.ai_config import AIConfig
        from app.models.voice_config import VoiceConfig

        ai = await self.db.get(AIConfig, uuid.UUID(tenant_id))
        voice = await self.db.get(VoiceConfig, uuid.UUID(tenant_id))

        if not ai:
            ai_result = await self.db.execute(
                text("SELECT * FROM ai_configs WHERE tenant_id = :tid LIMIT 1"),
                {"tid": uuid.UUID(tenant_id)},
            )
            row = ai_result.scalar_one_or_none()
            if row:
                ai = row

        if not voice:
            voice_result = await self.db.execute(
                text("SELECT * FROM voice_configs WHERE tenant_id = :tid LIMIT 1"),
                {"tid": uuid.UUID(tenant_id)},
            )
            row = voice_result.scalar_one_or_none()
            if row:
                voice = row

        return {
            "system_prompt": ai.system_prompt if ai else "You are Clara, a helpful booking assistant for a dental clinic.",
            "llm_provider": ai.llm_provider if ai else "groq",
            "llm_model": ai.llm_model if ai else "llama-3.1-8b-instant",
            "stt_provider": ai.stt_provider if ai else "whisper",
            "tts_provider": ai.tts_provider if ai else "kokoro",
            "temperature": float(ai.temperature) if ai and ai.temperature is not None else 0.7,
            "voice_id": voice.voice_id if voice else "af_heart",
            "language": voice.language if voice else "en-US",
        }

    async def update_transcript(
        self,
        session_id: str,
        event: dict,
        fsm_state: Optional[str] = None,
        intent_context: Optional[dict] = None,
    ):
        session = await self.db.get(CallSession, uuid.UUID(session_id))
        if not session:
            return

        transcript = list(session.transcript or [])
        transcript.append(event)
        session.transcript = transcript

        if fsm_state:
            session.fsm_state = fsm_state
        if intent_context is not None:
            existing = dict(session.intent_context or {})
            existing.update(intent_context)
            existing["conversation_turns"] = existing.get("conversation_turns", 0) + 1
            session.intent_context = existing

        session.updated_at = datetime.now(timezone.utc)

    async def get_context(self, session_id: str) -> dict:
        session = await self.db.get(CallSession, uuid.UUID(session_id))
        if not session:
            return {}
        return {
            "fsm_state": session.fsm_state,
            "fsm_stack": list(session.fsm_stack or []),
            "intent_context": dict(session.intent_context or {}),
            "transcript": list(session.transcript or []),
            "agent_config_snapshot": dict(session.agent_config_snapshot or {}),
            "status": session.status,
        }

    async def transition_state(
        self, session_id: str, new_state: str, push_stack: bool = False
    ):
        session = await self.db.get(CallSession, uuid.UUID(session_id))
        if not session:
            return

        if push_stack:
            stack = list(session.fsm_stack or [])
            stack.append(session.fsm_state)
            session.fsm_stack = stack

        session.fsm_state = new_state
        session.updated_at = datetime.now(timezone.utc)

    async def push_fsm_state(self, session_id: str):
        session = await self.db.get(CallSession, uuid.UUID(session_id))
        if not session:
            return
        stack = list(session.fsm_stack or [])
        stack.append(session.fsm_state)
        session.fsm_stack = stack
        session.updated_at = datetime.now(timezone.utc)

    async def pop_fsm_state(self, session_id: str) -> Optional[str]:
        session = await self.db.get(CallSession, uuid.UUID(session_id))
        if not session:
            return None
        stack = list(session.fsm_stack or [])
        if not stack:
            return None
        previous = stack.pop()
        session.fsm_stack = stack
        session.fsm_state = previous
        session.updated_at = datetime.now(timezone.utc)
        return previous

    async def end_session(self, session_id: str, ended_reason: str = "completed"):
        session = await self.db.get(CallSession, uuid.UUID(session_id))
        if not session:
            return
        session.status = "ended"
        session.ended_reason = ended_reason
        session.ended_at = datetime.now(timezone.utc)
        if session.started_at:
            delta = session.ended_at - session.started_at
            session.duration_seconds = int(delta.total_seconds())
        session.updated_at = datetime.now(timezone.utc)

    async def start_session(self, session_id: str):
        """Mark a session as in-progress (e.g. when a phone call is answered)."""
        session = await self.db.get(CallSession, uuid.UUID(session_id))
        if not session:
            return
        session.status = "in_call"
        session.updated_at = datetime.now(timezone.utc)
