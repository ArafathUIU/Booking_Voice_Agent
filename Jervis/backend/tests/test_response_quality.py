"""Tests for the response-quality fixes: glued sentences, history size,
and mid-sentence truncation.
"""

from app.agents.llm_service import LLMService, _trim_to_last_sentence
from app.agents.response_arbiter import sanitize_for_speech
from app.agents.conversation_state import ConversationState
from app.config import settings


def test_sanitize_fixes_glued_question_mark():
    raw = "Sure.You'd like a cleaning?Let me check the calendar."
    out = sanitize_for_speech(raw)
    assert "?L" not in out
    assert ".Y" not in out
    assert out == "Sure. You'd like a cleaning? Let me check the calendar."


def test_sanitize_fixes_glued_comma():
    raw = "Okay,let's find a time that works."
    out = sanitize_for_speech(raw)
    assert "Okay, let's" in out


def test_trim_to_last_sentence_drops_dangling_fragment():
    truncated = "Sure, that works great. Let me just check the calendar f"
    assert _trim_to_last_sentence(truncated) == "Sure, that works great."


def test_trim_to_last_sentence_keeps_text_with_no_boundary():
    # Never return an empty string just because the model never finished
    # a sentence before hitting max_tokens.
    assert _trim_to_last_sentence("thanks so much") == "thanks so much"


def test_llm_history_uses_configured_window_not_the_full_buffer():
    """Regression: settings.llm_history_turns was defined but never passed
    to recent_history(), so the small model always got the full 12-turn
    buffer instead of the intended shorter window. A longer, noisier
    context measurably worsens output quality on small models.
    """
    import asyncio

    state = ConversationState()
    for i in range(20):
        state.add_turn(f"user line {i}", f"assistant line {i}")

    service = LLMService.__new__(LLMService)
    messages = asyncio.run(LLMService.build_messages(service, state, "hello again"))

    history_messages = messages[1:-1]  # drop system + trailing user turn
    assert len(history_messages) == 2 * settings.llm_history_turns


def test_bare_number_answers_the_time_question():
    """Regression: a caller answering the time question with just '3' or
    'three' — the most natural way to answer it — used to be silently
    dropped, leaving time_pref empty and the same question repeating
    forever.
    """
    from app.agents.conversation_state import extract_time

    assert extract_time("3") == "3:00"
    assert extract_time("three") == "3:00"
    assert extract_time("three pm") == "3:00 PM"
    assert extract_time("3:30") == "3:30"


def test_ordinal_day_answers_the_date_question():
    """Regression: 'the 15th' / 'August 5th' used to be dropped by
    extract_date, leaving date_pref empty and ASK_DATE repeating."""
    from app.agents.conversation_state import extract_date

    assert extract_date("the 15th") == "the 15th"
    assert extract_date("August 5th") == "August 5"