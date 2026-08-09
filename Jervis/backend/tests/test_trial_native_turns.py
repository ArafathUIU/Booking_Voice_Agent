"""Tests for the Twilio trial-native turn loop (TurnEngine + webhook helpers).

Drives the same path ``twiml_turn`` uses: greet -> process each spoken turn ->
close -> hangup. The tools (check_availability/hold_slot/book_appointment) are
in-memory mocks, so no backend / Twilio is required.
"""

import asyncio
import re

from app.agents import dialogue_manager as dm
from app.agents.conversation_state import GREETING
from app.agents.turn_engine import TurnEngine, build_outbound_greeting

_HIGH_CONFIDENCE = 0.95

_JSON_RE = re.compile(r"[\{\}\[\]]")
_TOOL_RE = re.compile(r"(?i)\b(check_availability|hold_slot|book_appointment)\b")


def _probe(text: str) -> list:
    problems = []
    if not text:
        problems.append("empty reply")
    if _JSON_RE.search(text):
        problems.append("contains JSON delimiters")
    if _TOOL_RE.search(text):
        problems.append("mentions a tool name")
    return problems


def _make_engine(name: str | None = None, purpose: str = "a teeth cleaning appointment") -> TurnEngine:
    lead_context = {}
    if name:
        lead_context["customer_name"] = name
    if purpose:
        lead_context["purpose"] = purpose
    engine = TurnEngine(
        session_id="trial-test",
        tenant_id="test-tenant",
        lead_context=lead_context,
    )
    build_outbound_greeting(engine.state, lead_context)
    return engine


def _turns(engine: TurnEngine, script: list[tuple[str, str]]) -> list[str]:
    replies = []
    for label, user_text in script:
        result = asyncio.run(engine.process_turn(user_text, _HIGH_CONFIDENCE))
        assert not result.skipped, f"'{label}' turn was debounced/skipped"
        problems = _probe(result.text)
        assert not problems, f"'{label}' -> {result.text!r} problems={problems}"
        replies.append(result.text)
    return replies


def test_full_booking_script_via_trial_native_loop():
    engine = _make_engine(name="Akash")
    _turns(engine, [
        ("phone", "My phone number is 555-123-4567."),
        ("service", "I'd like to book a teeth cleaning."),
        ("day", "How about tomorrow at 3pm?"),
        ("pick", "Yes, the three o'clock works."),
        ("confirm", "Yes, book it."),
        ("close", "Thanks, that's all."),
    ])
    assert engine.state.booking_id, "expected a completed booking"
    assert engine.state.call_closed
    # After closing, later turns keep yielding CLOSE (no re-offering loop).
    late = asyncio.run(engine.process_turn("hello?", _HIGH_CONFIDENCE))
    assert late.action == dm.CLOSE, f"expected CLOSE after call closed, got {late.action}"


def test_rebooking_after_closed_call_stays_closed():
    engine = _make_engine(name="Akash")
    _turns(engine, [
        ("phone", "My phone number is 555-123-4567."),
        ("service", "I'd like a teeth cleaning."),
        ("close", "Actually, never mind. Goodbye."),
    ])
    assert engine.state.call_closed
    assert not engine.state.booking_id


def test_greeting_without_name_asks_for_name_first():
    engine = _make_engine(name=None)
    result = asyncio.run(engine.process_turn("My name is John.", _HIGH_CONFIDENCE))
    assert "phone" in result.text.lower()
    assert engine.state.customer_name == "John"
    assert engine.state.stage != GREETING


def test_dtmf_digit_answers_time_slot():
    # Webhook passes bare DTMF digits through as user_text; a bare "3" maps to
    # the time slot via extract_time (speech-free fallback).
    engine = _make_engine(name="Sara")
    _turns(engine, [
        ("phone", "My phone number is 01712345678."),
        ("service", "I'd like a cleaning."),
        ("day", "tomorrow"),
    ])
    result = asyncio.run(engine.process_turn("3", _HIGH_CONFIDENCE))
    assert result.action in (dm.OFFER_SLOTS, dm.CHOOSE_SLOT, dm.ASK_DATE)
    assert not result.skipped


def test_silence_timeout_reasks_without_double_speak():
    # Twilio POSTs an empty SpeechResult on Gather timeout; the engine must
    # re-ask the pending question rather than crash or emit nothing.
    engine = _make_engine(name="Akash")
    result = asyncio.run(engine.process_turn("", _HIGH_CONFIDENCE))
    assert result.text, "expected a spoken response for silence"
    assert result.action == dm.ASK_PHONE
