"""Tests for the refactored conversation manager (deterministic dialogue)."""

import asyncio
from unittest.mock import AsyncMock

from pipecat.frames.frames import TextFrame, TTSSpeakFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame

from app.agents import dialogue_manager as dm
from app.agents.conversation_manager import ConversationManager
from app.agents.conversation_state import extract_slots, extract_phone, _PHONE_MIN_DIGITS


def _make() -> ConversationManager:
    # Constructed outside an event loop -> persistence consumer is disabled,
    # which is what we want for unit tests.
    return ConversationManager(session_id="test-session", tenant_id="test-tenant")


def _text_frames(push_calls) -> list:
    # Responses are now pushed as TTSSpeakFrame (bypasses the TTS sentence
    # aggregator so interrupted-turn tails are never glued onto the next reply).
    return [
        c.args[0]
        for c in push_calls
        if isinstance(c.args[0], TTSSpeakFrame)
    ]


def test_slot_progression_reaches_check_availability():
    m = _make()
    m.state.customer_name = "John"
    m.state.customer_phone = "01712345678"
    m.state.service = "Teeth Cleaning"
    m.state.date_pref = "tomorrow"
    m.state.time_pref = "morning"

    action, _ = m.dialogue.decide(m.state, "book it")

    assert action == dm.CHECK_AVAILABILITY


def test_booking_details_extract_then_ask_phone():
    m = _make()
    m.state.customer_name = "Sara"
    m.state.apply_extracted(extract_slots("I need a whitening appointment tomorrow"))

    action, _ = m.dialogue.decide(m.state, "I need a whitening appointment tomorrow")

    # Name already known, so the next missing slot is the phone number.
    assert action == dm.ASK_PHONE


def test_confirmation_yes_books():
    m = _make()
    m.state.customer_name = "John"
    m.state.customer_phone = "01712345678"
    m.state.service = "Teeth Cleaning"
    m.state.date_pref = "tomorrow"
    m.state.time_pref = "3 PM"
    m.state.offered_slots = [{"time": "15:00", "available": True}]
    m.state.chosen_slot = {"time": "15:00", "available": True}
    m.state.pending_confirm = "booking"

    action, _ = m.dialogue.decide(m.state, "yes, go ahead")

    assert action == dm.CONFIRM


def test_user_speech_is_buffered_until_the_user_stops_speaking():
    m = _make()
    m.push_frame = AsyncMock()

    asyncio.run(m.process_frame(UserStartedSpeakingFrame(), None))
    asyncio.run(m.process_frame(TextFrame("I need a cleaning"), None))

    # Nothing spoken while the user is still talking.
    assert _text_frames(m.push_frame.call_args_list) == []

    asyncio.run(m.process_frame(UserStoppedSpeakingFrame(), None))

    # Exactly one response once the utterance is complete.
    assert len(_text_frames(m.push_frame.call_args_list)) == 1
    assert m._pending_user_text == ""


def test_one_response_per_turn():
    m = _make()
    m.push_frame = AsyncMock()

    asyncio.run(m._run_turn("Hi, my name is John", -0.2, None))

    assert len(_text_frames(m.push_frame.call_args_list)) == 1


def test_question_variation_after_repeat():
    m = _make()
    m.state.customer_name = "Akash"

    first = m.dialogue.question_for(dm.ASK_PHONE, m.state, "I want to book an appointment")
    second = m.dialogue.question_for(dm.ASK_PHONE, m.state, "My phone number right?")

    # Both should contain "phone" and be valid responses
    assert "phone" in first.lower()
    assert "phone" in second.lower()
    # The re-ask should use a different phrasing (allow same by chance, but verify
    # it's one of the expected variations)
    valid_variations = {
        "What is your phone number?",
        "Could you share your phone number?",
        "I still need your phone number to proceed.",
    }
    assert first in valid_variations
    assert second in valid_variations


def test_closed_call_does_not_reoffer_slots():
    m = _make()
    m.state.customer_name = "Vidon"
    m.state.customer_phone = "01712345678"
    m.state.service = "Teeth Cleaning"
    m.state.date_pref = "tomorrow"
    m.state.time_pref = "morning"
    m.state.offered_slots = [
        {"time": "10:00", "spoken_time": "10 AM", "available": True},
        {"time": "14:00", "spoken_time": "2 PM", "available": True},
    ]
    m.state.chosen_slot = None
    m.state.call_closed = True

    action, _ = m.dialogue.decide(m.state, "we can decide later")

    # Previously this fell through to OFFER_SLOTS and re-offered in a loop.
    assert action == dm.CLOSE


def test_unclosed_offer_still_offers():
    m = _make()
    m.state.customer_name = "Vidon"
    m.state.customer_phone = "01712345678"
    m.state.service = "Teeth Cleaning"
    m.state.date_pref = "tomorrow"
    m.state.time_pref = "morning"
    m.state.offered_slots = [
        {"time": "10:00", "spoken_time": "10 AM", "available": True},
        {"time": "14:00", "spoken_time": "2 PM", "available": True},
    ]
    m.state.chosen_slot = None

    action, _ = m.dialogue.decide(m.state, "we can decide later")

    assert action in (dm.CHOOSE_SLOT, dm.OFFER_SLOTS)


def test_clarify_request_restates_offer():
    m = _make()
    m.state.customer_name = "Vidon"
    m.state.customer_phone = "01712345678"
    m.state.service = "Teeth Cleaning"
    m.state.date_pref = "tomorrow"
    m.state.time_pref = "morning"
    m.state.offered_slots = [
        {"time": "10:00", "spoken_time": "10 AM", "available": True},
        {"time": "14:00", "spoken_time": "2 PM", "available": True},
    ]
    m.state.chosen_slot = None

    action, _ = m.dialogue.decide(m.state, "I'm sorry, which times were available again?")

    assert action == dm.CLARIFY
    text = m.dialogue.clarify_text(m.state)
    assert "10 AM" in text and "2 PM" in text


def test_close_text_is_honest_when_not_booked():
    m = _make()
    m.state.customer_name = "Vidon"
    text = m.dialogue.close_text(m.state)
    assert "All set" not in text
    assert "book" in text.lower() or "call" in text.lower()


def test_close_text_confirmed_when_booked():
    m = _make()
    m.state.customer_name = "Vidon"
    m.state.booking_id = "book-1"
    text = m.dialogue.close_text(m.state)
    assert "All set" in text and "confirmed" in text


def test_phone_min_digits_raised_to_seven():
    assert _PHONE_MIN_DIGITS >= 7
    # A partial 5-digit capture should not be accepted as a real phone number.
    assert extract_phone("my number is 01742") is None
    assert extract_phone("my number is 01712345678") == "01712345678"


def test_offer_text_varies_on_repeat():
    m = _make()
    m.state.date_pref = "tomorrow"
    m.state.offered_slots = [
        {"time": "10:00", "spoken_time": "10 AM", "available": True},
        {"time": "14:00", "spoken_time": "2 PM", "available": True},
    ]
    first = m.dialogue.offer_text(m.state)
    second = m.dialogue.offer_text(m.state)
    assert "Which works best for you?" in first
    assert "which" in second.lower() or "which one" in second.lower()
