"""Deterministic dialogue policy.

Decides the *next action* from the current conversation state and the caller's
latest utterance. The policy never calls an LLM: it only picks an action. The
conversation manager executes the action (asking a question, running a tool, or
delegating natural-language generation to the LLM service).

The booking flow is a fixed slot-completion machine:

  name -> phone -> service -> date/time -> check availability -> offer ->
  choose -> confirm -> book -> done

The caller can provide several slots in one sentence; extraction happens before
``decide`` runs, so missing slots shrink over time. Slot choices and yes/no
answers are matched against the live offered slots only, so the agent never
invents availability.

**Session memory:**
The conversation manager MUST keep the same ``ConversationState`` instance
(``state``) in memory for the entire session and pass it back to ``decide()``
on every turn. That object is the single source of truth for what has been
said, gathered, and offered so far.
"""

import random
import re
from datetime import datetime, timedelta
from typing import Optional

from app.agents.constants import KNOWLEDGE_BASE
from app.config import settings
from app.agents.conversation_state import (
    CONFIRMING,
    DONE,
    GATHERING,
    GREETING,
    OFFERING,
    TIME_WORDS,
)

# Actions
ASK_NAME = "ask_name"
ASK_PHONE = "ask_phone"
ASK_SERVICE = "ask_service"
ASK_DATE = "ask_date"
CHECK_AVAILABILITY = "check_availability"
OFFER_SLOTS = "offer_slots"
CHOOSE_SLOT = "choose_slot"
CONFIRM_BOOKING = "confirm_booking"
CONFIRM = "confirm"
CANCEL_CONFIRM = "cancel_confirm"
EXECUTE_BOOKING = "execute_booking"
CONFIRM_BOOKED = "confirm_booked"
ANSWER_QUESTION = "answer_question"
CLOSE = "close"
RESPOND = "respond"
CLARIFY = "clarify"
OUT_OF_HOURS = "out_of_hours"
UNAVAILABLE_TIME = "unavailable_time"

_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|okay|ok|confirm|confirmed|book it|book that|"
    r"sounds good|that works|that's fine|please do|go ahead)\b",
    re.I,
)
_NO_RE = re.compile(
    r"\b(no|nah|nope|not really|don'?t want|do not want|never mind|actually\s+no|"
    r"cancel it|change it)\b",
    re.I,
)

_BOOKING_KEYWORDS = [
    "book", "appointment", "schedule", "slot", "reschedule", "cancel",
    "cleaning", "whitening", "checkup", "check up", "dentist", "tooth",
    "filling", "canal", "extraction", "tomorrow", "today", "pm", "am",
    "morning", "afternoon", "evening", "next", "earliest", "available",
    "time",
]

_QUESTION_KEYWORDS = {
    "business_hours": ["what time are you", "what hours", "when are you", "hours", "open", "close"],
    "services": ["what services", "what do you offer", "what do you do", "services", "offer"],
    "location": ["where are you", "location", "address", "located", "where is the clinic"],
    "cancellation": ["cancel", "reschedule", "policy"],
    "pricing": ["how much", "price", "cost", "fee", "pricing", "charge"],
}

_CLOSING_KEYWORDS = [
    "thanks", "thank you", "goodbye", "bye", "that's all", "that is all",
    "no that's it", "no that's all", "all set", "have a good day",
    "take care", "see you",
]

# Signals that the caller is annoyed, confused, or struggling with the current
# question. When seen, the agent acknowledges instead of repeating the ask.
_FRUSTRATION_RE = re.compile(
    r"\b(repeat(?:ing|ed)?|repeating|again|can'?t|cannot|don'?t know|"
    r"don'?t remember|forgot|confus(?:ed|ing)|hard|difficult|not working|"
    r"annoying|frustrat|unfair|why|problem|stuck|stop|never mind|nevermind|"
    r"never)\b",
    re.I,
)

# Signals the caller needs the offered/confirmed details repeated or broken
# down again rather than a fresh question. Mapped to a gentle clarifying reply.
_CLARIFICATION_RE = re.compile(
    r"\b(which (?:one|time|slot|day)|could you say|can you say|say again|"
    r"what time|what times|what (?:days|slots)|repeat that|again|hold on|"
    r"i[`'’]m not sure|not sure which|don[`'’]t know which|too fast|"
    r"slow down|last one|lost you|missed that|missed you)\b",
    re.I,
)


