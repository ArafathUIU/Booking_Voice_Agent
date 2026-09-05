"""Headless turn engine — the deterministic booking brain, transport-agnostic.

Both voice transports share this logic:
  * the pipecat pipeline (LiveKit / Twilio Media Streams) via ConversationManager,
  * the Twilio trial-native ``<Gather input="speech">`` webhook path.

It reuses the exact same slot extraction, dialogue policy, tools, and LLM
small-talk as the streaming path, but produces a plain spoken line instead of
pushing pipecat frames. This is what makes the booking flow work on Twilio trial
accounts (which strip the ``<Stream>`` verb).
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.agents import dialogue_manager as dm
from app.agents.constants import FALLBACK_SPEECH, REPAIR_SPEECH
from app.agents.conversation_state import (
    CHECKING,
    CONFIRMING,
    DONE,
    GATHERING,
    GREETING,
    OFFERING,
    ClinicDateTimeResolver,
    ConversationState,
    extract_slots,
)
from app.agents.dialogue_manager import DialogueManager
from app.agents.llm_service import LLMService
from app.agents.response_arbiter import ResponseArbiter
from app.agents.tools import book_appointment, check_availability, hold_slot
from app.config import settings
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """The outcome of processing one user utterance."""

    user_text: str = ""
    text: str = ""  # the single spoken line ("" when the turn was skipped)
    action: str = ""
    source: str = "action"
    turn_no: int = 0
    latency_ms: int = 0
    skipped: bool = False  # True when the turn was debounced (say nothing)
    llm_timed_out: bool = False


class TurnEngine:
    """Owns ConversationState + the deterministic dialogue for one session.

    ``process_turn`` mirrors the old ``ConversationManager._handle_turn`` +
    ``_push_response`` arbitration, minus any pipecat/frame concern.
    """

    # Ask actions eligible for the repeat-question debounce. Only plain
    # slot-questions — never CLOSE/CONFIRM/etc — so we never suppress a
    # meaningfully different response.
    _DEBOUNCE_ACTIONS = {
        dm.ASK_NAME, dm.ASK_PHONE, dm.ASK_SERVICE,
        dm.ASK_DATE,
    }
    _DEBOUNCE_SECONDS = 3.5

    def __init__(
        self,
        state: ConversationState | None = None,
        session_id: str | None = None,
        tenant_id: str | None = None,
        lead_context: dict | None = None,
    ):
        self.state = state or ConversationState()
        self.session_id = session_id
        self.tenant_id = tenant_id
        self.dialogue = DialogueManager()
        self.llm = LLMService()
        self.arbiter = ResponseArbiter()
        self.resolver = ClinicDateTimeResolver()

        self._last_asked_action = None
        self._last_asked_at = 0.0
        self._llm_timed_out = False

        # Seed known caller details for outbound calls.
        lead_context = lead_context or {}
        if lead_context.get("customer_name"):
            self.state.customer_name = lead_context["customer_name"]
        if lead_context.get("customer_phone") or lead_context.get("phone"):
            self.state.customer_phone = (
                lead_context.get("customer_phone") or lead_context.get("phone")
            )
        if lead_context.get("service"):
            self.state.service = lead_context["service"]

    # ------------------------------------------------------------------ #
    # One-turn processing
    # ------------------------------------------------------------------ #

    async def process_turn(
        self, user_text: str, confidence: float | None = None
    ) -> TurnResult:
        """Turn one utterance into exactly ONE spoken response."""
        turn_no = self.state.turn_count + 1
        started = time.perf_counter()
        self.state.turn_count = turn_no
        self.state.last_asr_text = user_text
        self.state.last_confidence = confidence

        if self._should_repair(user_text, confidence):
            self.state.pending_question = REPAIR_SPEECH
            return TurnResult(
                user_text=user_text,
                text=REPAIR_SPEECH,
                action="repair",
                source="repair",
                turn_no=turn_no,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        extracted = extract_slots(
            user_text,
            # Bare-word name capture ("John") is only safe when we are actively
            # collecting the name. Once we have one, "root cala" / "to help"
            # (answers to the service question) must NOT overwrite the name.
            allow_bare_name=not bool(self.state.customer_name),
        )
        self.state.apply_extracted(extracted, user_text=user_text)

        # Resolve appointment datetime when both date and time are available.
        if self.state.date_pref and self.state.time_pref:
            self.state.resolve_appointment_dt(self.resolver)

        action, payload = self.dialogue.decide(self.state, user_text)

        if self._is_debounced_repeat(action, extracted):
            logger.info(
                "TURN %s: debounced repeat of action=%s (likely a mid-answer "
                "pause split by VAD) — staying quiet instead of re-asking",
                turn_no, action,
            )
            return TurnResult(
                user_text=user_text,
                action=action,
                turn_no=turn_no,
                latency_ms=int((time.perf_counter() - started) * 1000),
                skipped=True,
            )

        text = await self._execute_action(action, payload, user_text)

        source = self._action_source(action)
        if not text:
            if self.state.pending_confirm and self.state.chosen_slot:
                date_str = dm._resolve_date_spoken(self.state.date_pref)
                time_str = self.state.chosen_slot.get("spoken_time") or self.state.time_pref
                source = "action"
                text = (
                    f"Just to confirm, we have a {self.state.service} on {date_str} at {time_str}. "
                    f"Shall I go ahead and book it, or would you prefer a different time?"
                )
            else:
                source = "fallback"
                text = FALLBACK_SPEECH

        # Arbitrate: exactly one clean spoken line.
        self.arbiter.reset_turn()
        self.arbiter.propose(source, text)
        final = self.arbiter.resolve()

        llm_timed_out = self._llm_timed_out
        self._llm_timed_out = False

        # History: never pollute clean history with ASR noise or an LLM timeout.
        if action != "repair":
            if not (action == dm.RESPOND and llm_timed_out):
                self.state.add_turn(user_text, final)
            else:
                self.state.add_turn(user_text, None)

        self.state.pending_question = final
        self._note_asked(action)

        return TurnResult(
            user_text=user_text,
            text=final,
            action=action,
            source=source,
            turn_no=turn_no,
            latency_ms=int((time.perf_counter() - started) * 1000),
            llm_timed_out=llm_timed_out,
        )

    # ------------------------------------------------------------------ #
    # Repair gate + debounce
    # ------------------------------------------------------------------ #

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

    def _is_debounced_repeat(self, action: str, extracted: dict) -> bool:
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
            self._last_asked_action = None

    # ------------------------------------------------------------------ #
    # Action execution
    # ------------------------------------------------------------------ #

    def _action_source(self, action: str) -> str:
        if action == "repair":
            return "repair"
        if action == dm.ANSWER_QUESTION:
            return "knowledge"
        if action == dm.RESPOND:
            return "llm"
        return "action"

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

        if action == dm.OUT_OF_HOURS:
            requested = payload.get("requested_time", "that time")
            date_str = dm._resolve_date_spoken(self.state.date_pref)
            spoken = [s["spoken_time"] for s in self.state.offered_slots][:3]
            list_str = ", ".join(spoken) if spoken else "our morning or afternoon slots"
            return (
                f"I'm sorry, our clinic is open from 9 AM to 6 PM, so we don't have appointments at {requested}. "
                f"For {date_str}, we have {list_str} available. Which one suits you?"
            )

        if action == dm.UNAVAILABLE_TIME:
            requested = payload.get("requested_time", "that time")
            date_str = dm._resolve_date_spoken(self.state.date_pref)
            spoken = [s["spoken_time"] for s in self.state.offered_slots][:3]
            list_str = ", ".join(spoken) if spoken else "our other open slots"
            return (
                f"I'm sorry, {requested} isn't available on {date_str}. "
                f"The slots we have open are {list_str}. Which one would you prefer?"
            )

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

            if start_dt is None:
                raise ValueError("Could not determine appointment start time")
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
            # Keep conversation open for follow-up questions.
            self.state.stage = OFFERING
            logger.info("Booking confirmed: %s", booking)
            return self.dialogue.booked_text(self.state)
        except Exception as e:
            logger.exception("Booking failed with error: %s", str(e))
            self.state.stage = CONFIRMING
            return f"I'm sorry, I couldn't complete the booking due to an error: {str(e)}. Shall I try again?"

    async def _llm_reply(self, user_text: str):
        result = await self.llm.generate(self.state, user_text)
        self._llm_timed_out = result.timed_out
        return result.text or None

    # ------------------------------------------------------------------ #
    # Booking snapshot (for the bookings table)
    # ------------------------------------------------------------------ #

    def booking_snapshot(self) -> dict | None:
        outcome = self.state.booking_outcome
        if not outcome or outcome.get("status") != "confirmed":
            return None
        slot = self.state.chosen_slot or {}
        return {
            "session_id": str(self.session_id) if self.session_id else None,
            "customer_name": self.state.customer_name,
            "customer_phone": self.state.customer_phone,
            "slot": slot.get("spoken_time") or slot.get("time"),
            "booking_date": self.state.resolved_appointment_dt,
            "booking_confirmed": True,
        }


# ---------------------------------------------------------------------- #
# Outbound greeting
# ---------------------------------------------------------------------- #

def build_outbound_greeting(state: ConversationState, lead_context: dict | None) -> str:
    """Build the first thing Clara says on an outbound call, seeding stage."""
    lead_context = lead_context or {}
    if lead_context.get("customer_name"):
        name = lead_context["customer_name"]
        purpose = lead_context.get("purpose")
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
        state.stage = GATHERING
    elif lead_context.get("purpose"):
        purpose = lead_context["purpose"]
        greeting = (
            "Hi there! This is Clara from the dental clinic. "
            f"I believe you filled in a form about {purpose}. "
            "May I ask your name, please?"
        )
        state.stage = GREETING
    else:
        greeting = (
            "Hi there, welcome to our dental clinic! I'm Clara, your booking "
            "assistant. May I ask your name, please?"
        )
        state.stage = GREETING
    state.add_assistant_line(greeting)
    state.pending_question = greeting
    return greeting


# ---------------------------------------------------------------------- #
# Session persistence (headless path — mirrors ConversationManager)
# ---------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def persist_turn(
    session_id: str,
    *,
    user_text: str,
    agent_text: str,
    engine: TurnEngine,
):
    """Persist one turn to call_sessions (transcript, stage, intent context,
    booking outcome + bookings row) without any pipecat involvement."""
    from app.models.call_session import CallSession

    state = engine.state
    snapshot = engine.booking_snapshot()

    async with async_session_factory() as db:
        session = await db.get(CallSession, uuid.UUID(session_id))
        if session is None:
            logger.warning("Session %s not found for persistence", session_id)
            return

        transcript = list(session.transcript or [])
        if user_text:
            transcript.append({
                "type": "user_speech", "speaker": "user",
                "text": user_text, "timestamp": _now_iso(),
            })
        if agent_text:
            transcript.append({
                "type": "agent_speech", "speaker": "agent",
                "text": agent_text, "timestamp": _now_iso(),
            })
        session.transcript = transcript

        session.fsm_state = state.stage
        session.intent_context = state.to_intent_context()
        if state.booking_outcome:
            session.booking_outcome = json.dumps(state.booking_outcome, default=str)
            session.status = "ended"
            session.ended_reason = "booked"

        if snapshot and snapshot.get("booking_confirmed"):
            await _insert_booking(db, session_id, snapshot)

        if session.status == "ringing":
            session.status = "in_call"
        session.updated_at = datetime.now(timezone.utc)
        await db.commit()


async def _insert_booking(db, session_id: str, snapshot: dict):
    from app.models.booking import Booking

    try:
        existing = await db.execute(
            Booking.__table__.select().where(
                Booking.session_id == uuid.UUID(session_id)
            )
        )
        if existing.first() is not None:
            return
        db.add(Booking(
            session_id=uuid.UUID(session_id),
            customer_name=snapshot.get("customer_name"),
            customer_phone=snapshot.get("customer_phone"),
            slot=snapshot.get("slot"),
            booking_date=snapshot.get("booking_date"),
            booking_confirmed=snapshot.get("booking_confirmed", True),
        ))
    except Exception:
        logger.exception("Failed to persist booking row")


async def finalize_session(session_id: str, lead_id: str | None, outcome: str = "completed"):
    """Mark the call session + lead as ended (headless path)."""
    from app.services.call_service import CallService
    from app.services.lead_service import LeadService

    async with async_session_factory() as db:
        cs = CallService(db)
        svc = LeadService(db)
        await cs.end_session(session_id, outcome)
        if lead_id:
            await svc.mark_completed(lead_id)
        await db.commit()
    logger.info("Finalized headless session=%s lead=%s outcome=%s", session_id, lead_id, outcome)
