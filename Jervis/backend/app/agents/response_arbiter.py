"""Response arbitration.

Guarantees the caller hears exactly ONE response per turn. Sources (dialogue
policy, tool results, LLM) submit proposals; ``resolve`` picks a single winner
by priority; ``push_once`` enforces the one-frame-per-turn invariant so no code
path can double-speak.
"""

import logging
import re

from app.agents.constants import FALLBACK_SPEECH

logger = logging.getLogger(__name__)

PRIORITY = {
    "repair": 100,       # caller couldn't be heard -> ask to repeat
    "action": 90,        # deterministic dialogue action (question/offer/confirm)
    "tool": 85,          # tool-driven announcement
    "knowledge": 70,     # FAQ answer from knowledge base
    "llm": 50,           # free-form LLM generation
    "fallback": 10,      # last resort
}


def sanitize_for_speech(text: str) -> str:
    """Strip code/JSON/markup so TTS never reads technical junk."""
    if not text:
        return FALLBACK_SPEECH

    cleaned = text.strip()

    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
    cleaned = re.sub(r"`[^`]+`", " ", cleaned)
    cleaned = re.sub(r"(?is)<\s*/?\s*function[^>]*>.*?</\s*function\s*>", " ", cleaned)
    cleaned = re.sub(r"(?is)<\s*/?\s*function[^>]*>", " ", cleaned)
    cleaned = re.sub(r"<[^>]{0,120}>", " ", cleaned)
    cleaned = re.sub(r"\{[^{}]{0,400}\}", " ", cleaned)
    cleaned = re.sub(r"\[[^\[\]]{0,400}\]", " ", cleaned)
    cleaned = re.sub(r"(?i)\b(check_availability|hold_slot|book_appointment|update_intent_context)\b", " ", cleaned)
    cleaned = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*[-*•]\s+", "", cleaned)
    cleaned = re.sub(r"[*_~]{1,3}", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # The small model sometimes glues sentences ("question?You'd like...");
    # TTS would read that as one breath. Put the space back.
    cleaned = re.sub(r"([?!]+)(?=[A-Za-z0-9])", r"\1 ", cleaned)
    cleaned = re.sub(r"\.(?=[A-Z])", ". ", cleaned)
    cleaned = re.sub(r",(?=[A-Za-z])", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        return FALLBACK_SPEECH
    if cleaned.startswith(("{", "[", "<")):
        return FALLBACK_SPEECH
    if re.search(r"(?i)\b(def |import |class |console\.|function\(|=>)\b", cleaned):
        return FALLBACK_SPEECH
    letters = sum(c.isalpha() or c.isspace() for c in cleaned)
    if letters < max(8, int(0.55 * len(cleaned))):
        return FALLBACK_SPEECH

    return cleaned


class ResponseArbiter:
    def __init__(self):
        self._proposals: list = []
        self._pushed_this_turn = False

    def reset_turn(self):
        self._proposals = []
        self._pushed_this_turn = False

    def propose(self, source: str, text: str):
        """Register a candidate response from a source."""
        text = (text or "").strip()
        if not text:
            return
        self._proposals.append((source, text, PRIORITY.get(source, 10)))

    def resolve(self) -> str:
        if not self._proposals:
            return FALLBACK_SPEECH
        best = max(self._proposals, key=lambda p: p[2])
        return sanitize_for_speech(best[1])

    def push_once(self) -> bool:
        """Return True if a frame may be pushed this turn; False otherwise."""
        if self._pushed_this_turn:
            logger.warning("arbiter: blocked a second push for this turn")
            return False
        self._pushed_this_turn = True
        return True