def is_booking_intent(text_lower: str) -> bool:
    return any(kw in text_lower for kw in _BOOKING_KEYWORDS)


def is_yes(text_lower: str) -> bool:
    return bool(_YES_RE.search(text_lower))


def is_no(text_lower: str) -> bool:
    return bool(_NO_RE.search(text_lower))


def is_closing(text_lower: str) -> bool:
    return any(kw in text_lower for kw in _CLOSING_KEYWORDS)


def detect_question_topic(text_lower: str) -> Optional[str]:
    for topic, keys in _QUESTION_KEYWORDS.items():
        if any(k in text_lower for k in keys):
            return topic
    return None


def _time_candidates(text_lower: str) -> set:
    """Canonical 'HH:MM' candidates mentioned in the utterance."""
    candidates = set()
    for m in re.finditer(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)?\b",
        text_lower,
    ):
        hour = int(m.group(1))
        minute = m.group(2) or "00"
        meridian = (m.group(3) or "").lower()
        if "p" in meridian:
            if hour == 12:
                candidates.add(f"12:{minute}")
            elif 1 <= hour < 12:
                candidates.add(f"{hour + 12:02d}:{minute}")
        elif "a" in meridian:
            if hour == 12:
                candidates.add(f"00:{minute}")
            else:
                candidates.add(f"{hour:02d}:{minute}")
        elif hour >= 13:
            candidates.add(f"{hour:02d}:{minute}")
        else:
            candidates.add(f"{hour:02d}:{minute}")
            if 1 <= hour < 12:
                candidates.add(f"{hour + 12:02d}:{minute}")

    for word, num in TIME_WORDS.items():
        if re.search(rf"\b{word}\b", text_lower):
            if re.search(rf"\b{word}\s+(pm|p\.?m\.?)\b", text_lower):
                if num == 12:
                    candidates.add("12:00")
                elif num < 12:
                    candidates.add(f"{num + 12:02d}:00")
            elif re.search(rf"\b{word}\s+(am|a\.?m\.?)\b", text_lower):
                if num == 12:
                    candidates.add("00:00")
                else:
                    candidates.add(f"{num:02d}:00")
            else:
                candidates.add(f"{num:02d}:00")
                if 1 <= num < 12:
                    candidates.add(f"{num + 12:02d}:00")
    return candidates


def check_out_of_hours_request(text_lower: str) -> Optional[str]:
    """Return formatted spoken time if the caller requested an out-of-hours time (before 9 AM or 6 PM or later)."""
    start_hour = settings.business_hours_start
    end_hour = settings.business_hours_end
    candidates = _time_candidates(text_lower)
    if not candidates:
        return None
    # If any candidate falls within business hours (e.g. "3" -> 15:00), don't treat as out-of-hours
    for c in candidates:
        try:
            h = int(c.split(":")[0])
            if start_hour <= h < end_hour:
                return None
        except Exception:
            pass

    for c in candidates:
        try:
            h, m = map(int, c.split(":"))
            if h < start_hour or h >= end_hour:
                suffix = "AM" if h < 12 else "PM"
                h12 = h % 12 or 12
                return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"
        except Exception:
            continue
    return None


def check_unoffered_time_request(text_lower: str, offered_slots: list) -> Optional[str]:
    """Return formatted spoken time if the caller requested an in-hours time that is not among offered slots."""
    if text_lower.strip() in ("1", "2", "3", "4", "5"):
        return None
    candidates = _time_candidates(text_lower)
    if not candidates or not offered_slots:
        return None
    matched = any(_slot_aliases(s.get("time", "")) & candidates for s in offered_slots)
    if not matched:
        start_hour = settings.business_hours_start
        end_hour = settings.business_hours_end
        for c in candidates:
            try:
                h, m = map(int, c.split(":"))
                if start_hour <= h < end_hour:
                    suffix = "AM" if h < 12 else "PM"
                    h12 = h % 12 or 12
                    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"
            except Exception:
                continue
    return None


