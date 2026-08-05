"""Conversation manager for the dental clinic booking agent.

Responsible for one thing: turning one user utterance into exactly ONE spoken
response.

Flow per turn:
  1. Repair gate (only on genuinely low ASR confidence).
  2. Deterministic slot extraction into the persistent ConversationState.
  3. Deterministic dialogue policy picks the next action.
  4. The action is executed (question template, tool call, or LLM generation).
  5. The response arbiter picks a single winner and pushes one frame.
  6. A background consumer persists the turn to the database (never a
     fire-and-forget task ΓÇö pipecat cancels those at the end of frame
     processing, which is why transcripts used to stay empty).
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone, timedelta

from pipecat.frames.frames import (
    InterruptionFrame,
    TextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor

from app.agents import dialogue_manager as dm
from app.agents.constants import FALLBACK_SPEECH, REPAIR_SPEECH
from app.agents.conversation_state import (
    CHECKING,
    CONFIRMING,
    DONE,
    GATHERING,
    GREETING,
    OFFERING,
    ConversationState,
    ClinicDateTimeResolver,
    extract_slots,
)
from app.agents.dialogue_manager import DialogueManager
from app.agents.llm_service import LLMService
from app.agents.response_arbiter import ResponseArbiter, sanitize_for_speech
from app.agents.tools import book_appointment, check_availability, hold_slot
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

        self.state = ConversationState()
        self.dialogue = DialogueManager()
        self.llm = LLMService()
        self.arbiter = ResponseArbiter()
        self.resolver = ClinicDateTimeResolver()

        # Outbound (SIP) call: the agent already knows who it is calling and why,
        # so the fixed-order FSM skips name/phone/service collection.
        self.lead_context = lead_context or {}
        if self.lead_context.get("customer_name"):
            self.state.customer_name = self.lead_context["customer_name"]
        if self.lead_context.get("customer_phone") or self.lead_context.get("phone"):
            self.state.customer_phone = self.lead_context.get("customer_phone") or self.lead_context.get("phone")
        if self.lead_context.get("service"):
            self.state.service = self.lead_context["service"]

        self._user_speaking = False
        self._pending_user_text = ""
        self._deferred_user_text = ""
        self._turn_in_progress = False
        self._greeting_pushed = False
        self._user_joined = False
        self._llm_timed_out = False
        self._last_confidence = None

        # Repeat-question guard
        self._last_asked_action = None
        self._last_asked_at = 0.0

        # Persistence
        self._persist_queue: asyncio.Queue = asyncio.Queue()
        self._persist_task = None
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

        if self.lead_context and self.lead_context.get("customer_name"):
            name = self.lead_context["customer_name"]
            purpose = self.lead_context.get("purpose")
            if purpose:
                greeting = (
                    f"Hi {name}! This is Clara from the dental clinic. "
                    f"I believe you filled in a form about {purpose}. "
                    f"What day would work best for you?"
                )
            else:
                greeting = (
                    f"Hi {name}! This is Clara from the dental clinic. "
                    f"What day would work best for you?"
                )
            self.state.stage = GATHERING
        elif self.lead_context and self.lead_context.get("purpose"):
            # No name given in the form, but we still know why we're calling.
            purpose = self.lead_context["purpose"]
            greeting = (
                "Hi there! This is Clara from the dental clinic. "
                f"I believe you filled in a form about {purpose}. "
                "May I ask your name, please?"
            )
            self.state.stage = GREETING
        else:
            greeting = (
                "Hi there, welcome to our dental clinic! I'm Clara, your booking "
                "assistant. May I ask your name, please?"
            )
            self.state.stage = GREETING
        self.state.add_assistant_line(greeting)
        self.state.pending_question = greeting
        logger.info("Pushing greeting")
        try:
            await self.push_frame(TTSSpeakFrame(greeting))
        except Exception:
            logger.exception("Greeting push failed")

        # Send greeting to WebSocket for chat display
        asyncio.create_task(ws_manager.send_transcript(self.session_id, "agent", greeting))

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
                await self._handle_turn(user_text, confidence, direction)
            except Exception:
                logger.exception("Turn handling failed")
                try:
                    await self._push_response(
                        user_text, FALLBACK_SPEECH, direction,
                        action="fallback", turn_no=self.state.turn_count + 1,
                        started=time.perf_counter(), confidence=confidence, extracted={},
                    )
                except Exception:
                    logger.exception("Fallback push also failed")
            finally:
                self._turn_in_progress = False
            user_text = self._deferred_user_text
            self._deferred_user_text = ""
            confidence = None

    # ------------------------------------------------------------------ #
    # One-turn handling
    # ------------------------------------------------------------------ #

    async def _handle_turn(self, user_text: str, confidence, direction):
        turn_no = self.state.turn_count + 1
        started = time.perf_counter()
        self.state.turn_count = turn_no
        self.state.last_asr_text = user_text
        self.state.last_confidence = confidence

        # After booking, let the normal dialogue flow handle the turn.
        # The user can book another service, ask questions, or say goodbye.
        # The call stays open until the user explicitly closes or the
        # dialogue policy reaches a natural end.

        if self._should_repair(user_text, confidence):
            await self._push_response(
                user_text, REPAIR_SPEECH, direction,
                action="repair", turn_no=turn_no, started=started,
                confidence=confidence, extracted={},
            )
            return

        extracted = extract_slots(
            user_text,
            # Bare-word name capture ("John") is only safe when we are actively
            # collecting the name. Once we have one, "root cala" / "to help"
            # (answers to the service question) must NOT overwrite the name.
            allow_bare_name=not bool(self.state.customer_name),
        )
        self.state.apply_extracted(extracted)

        # Resolve appointment datetime when both date and time are available
        if self.state.date_pref and self.state.time_pref:
            self.state.resolve_appointment_dt(self.resolver)

        action, payload = self.dialogue.decide(self.state, user_text)

        if self._is_debounced_repeat(action, extracted):
            logger.info(
                "TURN %s: debounced repeat of action=%s (likely a mid-answer "
                "pause split by VAD) ΓÇö staying quiet instead of re-asking",
                turn_no, action,
            )
            return

        text = await self._execute_action(action, payload, user_text)

        await self._push_response(
            user_text, text, direction,
            action=action, turn_no=turn_no, started=started,
            confidence=confidence, extracted=extracted,
        )

    # Ask actions eligible for the repeat-question debounce. Only plain
    # slot-questions ΓÇö never CLOSE/CONFIRM/etc ΓÇö so we never suppress a
    # meaningfully different response.
    _DEBOUNCE_ACTIONS = {
        dm.ASK_NAME, dm.ASK_PHONE, dm.ASK_SERVICE,
        dm.ASK_DATE,
    }
    _DEBOUNCE_SECONDS = 3.5

    def _is_debounced_repeat(self, action: str, extracted: dict) -> bool:
        """True if this is the same ask we *just* spoke, the caller's last
        fragment gave us nothing new, and not enough time has passed for
        them to plausibly have heard the question and replied to it.
        """
        if action not in self._DEBOUNCE_ACTIONS:
            return False
        if action != self._last_asked_action:
            return False
        if extracted:
            return False
        return (time.perf_counter() - self._last_asked_at) < self._DEBOUNCE_SECONDS

    def _note_asked(self, action: str):
        if action in self._DEBOUNCE_ACTIONS:
            self._last_asked_action = action
            self._last_asked_at = time.perf_counter()
        else:
            # Any non-ask action (answered question, offered slots, closed,
            # etc.) means the conversation moved on ΓÇö clear the guard so a
            # later, unrelated repeat of the same ask isn't debounced.
            self._last_asked_action = None

    async def _execute_action(self, action: str, payload: dict, user_text: str):
        if action in (
            dm.ASK_NAME, dm.ASK_PHONE, dm.ASK_SERVICE,
            dm.ASK_DATE,
        ):
            if self.state.stage == GREETING:
                self.state.stage = GATHERING
            return self.dialogue.question_for(action, self.state, user_text)

        if action == dm.CHECK_AVAILABILITY:
            self.state.stage = CHECKING
            slots = await check_availability(
                date=self.state.date_pref or "today",
                service_name=self.state.service,
                resolver=self.resolver,
            )
            self.state.offered_slots = slots
            self.state.stage = OFFERING
            logger.info("check_availability(%s) -> %d slots", self.state.date_pref, len(slots))
            return self.dialogue.offer_text(self.state)

        if action == dm.OFFER_SLOTS:
            self.state.stage = OFFERING
            return self.dialogue.offer_text(self.state)

        if action == dm.CHOOSE_SLOT:
            self.state.chosen_slot = payload["slot"]
            # Update time_pref and resolved_appointment_dt with the chosen slot
            slot_time = payload["slot"].get("time")
            if slot_time:
                self.state.time_pref = slot_time
                self.state.resolve_appointment_dt(self.resolver)
            self.state.pending_confirm = "booking"
            self.state.stage = CONFIRMING
            return self.dialogue.confirm_text(self.state)

        if action == dm.CONFIRM_BOOKING:
            self.state.pending_confirm = "booking"
            self.state.stage = CONFIRMING
            return self.dialogue.confirm_text(self.state)

        if action == dm.CONFIRM:
            return await self._complete_booking()

        if action == dm.CANCEL_CONFIRM:
            self.state.pending_confirm = None
            self.state.chosen_slot = None
            self.state.stage = OFFERING
            return self.dialogue.reoffer_text(self.state)

        if action == dm.ANSWER_QUESTION:
            return self.dialogue.answer_question(payload.get("topic")) or FALLBACK_SPEECH

        if action == dm.CLOSE:
            self.state.pending_confirm = None
            self.state.stage = DONE
            self.state.call_closed = True
            return self.dialogue.close_text(self.state)

        if action == dm.CLARIFY:
            return self.dialogue.clarify_text(self.state)

        if action == dm.RESPOND:
            return await self._llm_reply(user_text)

        return None

    async def _complete_booking(self):
        slot = self.state.chosen_slot or {}
        slot_time = slot.get("time")
        try:
            start_dt = self.state.resolved_appointment_dt
            if slot_time and start_dt is None:
                hold = await hold_slot(
                    slot_time,
                    self.state.service,
                    resolver=self.resolver,
                )
                logger.info("hold_slot(%s) -> %s", slot_time, hold.get("message"))
                start_dt = hold.get("start_dt")
            
            # Calculate end_dt from start_dt using slot duration
            if start_dt is None:
                raise ValueError("Could not determine appointment start time")
            from app.config import settings
            end_dt = start_dt + timedelta(minutes=settings.slot_duration_minutes)

            booking = await book_appointment(
                customer_name=self.state.customer_name,
                customer_phone=self.state.customer_phone,
                notes="; ".join(self.state.notes) or None,
                start_dt=start_dt,
                end_dt=end_dt,
            )
            self.state.booking_id = booking.get("booking_id")
            self.state.booking_outcome = {
                "status": "confirmed",
                "booking_id": booking.get("booking_id"),
            }
            self.state.stage = DONE
            self.state.pending_confirm = None
            # Keep conversation open for follow-up questions or changes
            self.state.stage = OFFERING
            logger.info("Booking confirmed: %s", booking)
            return self.dialogue.booked_text(self.state) + " Is there anything else you would like to change or ask?"
        except Exception as e:
            logger.exception("Booking failed with error: %s", str(e))
            self.state.stage = CONFIRMING
            return f"I'm sorry, I couldn't complete the booking due to an error: {str(e)}. Shall I try again?"

    async def _llm_reply(self, user_text: str):
        result = await self.llm.generate(self.state, user_text)
        self._llm_timed_out = result.timed_out
        return result.text or None

    # ------------------------------------------------------------------ #
    # Response output (one frame per turn, guaranteed)
    # ------------------------------------------------------------------ #

    def _action_source(self, action: str) -> str:
        if action == "repair":
            return "repair"
        if action == dm.ANSWER_QUESTION:
            return "knowledge"
        if action == dm.RESPOND:
            return "llm"
        return "action"

    async def _push_response(
        self, user_text, text, direction, *, action, turn_no, started, confidence, extracted
    ):
        source = self._action_source(action)
        if not text:
            source = "fallback"
            text = FALLBACK_SPEECH

        self.arbiter.reset_turn()
        self.arbiter.propose(source, text)
        final = self.arbiter.resolve()

        record_assistant = not (action == dm.RESPOND and self._llm_timed_out)
        self._llm_timed_out = False

        if action == "repair":
            pass  # never pollute clean history with ASR noise
        elif record_assistant:
            self.state.add_turn(user_text, final)
        else:
            self.state.add_turn(user_text, None)

        self.state.pending_question = final

        pushed = False
        if self.arbiter.push_once():
            try:
                # TTSSpeakFrame (not TextFrame) drives the TTS directly and
                # bypasses the sentence aggregator. TextFrame responses were
                # being concatenated with the previous interrupted turn's
                # unspoken tail (e.g. "[Could you tell me your name,
                # please?Thanks, Akash.]") and played twice, because the
                # aggregator buffer was never cleared between turns.
                await self.push_frame(TTSSpeakFrame(final), direction)
                pushed = True
                self._note_asked(action)
            except Exception:
                logger.exception("push_frame failed")

        self._enqueue_persist(user_text, final, action)

        # Push transcript to WebSocket clients for real-time chat display.
        if user_text:
            asyncio.create_task(ws_manager.send_transcript(self.session_id, "user", user_text))
        if final and action != "repair":
            asyncio.create_task(ws_manager.send_transcript(self.session_id, "agent", final))

        latency_ms = int((time.perf_counter() - started) * 1000)
        self._log_turn(turn_no, user_text, confidence, extracted, action, final, pushed, latency_ms)

    def _should_repair(self, user_text: str, confidence) -> bool:
        """Repair ONLY on genuinely low confidence AND noise-like utterance."""
        if not user_text:
            return False
        if confidence is None:
            return False
        if confidence >= settings.asr_confidence_repair_threshold:
            return False
        words = user_text.strip().split()
        if len(words) >= 5:
            return False
        if re.search(r"[a-z]{2,}", user_text.lower()) and len(words) >= 2:
            return False
        return True

    def _log_turn(self, turn_no, user_text, confidence, extracted, action, final, pushed, latency_ms):
        payload = {
            "turn": turn_no,
            "session": self.session_id[:8],
            "action": action,
            "confidence": confidence,
            "extracted": {k: v for k, v in (extracted or {}).items() if v},
            "stage": self.state.stage,
            "slots": self.state.slot_summary(),
            "latency_ms": latency_ms,
            "pushed": pushed,
            "user": user_text,
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
                "booking": self._booking_snapshot(),
            })
        except Exception:
            logger.exception("Failed to enqueue persistence")

    def _booking_snapshot(self) -> dict | None:
        """Capture booking details for the bookings table when confirmed."""
        outcome = self.state.booking_outcome
        if not outcome or outcome.get("status") != "confirmed":
            return None
        slot = self.state.chosen_slot or {}
        return {
            "session_id": str(self.session_id),
            "customer_name": self.state.customer_name,
            "customer_phone": self.state.customer_phone,
            "slot": slot.get("spoken_time") or slot.get("time"),
            "booking_date": self.state.resolved_appointment_dt,
            "booking_confirmed": True,
        }

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
                session.booking_outcome = json.dumps(item["booking_outcome"])
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
