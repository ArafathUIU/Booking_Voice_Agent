"""Conversation manager for the dental clinic booking agent (pipecat flavor).

Owns the frame-level behaviour of one user utterance becoming exactly ONE
spoken response: barge-in, utterance buffering, turn deferral, and pushing the
final TTSSpeakFrame. The deterministic booking logic itself (slot extraction,
dialogue policy, tool execution, LLM small-talk, response arbitration) lives in
the transport-agnostic ``turn_engine.TurnEngine``, which is shared with the
Twilio trial-native ``<Gather>`` path so both transports behave identically.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from pipecat.frames.frames import (
    InterruptionFrame,
    TextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor

from app.agents.constants import FALLBACK_SPEECH
from app.agents.turn_engine import TurnEngine, build_outbound_greeting
from app.config import settings
from app.db.session import async_session_factory
from app.models.call_session import CallSession
from app.websockets import manager as ws_manager

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConversationManager(FrameProcessor):
    def __init__(self, session_id: str, tenant_id: str, lead_context: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self._FrameProcessor__started = True
        self.session_id = session_id
        self.tenant_id = tenant_id

        self._lead_context = dict(lead_context or {})

        # The headless booking brain (also drives the Twilio <Gather> path).
        self.engine = TurnEngine(
            session_id=session_id,
            tenant_id=tenant_id,
            lead_context=self._lead_context,
        )
        self.state = self.engine.state
        self.dialogue = self.engine.dialogue
        self.arbiter = self.engine.arbiter

        self._user_speaking = False
        self._pending_user_text = ""
        self._deferred_user_text = ""
        self._turn_in_progress = False
        self._greeting_pushed = False
        self._user_joined = False
        self._last_confidence = None

        # Persistence
        self._persist_queue: asyncio.Queue = asyncio.Queue()
        self._persist_task = None
        self._background_tasks: set[asyncio.Task] = set()
        try:
            self._persist_task = asyncio.get_running_loop().create_task(self._persist_loop())
        except RuntimeError:
            logger.warning("No running event loop; DB persistence disabled")

        # Load existing conversation history from DB
        self._history_loaded = False

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #

    async def on_user_joined(self):
        """Called once when the first participant joins the room."""
        if self._user_joined:
            return
        self._user_joined = True
        if self._greeting_pushed:
            return
        self._greeting_pushed = True

        # Load existing conversation history from DB (e.g. for resumed calls)
        await self._load_history()

        # Send loaded history to WebSocket for chat display
        await self._send_history_to_ws()

        greeting = build_outbound_greeting(self.state, self.engine_lead_context())
        logger.info("Pushing greeting")
        try:
            await self.push_frame(TTSSpeakFrame(greeting))
        except Exception:
            logger.exception("Greeting push failed")

        # Send greeting to WebSocket for chat display
        task = asyncio.create_task(ws_manager.send_transcript(self.session_id, "agent", greeting))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def engine_lead_context(self) -> dict:
        """Expose the lead context used to seed the engine (for the greeting)."""
        return getattr(self, "_lead_context", {})

    async def _load_history(self):
        """Load existing transcript from DB into conversation state history."""
        if self._history_loaded:
            return
        self._history_loaded = True
        try:
            async with async_session_factory() as db:
                session = await db.get(CallSession, uuid.UUID(self.session_id))
                if session is None:
                    logger.warning("Session %s not found for history load", self.session_id)
                    return
                transcript = list(session.transcript or [])
                for entry in transcript:
                    if entry.get("type") == "user_speech" and entry.get("text"):
                        self.state.history.append(
                            {"role": "user", "content": entry["text"]}
                        )
                    elif entry.get("type") == "agent_speech" and entry.get("text"):
                        self.state.history.append(
                            {"role": "assistant", "content": entry["text"]}
                        )
                # Sync turn_count
                self.state.turn_count = len([
                    h for h in self.state.history if h["role"] == "user"
                ])
                logger.info("Loaded %d history turns from DB", len(transcript))
        except Exception:
            logger.exception("Failed to load conversation history")

    async def _send_history_to_ws(self):
        """Send loaded conversation history to WebSocket for chat display."""
        try:
            async with async_session_factory() as db:
                session = await db.get(CallSession, uuid.UUID(self.session_id))
                if session is None:
                    return
                transcript = list(session.transcript or [])
                for entry in transcript:
                    if entry.get("type") == "user_speech" and entry.get("text"):
                        await ws_manager.send_transcript(
                            self.session_id, "user", entry["text"]
                        )
                    elif entry.get("type") == "agent_speech" and entry.get("text"):
                        await ws_manager.send_transcript(
                            self.session_id, "agent", entry["text"]
                        )
        except Exception:
            logger.exception("Failed to send history to WebSocket")

    async def shutdown(self):
        """Drain pending writes, then mark the session ended."""
        logger.info("shutdown: beginning for session=%s", self.session_id)
        try:
            await self._persist_queue.put(None)
            if self._persist_task:
                await asyncio.wait_for(self._persist_task, timeout=5)
        except Exception:
            logger.exception("Persistence shutdown failed")
        try:
            await self._persist_ended()
        except Exception:
            logger.exception("Failed to persist ended state")
        logger.info("shutdown: complete for session=%s", self.session_id)

    # ------------------------------------------------------------------ #
    # Frame handling
    # ------------------------------------------------------------------ #

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        # Barge-in: stop TTS the moment the caller starts speaking.
        if isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking = True
            self._pending_user_text = ""
            try:
                await self.push_frame(InterruptionFrame(), direction)
            except Exception:
                logger.debug("Interruption frame push failed", exc_info=True)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False
            pending = self._pending_user_text
            self._pending_user_text = ""
            await self.push_frame(frame, direction)
            if pending:
                await self._run_turn(pending, self._last_confidence, direction)
            return

        if isinstance(frame, TextFrame):
            user_text = (frame.text or "").strip()
            if not user_text:
                return
            self._last_confidence = self._confidence_from_frame(frame)
            if self._user_speaking:
                # Merge fragments of one utterance instead of answering each one.
                self._pending_user_text = " ".join(
                    part for part in (self._pending_user_text, user_text) if part
                ).strip()
                logger.info("Buffering user speech: %s", user_text)
                return
            await self._run_turn(user_text, self._last_confidence, direction)
            return

        await self.push_frame(frame, direction)

    @staticmethod
    def _confidence_from_frame(frame):
        result = getattr(frame, "result", None)
        if isinstance(result, dict):
            avg = result.get("avg_logprob")
            if isinstance(avg, (int, float)):
                return float(avg)
        return None

    async def _run_turn(self, user_text: str, confidence, direction):
        """Run turn(s) sequentially, deferring any that arrive mid-turn."""
        while user_text:
            if self._turn_in_progress:
                self._deferred_user_text = " ".join(
                    part for part in (self._deferred_user_text, user_text) if part
                ).strip()
                return
            self._turn_in_progress = True
            try:
                result = await self.engine.process_turn(user_text, confidence)
                if not result.skipped:
                    await self._push_response(user_text, result, direction)
            except Exception:
                logger.exception("Turn handling failed")
                try:
                    fallback = await self._fallback_result(user_text, confidence)
                    await self._push_response(user_text, fallback, direction)
                except Exception:
                    logger.exception("Fallback push also failed")
            finally:
                self._turn_in_progress = False
            user_text = self._deferred_user_text
            self._deferred_user_text = ""
            confidence = None

    async def _fallback_result(self, user_text: str, confidence):
        """Build a TurnResult from FALLBACK_SPEECH after an unexpected failure."""
        from app.agents.turn_engine import TurnResult

        self.engine.state.turn_count += 1
        return TurnResult(
            user_text=user_text,
            text=FALLBACK_SPEECH,
            action="fallback",
            source="fallback",
            turn_no=self.engine.state.turn_count,
            latency_ms=0,
        )

    # ------------------------------------------------------------------ #
    # Response output (one frame per turn, guaranteed)
    # ------------------------------------------------------------------ #

    async def _push_response(self, user_text: str, result, direction):
        """Push the single winning response as a frame + persist the turn."""
        final = result.text or FALLBACK_SPEECH
        action = result.action

        pushed = False
        if self.arbiter.push_once():
            try:
                # TTSSpeakFrame (not TextFrame) drives the TTS directly and
                # bypasses the sentence aggregator. TextFrame responses were
                # being concatenated with the previous interrupted turn's
                # unspoken tail and played twice.
                await self.push_frame(TTSSpeakFrame(final), direction)
                pushed = True
            except Exception:
                logger.exception("push_frame failed")

        self._enqueue_persist(user_text, final, action)

        # Push transcript to WebSocket clients for real-time chat display.
        if user_text:
            task = asyncio.create_task(ws_manager.send_transcript(self.session_id, "user", user_text))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        if final and action != "repair":
            task = asyncio.create_task(ws_manager.send_transcript(self.session_id, "agent", final))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        self._log_turn(result, final, pushed)

    def _log_turn(self, result, final: str, pushed: bool):
        payload = {
            "turn": result.turn_no,
            "session": self.session_id[:8],
            "action": result.action,
            "confidence": self.state.last_confidence,
            "stage": self.state.stage,
            "slots": self.state.slot_summary(),
            "latency_ms": result.latency_ms,
            "pushed": pushed,
            "user": result.user_text,
            "response": final,
        }
        logger.info("TURN %s", json.dumps(payload, default=str))

    # ------------------------------------------------------------------ #
    # Persistence (background consumer)
    # ------------------------------------------------------------------ #

    def _enqueue_persist(self, user_text: str, agent_text: str, action: str):
        try:
            self._persist_queue.put_nowait({
                "user": user_text or "",
                "agent": agent_text or "",
                "action": action,
                "stage": self.state.stage,
                "intent_context": self.state.to_intent_context(),
                "booking_outcome": self.state.booking_outcome,
                "booking": self.engine.booking_snapshot(),
            })
        except Exception:
            logger.exception("Failed to enqueue persistence")

    async def _persist_loop(self):
        while True:
            item = await self._persist_queue.get()
            if item is None:
                return
            try:
                await self._persist_once(item)
            except Exception:
                logger.exception("Failed to persist state")

    async def _persist_once(self, item: dict):
        from app.db.session import async_session_factory
        from app.models.call_session import CallSession

        async with async_session_factory() as db:
            session = await db.get(CallSession, uuid.UUID(self.session_id))
            if session is None:
                logger.warning("Session %s not found for persistence", self.session_id)
                return

            transcript = list(session.transcript or [])
            if item.get("user"):
                transcript.append({
                    "type": "user_speech", "speaker": "user",
                    "text": item["user"], "timestamp": _now_iso(),
                })
            if item.get("agent"):
                transcript.append({
                    "type": "agent_speech", "speaker": "agent",
                    "text": item["agent"], "timestamp": _now_iso(),
                })
            session.transcript = transcript

            if item.get("stage"):
                session.fsm_state = item["stage"]
            if item.get("intent_context") is not None:
                session.intent_context = item["intent_context"]
            if item.get("booking_outcome"):
                # booking_outcome is a VARCHAR column; store the dict as JSON text.
                session.booking_outcome = json.dumps(item["booking_outcome"], default=str)
                session.status = "ended"
                session.ended_reason = "booked"

            await self._persist_booking(db, item.get("booking"))

            if session.status == "ringing":
                session.status = "in_call"
            session.updated_at = datetime.now(timezone.utc)
            await db.commit()

    async def _persist_booking(self, db, booking: dict | None):
        """Insert a row into the bookings table when a session is confirmed."""
        from app.models.booking import Booking

        if not booking or not booking.get("booking_confirmed"):
            return
        try:
            existing = await db.execute(
                Booking.__table__.select().where(
                    Booking.session_id == uuid.UUID(self.session_id)
                )
            )
            if existing.first() is not None:
                return
            db.add(Booking(
                session_id=uuid.UUID(self.session_id),
                customer_name=booking.get("customer_name"),
                customer_phone=booking.get("customer_phone"),
                slot=booking.get("slot"),
                booking_date=booking.get("booking_date"),
                booking_confirmed=booking.get("booking_confirmed", True),
            ))
        except Exception:
            logger.exception("Failed to persist booking row")

    async def _persist_ended(self):
        from app.db.session import async_session_factory
        from app.models.call_session import CallSession

        async with async_session_factory() as db:
            session = await db.get(CallSession, uuid.UUID(self.session_id))
            if session is None:
                logger.warning("persist_ended: session %s not found", self.session_id)
                return
            logger.info(
                "persist_ended: loaded session=%s status=%r fsm=%r",
                session.livekit_room, session.status, session.fsm_state,
            )
            if session.status != "ended":
                session.status = "ended"
                session.ended_reason = session.ended_reason or "caller_left"
            if session.started_at:
                session.duration_seconds = int(
                    (datetime.now(timezone.utc) - session.started_at).total_seconds()
                )
            session.updated_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("persist_ended: committed")