def _slot_aliases(slot_time: str) -> set:
    """Variants of a slot time so '3' can match '15:00'."""
    aliases = {slot_time}
    hour_str, minute = slot_time.split(":")
    hour = int(hour_str)
    aliases.add(f"{hour:d}:{minute}")
    aliases.add(hour_str)
    aliases.add(f"{hour:02d}")
    # 12-hour variants (e.g. 15:00 -> 3:00, 03:00, 3, 03)
    h12 = hour % 12 or 12
    aliases.add(f"{h12:d}:{minute}")
    aliases.add(f"{h12:02d}:{minute}")
    aliases.add(str(h12))
    aliases.add(f"{h12:02d}")
    return aliases


def match_slot_choice(text: str, offered_slots: list) -> Optional[dict]:
    """Return the first offered slot whose time the utterance points at."""
    if not offered_slots or not text:
        return None
    lower = text.lower()
    candidates = _time_candidates(lower)

    if candidates:
        for slot in offered_slots:
            if _slot_aliases(slot.get("time", "")) & candidates:
                return slot

    for bucket, lo, hi in (("morning", 9, 12), ("afternoon", 12, 17), ("evening", 17, 21)):
        if bucket in lower:
            for slot in offered_slots:
                try:
                    hour = int(slot.get("time", "").split(":")[0])
                except (ValueError, IndexError):
                    continue
                if lo <= hour < hi:
                    return slot
    return None


def _resolve_date_spoken(date_pref: str) -> str:
    """Turn relative words like 'tomorrow' into spoken weekday names."""
    if not date_pref:
        return "that day"
    lower = date_pref.lower().strip()
    
    # Use the resolver for proper timezone-aware date resolution
    from app.agents.conversation_state import ClinicDateTimeResolver
    resolver = ClinicDateTimeResolver()
    today = resolver._today_start()
    
    if lower in ("tomorrow", "tmrw", "tom"):
        day = today + timedelta(days=1)
        return f"tomorrow ({day.strftime('%A')})"
    if lower in ("today",):
        return f"today ({today.strftime('%A')})"
    if lower == "the day after tomorrow":
        day = today + timedelta(days=2)
        return f"day after tomorrow ({day.strftime('%A')})"
    
    # Try to resolve via the resolver for weekdays, "next Monday", etc.
    date_dt = resolver.resolve_date(date_pref)
    if date_dt:
        return date_dt.strftime("%A, %B %d")
    
    # Handle numeric dates like "1/2" -> "January 2"
    import re
    nm = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?\b", lower)
    if nm:
        month = int(nm.group(1))
        day = int(nm.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            dt = datetime(today.year, month, day, tzinfo=resolver.tz)
            return dt.strftime("%A, %B %d")
    
    # Handle "the 15th" style
    om = re.search(r"\b(\d{1,2})(st|nd|rd|th)\b", lower)
    if om:
        day = int(om.group(1))
        if 1 <= day <= 31 and len(lower.split()) <= 4:
            dt = datetime(today.year, today.month, day, tzinfo=resolver.tz)
            return dt.strftime("%A, %B %d")
    
    return date_pref


class DialogueManager:
    """Owns the deterministic next-action decision and question phrasing."""

    def decide(self, state, user_text: str):
        """Return ``(action, payload)`` for the current utterance.

        Payload carries whatever the executor needs (e.g. the matched slot).
        """
        lower = (user_text or "").lower().strip()

        # The agent already said goodbye. Do NOT restart the slot-gathering /
        # offer loop; only a fresh booking intent can start a new flow.
        if state.call_closed and not is_booking_intent(lower):
            return (CLOSE, {})

        # Closing the call (only when the booking flow isn't mid-request).
        if is_closing(lower) and (state.stage == DONE or not is_booking_intent(lower)):
            return (CLOSE, {})

        # Slot choice: caller names a time from the offered list. Allowed while
        # offering, confirming, or right after a check. Kept first so "actually
        # the 2 o'clock" still re-chooses even when a confirmation is pending.
        if state.offered_slots and state.stage in (OFFERING, CONFIRMING, DONE):
            slot = match_slot_choice(lower, state.offered_slots)
            if slot:
                return (CHOOSE_SLOT, {"slot": slot})

            # Check if caller specifically asked for an out-of-hours time (e.g. 9 PM)
            out_of_hours = check_out_of_hours_request(lower)
            if out_of_hours:
                return (OUT_OF_HOURS, {"requested_time": out_of_hours})

            # Check if caller asked for an in-hours time that is not among offered slots
            unoffered = check_unoffered_time_request(lower, state.offered_slots)
            if unoffered:
                return (UNAVAILABLE_TIME, {"requested_time": unoffered})

        # Yes/no while a confirmation is pending.
        if state.pending_confirm:
            if is_yes(lower):
                return (CONFIRM, {})
            if is_no(lower):
                return (CANCEL_CONFIRM, {})

            # If user asks a factual question while confirming
            topic = detect_question_topic(lower)
            if topic:
                return (ANSWER_QUESTION, {"topic": topic})

            # If user questions or discusses the date/time/slot
            if _CLARIFICATION_RE.search(lower) or any(w in lower for w in WEEKDAYS) or "tomorrow" in lower or "today" in lower or "schedule" in lower or "time" in lower:
                return (CLARIFY, {})

        # Factual questions are answered deterministically from the knowledge
        # base (never via the LLM, so answers stay correct and fast).
        if not state.pending_confirm:
            topic = detect_question_topic(lower)
            if topic and state.stage in (GREETING, GATHERING, OFFERING, CONFIRMING, DONE):
                return (ANSWER_QUESTION, {"topic": topic})

        # Caller asks for the offered/confirmed details again -> restate them
        # instead of re-asking a question.
        if state.offered_slots and _CLARIFICATION_RE.search(lower):
            return (CLARIFY, {})

        # Slot gathering in fixed order.
        if not state.customer_name:
            return (ASK_NAME, {})
        if not state.customer_phone:
            return (ASK_PHONE, {})
        if not state.service:
            return (ASK_SERVICE, {})
        if not state.date_pref:
            return (ASK_DATE, {})
        if not state.offered_slots:
            return (CHECK_AVAILABILITY, {})
        if state.chosen_slot is None:
            return (OFFER_SLOTS, {})
        if not state.pending_confirm:
            return (CONFIRM_BOOKING, {})

        # Nothing left to gather deterministically -> free-form response.
        return (RESPOND, {})

    def question_for(self, action: str, state, user_text: str = "") -> Optional[str]:
        count = state.ask_counts.get(action, 0) + 1
        state.ask_counts[action] = count

        if _FRUSTRATION_RE.search(user_text or ""):
            return self._frustrated_reply(action, state.customer_name)

        if count == 1:
            if action == ASK_NAME:
                return "May I have your name, please?"
            if action == ASK_PHONE:
                return "What is your phone number?"
            if action == ASK_SERVICE:
                return "What would you like help with today? We can book a cleaning, a checkup, or whitening."
            if action == ASK_DATE:
                return "What day would be perfect for you?"
            return None

        # Re-asks: vary the wording so the caller is not berated with the same
        # sentence, and give a gentle hint of what was (or wasn't) understood.
        if action == ASK_NAME:
            return random.choice([
                "Sorry, I didn't quite catch that. Could you tell me your name, please?",
                "I missed that. What name should I put on the booking?",
                "My apologies, I didn't hear your name clearly. Could you say it once more?",
            ])
        if action == ASK_PHONE:
            return random.choice([
                "What is your phone number?",
                "Could you share your phone number?",
                "I still need your phone number to proceed.",
            ])
        if action == ASK_SERVICE:
            return random.choice([
                "What treatment do you need today?",
                "What can I help you book?",
            ])
        if action == ASK_DATE:
            return random.choice([
                "What day would be perfect for you?",
                "Which day would you prefer?",
            ])
        return None

    def _frustrated_reply(self, action: str, name: Optional[str]) -> str:
        n = f", {name}" if name else ""
        if action == ASK_NAME:
            return "I'm sorry, I don't want to be difficult. Take your time — what's your name?"
        if action == ASK_PHONE:
            return f"I'm sorry for the hassle{n}. Whenever you're ready, just tell me your phone number."
        if action == ASK_SERVICE:
            return "No rush — just let me know what you'd like help with today."
        if action == ASK_DATE:
            return "No problem, just tell me the day you'd like to come in."
        return "I understand. Let's take it one step at a time."

    def offer_text(self, state) -> str:
        date_str = _resolve_date_spoken(state.date_pref)
        spoken = [s["spoken_time"] for s in state.offered_slots][:3]
        if not spoken:
            return f"Let me check availability for {date_str} again for you."
        count = state.ask_counts.get(OFFER_SLOTS, 0) + 1
        state.ask_counts[OFFER_SLOTS] = count
        list_str = ", ".join(spoken)
        if count == 1:
            return f"For {date_str}, we have {list_str} available. Which works best for you?"
        if count == 2:
            return f"On {date_str} we still have {list_str} open. Which one would you like?"
        return random.choice([
            f"For {date_str}, the times I have left are {list_str}. Which works best for you?",
            f"I have {list_str} available on {date_str}. Just tell me which time you'd prefer.",
        ])

    def confirm_text(self, state) -> str:
        name_part = f", {state.customer_name}," if state.customer_name else ","
        date_str = _resolve_date_spoken(state.date_pref)
        # Use spoken_time from chosen_slot if available, otherwise format time_pref
        if state.chosen_slot and state.chosen_slot.get("spoken_time"):
            time_str = state.chosen_slot["spoken_time"]
        else:
            time_str = state.time_pref
        return (
            f"Just to confirm{name_part} a {state.service} on {date_str} "
            f"at {time_str}. Shall I go ahead and book it?"
        )

    def booked_text(self, state) -> str:
        name = state.customer_name or "there"
        date_str = _resolve_date_spoken(state.date_pref)
        # Use spoken_time from chosen_slot if available, otherwise format time_pref
        if state.chosen_slot and state.chosen_slot.get("spoken_time"):
            time_str = state.chosen_slot["spoken_time"]
        else:
            time_str = state.time_pref
        return (
            f"Perfect, {name}! Your {state.service} is booked for {date_str} "
            f"at {time_str}. Is there any other information you need, or anything else you'd like to ask?"
        )

    def reoffer_text(self, state) -> str:
        date_str = _resolve_date_spoken(state.date_pref)
        spoken = [s["spoken_time"] for s in state.offered_slots][:3]
        if not spoken:
            return "No problem. Which time would you prefer instead?"
        list_str = ", ".join(spoken)
        count = state.ask_counts.get(OFFER_SLOTS, 0) + 1
        state.ask_counts[OFFER_SLOTS] = count
        if count <= 2:
            return f"No problem. For {date_str}, we also have {list_str} available. Which would you prefer?"
        return random.choice([
            f"Sure. The times I have for {date_str} are {list_str}. Which one suits you?",
            f"No worries. We've still got {list_str} on {date_str}. Which would you like?",
        ])

    def clarify_text(self, state) -> str:
        date_str = _resolve_date_spoken(state.date_pref)
        if state.pending_confirm and state.chosen_slot:
            name_part = f", {state.customer_name}" if state.customer_name else ""
            time_str = state.chosen_slot.get("spoken_time") or state.time_pref
            return (
                f"Yes, exactly{name_part}! We are confirming your {state.service} on {date_str} "
                f"at {time_str}. Shall I go ahead and book it, or would you prefer a different time?"
            )
        spoken = [s["spoken_time"] for s in state.offered_slots][:3]
        if not spoken:
            return "Of course. We're booking your appointment — just let me know which time you'd prefer."
        return random.choice([
            f"Sorry about that. For {date_str}, we have {', '.join(spoken)} available. Which works best for you?",
            f"Of course — I have {', '.join(spoken)} open on {date_str}. Which time would you like?",
        ])

    def close_text(self, state) -> str:
        name = state.customer_name or "there"
        count = state.ask_counts.get(CLOSE, 0) + 1
        state.ask_counts[CLOSE] = count
        if state.booking_id:
            return (
                f"All set, {name}! Your appointment is confirmed. "
                f"Thanks for calling and have a wonderful day!"
            )
        if count == 1:
            return f"Thanks for calling, {name}! You can book anytime by calling us back. Have a great day!"
        return f"Thanks again, {name}! I'm here if you need anything else. Take care!"

    def booked_close_text(self, state) -> str:
        name = state.customer_name or "there"
        return (
            f"Thank you, {name}! Your appointment is confirmed. "
            f"Have a great day!"
        )

    def answer_question(self, topic: str) -> Optional[str]:
        content = KNOWLEDGE_BASE.get(topic)
        if not content:
            return None
        return "Sure, " + content