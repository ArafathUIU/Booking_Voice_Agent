"""Per-call conversational state plus deterministic slot extraction.

The dialogue policy, arbitration, and LLM service all operate on a
``ConversationState`` instance instead of mutating a shared monolith. The state
holds the slots the caller has provided, the current stage, what was offered,
and a *clean* history (real user/assistant turns only) that is safe to hand to
an LLM.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings
from zoneinfo import ZoneInfo

from app.config import settings

# Dialogue stages (deterministic progression managed by the dialogue manager)
GREETING = "greeting"
GATHERING = "gathering"
CHECKING = "checking"
OFFERING = "offering"
CONFIRMING = "confirming"
BOOKING = "booking"
DONE = "done"

# Canonical service names, keyed by aliases the caller might say.
SERVICE_ALIASES = {
    "teeth cleaning": "Teeth Cleaning",
    "deep cleaning": "Teeth Cleaning",
    "cleaning": "Teeth Cleaning",
    "clean teeth": "Teeth Cleaning",
    "checkup": "Checkup",
    "check up": "Checkup",
    "check-up": "Checkup",
    "whitening": "Whitening",
    "white teeth": "Whitening",
    "root canal": "Root Canal",
    "root channel": "Root Canal",
    "canal": "Root Canal",
    "root": "Root Canal",
    "filling": "Filling",
    "extraction": "Extraction",
    "tooth pulled": "Extraction",
    "wisdom tooth": "Wisdom Tooth",
    "braces": "Braces",
    "crown": "Crown",
    "implant": "Implant",
}

# Words that must never be captured as a bare first/last name. Checked per
# token (not substring) so a name like "Smith" is not rejected because it
# contains "is".
_NAME_EXCLUDED = {
    "yes", "yeah", "yep", "yup", "no", "nah", "nope", "ok", "okay", "sure",
    "hi", "hello", "hey", "thanks", "thank", "bye", "goodbye", "uh", "um",
    "mhm", "hmm", "mm", "right", "got", "fine", "good", "great", "please",
    "appointment", "book", "booking", "cleaning", "checkup", "whitening",
    "dentist", "clinic", "phone", "number", "wait", "call", "can", "i", "my",
    "and", "the", "a", "an", "with", "for", "on", "at", "is", "it", "its",
    "am", "pm", "noon", "o'clock", "today", "tomorrow",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "said", "saying", "say", "tell", "telling", "told", "ask", "asking",
    "think", "thinking", "thought", "look", "looking", "want", "wanting",
    "need", "needing", "know", "knowing", "see", "seeing", "seen", "show", "showing",
    "hear", "hearing", "heard", "help", "helping", "hope", "hoping",
    "try", "trying", "go", "going", "gone", "come", "coming", "came",
    "done", "do", "doing", "make", "making", "made", "get", "getting",
    "take", "taking", "talk", "talking", "mean", "meaning", "meant",
    "slot", "slots", "time", "times", "day", "days", "week", "month",
    "here", "there", "ready", "available", "closed", "open", "schedule",
}

WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

TIME_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


class ClinicDateTimeResolver:
    """Resolves relative date/time expressions to timezone-aware datetimes."""

    def __init__(self, timezone: str = None):
        self.tz = ZoneInfo(timezone or settings.clinic_timezone)
        self.business_start = settings.business_hours_start
        self.business_end = settings.business_hours_end

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _today_start(self) -> datetime:
        now = self._now()
        return datetime(now.year, now.month, now.day, tzinfo=self.tz)

    def resolve_date(self, date_text: str) -> Optional[datetime]:
        """Convert 'today', 'tomorrow', 'Monday', 'next Friday' to date at midnight."""
        if not date_text:
            return None
        lower = date_text.lower().strip()
        today = self._today_start()

        if lower == "today":
            return today
        if lower == "tomorrow":
            return today + timedelta(days=1)
        if lower in ("the day after tomorrow", "day after tomorrow"):
            return today + timedelta(days=2)
        if "two days after tomorrow" in lower or "2 days after tomorrow" in lower:
            return today + timedelta(days=3)

        # "next Monday", "this Friday"
        m = re.match(
            r"^(next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
            lower,
        )
        if m:
            prefix, weekday = m.groups()
            target_weekday = WEEKDAYS.index(weekday)
            current_weekday = today.weekday()
            days_ahead = (target_weekday - current_weekday) % 7
            if prefix == "next" and days_ahead == 0:
                days_ahead = 7
            return today + timedelta(days=days_ahead)

        # Bare weekday: "Monday" -> next occurrence
        if lower in WEEKDAYS:
            target_weekday = WEEKDAYS.index(lower)
            current_weekday = today.weekday()
            days_ahead = (target_weekday - current_weekday) % 7
            if days_ahead == 0:
                days_ahead = 7
            return today + timedelta(days=days_ahead)

        # "Month Day" or "August 15" style
        for i, month in enumerate(_MONTHS, 1):
            if month in lower:
                day_match = re.search(rf"{month}\s+(\d{{1,2}})", lower)
                if day_match:
                    day = int(day_match.group(1))
                    return datetime(today.year, i, day, tzinfo=self.tz)

        # Numeric date: "8/10", "8/10/2026"
        nm = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?\b", lower)
        if nm:
            month = int(nm.group(1))
            day = int(nm.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(today.year, month, day, tzinfo=self.tz)

        return None

    def resolve_time(self, time_text: str) -> Optional[time]:
        """Convert '3 PM', '15:00', 'morning' to a time object."""
        if not time_text:
            return None
        lower = time_text.lower().strip()

        # Period labels
        period_map = {
            "morning": (9, 0),
            "afternoon": (13, 0),
            "evening": (17, 0),
            "noon": (12, 0),
            "as soon as possible": (9, 0),
        }
        if lower in period_map:
            h, m = period_map[lower]
            return time(h, m)

        # "3 PM", "15:00", "3:30 PM"
        m = re.search(
            r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)?\b",
            lower,
        )
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or "00")
            meridian = (m.group(3) or "").lower()
            if "p" in meridian and hour < 12:
                hour += 12
            if "a" in meridian and hour == 12:
                hour = 0
            if hour > 23:
                hour = 23
            return time(hour, minute)

        # Word form: "three PM"
        for word, num in TIME_WORDS.items():
            if re.search(rf"\b{word}\b", lower):
                wm = re.search(rf"\b{word}\s+(a\.?m\.?|p\.?m\.?|o'?clock)\b", lower)
                if wm:
                    meridian = wm.group(1).lower()
                    if "p" in meridian and num < 12:
                        num += 12
                    if "a" in meridian and num == 12:
                        num = 0
                    return time(num, 0)

        return None

    def combine(self, date_dt: datetime, time_obj: time) -> datetime:
        """Combine a date datetime and a time into a single timezone-aware datetime."""
        return datetime(
            date_dt.year, date_dt.month, date_dt.day,
            time_obj.hour, time_obj.minute, tzinfo=self.tz,
        )

    def display_date(self, dt: datetime) -> str:
        """Human-readable date for speech."""
        return dt.strftime("%A, %B %d")

    def display_time(self, dt: datetime) -> str:
        """Human-readable time for speech."""
        hour = dt.hour
        minute = dt.minute
        suffix = "AM" if hour < 12 else "PM"
        h12 = hour % 12 or 12
        if minute == 0:
            return f"{h12} {suffix}"
        return f"{h12}:{minute:02d} {suffix}"

    def display_datetime(self, dt: datetime) -> str:
        """Human-readable date and time for speech."""
        return f"{self.display_date(dt)} at {self.display_time(dt)}"


_PHONE_LEAD = (
    r"(?:phone(?:\s*(?:number|#))?|my\s+number|cell(?:phone)?|"
    r"mobile(?:\s*number)?|contact\s+number)\s*(?:is|:)?\s*"
)
# Real phone numbers are typically 7-11 digits. Below 7 is almost always a
# partial/garbled capture ("01742", "8044"), so require 7+ to move on.
_PHONE_MIN_DIGITS = 7


def extract_name(text: str, *, allow_bare: bool = True) -> Optional[str]:
    """Best-effort name capture.

    Phrase patterns first ("my name is X", "I'm X", "call me X", "this is X",
    "it's X", "the name is X"). If nothing matches and ``allow_bare`` is set, a
    short all-alpha utterance (e.g. just "John" or "John Smith") is treated as
    a name — with a guard list so times/services/noise are never swallowed.
    """
    if not text:
        return None

    phrase = re.search(
        r"(?:my name(?:'s|\s+is)|\bi'?m\b|\bi\s+am\b|\bcall me|"
        r"\bthe name\s+is|i\s+go\s+by)\s+([a-zA-Z]+)",
        text,
        re.I,
    )
    if phrase:
        token = phrase.group(1).lower()
        if token not in _NAME_EXCLUDED and not token.endswith("ing"):
            return phrase.group(1).capitalize()

    for lead in (r"\bthis is\s+([a-zA-Z]+)", r"\bit'?s\s+([a-zA-Z]+)"):
        match = re.search(lead, text, re.I)
        if match:
            token = match.group(1)
            t_lower = token.lower()
            if t_lower not in _NAME_EXCLUDED and not t_lower.endswith("ing") and not t_lower.endswith("ed"):
                if token[:1].isupper() or t_lower not in (
                    "the", "a", "an", "my", "your", "our", "not", "going",
                    "what", "how", "why", "just", "very", "really", "about",
                    "me", "him", "her", "them",
                ):
                    return token.capitalize()

    if allow_bare:
        cleaned = text.strip().rstrip(".?!, ")
        if not cleaned:
            return None
        tokens = cleaned.split()
        if 1 <= len(tokens) <= 2 and all(t.isalpha() for t in tokens):
            lower_tokens = [t.lower() for t in tokens]
            if any(t in _NAME_EXCLUDED for t in lower_tokens):
                return None
            return cleaned.title()
    return None


def extract_phone(text: str) -> Optional[str]:
    """Capture a phone number, even without "my number is" phrasing."""
    if not text:
        return None

    m = re.search(
        _PHONE_LEAD + r"([+]?[\d][\d\s\-().]{2,}\d)",
        text,
        re.I,
    )
    group = m.group(1) if m and m.lastindex else None
    if not group:
        m = re.search(r"\b[+]?\d[\d\s\-().]{2,}\d\b", text)
        group = m.group(0) if m else None
    if not group:
        return None

    digits = re.sub(r"\D", "", group)
    if len(digits) >= _PHONE_MIN_DIGITS:
        return digits
    return None


def extract_service(text: str) -> Optional[str]:
    """Return the canonical service name if the caller names one.

    Exact alias match first ("root canal"), then a phonetic-leaning fallback so
    ASR mis-hears like "root cala", "havocation" (vacation), or "white teeth"
    still resolve to the right service.
    """
    if not text:
        return None
    lower = text.lower()
    for alias, canonical in sorted(SERVICE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in lower:
            return canonical

    # Fuzzy fallback: try to match any alias against the whole normalized
    # utterance, allowing a few dropped/garbled characters from ASR.
    norm = re.sub(r"[^a-z ]", "", lower).strip()
    if not norm:
        return None
    best_canonical, best_score = None, 0.0
    for alias, canonical in SERVICE_ALIASES.items():
        score = _fuzzy_similarity(norm, alias)
        if score > best_score and score >= 0.62:
            best_score, best_canonical = score, canonical
    return best_canonical


def _fuzzy_similarity(a: str, b: str) -> float:
    """Cheap normalized edit-distance similarity in [0, 1]."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    a, b = a.lower(), b.lower()
    longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
    dp = list(range(len(shorter) + 1))
    for i, ca in enumerate(longer, 1):
        prev = dp[0]
        dp[0] = i
        for j, cb in enumerate(shorter, 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + (ca != cb))
            prev = cur
    distance = dp[-1]
    return 1.0 - (distance / max(len(longer), 1))


_ORDINAL_DATE_RE = re.compile(
    r"\b(?:(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+)?(\d{1,2})(?:st|nd|rd|th)?\b"
)

_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)

# Common short weekday abbreviations a caller might say instead of the full
# name. Gated to short replies only (see below) so real words like "sat"
# ("I sat down") or "wed" aren't misread as a day when they show up inside a
# longer sentence unrelated to scheduling.
_WEEKDAY_ABBR = {
    "mon": "Monday", "tue": "Tuesday", "tues": "Tuesday",
    "wed": "Wednesday", "weds": "Wednesday", "thu": "Thursday",
    "thur": "Thursday", "thurs": "Thursday", "fri": "Friday",
    "sat": "Saturday", "sun": "Sunday",
}


def extract_date(text: str) -> Optional[str]:
    """Canonical date labels: today / tomorrow / weekday, a month + day, or a
    bare ordinal day ("the 15th") said in direct reply to a date question.
    """
    if not text:
        return None
    lower = text.lower()

    # Check "two days after tomorrow" / "day after tomorrow" phrasing BEFORE
    # the plain "tomorrow" check below — otherwise the substring match on
    # "tomorrow" fires first and silently returns the wrong day with no
    # error and no re-ask.
    m_days = re.search(r"\b(?:(two|\d+)\s+)?days?\s+after\s+tomorrow\b", lower)
    if m_days:
        num_word = m_days.group(1)
        if num_word:
            return "two days after tomorrow" if num_word in ("two", "2") else f"{num_word} days after tomorrow"
        return "the day after tomorrow"
    if re.search(r"\btoday\b", lower):
        return "today"
    if re.search(r"\btomorrow\b", lower):
        return "tomorrow"

    m = re.search(
        r"\b((?:next|this)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lower,
    )
    if m:
        prefix = (m.group(1) or "").strip()
        weekday = m.group(2).capitalize()
        return f"{prefix.capitalize()} {weekday}" if prefix else weekday

    # "August 5th" / "the 5th of August" style — month name plus day.
    om = _ORDINAL_DATE_RE.search(lower)
    if om and om.group(1):
        month = om.group(1).capitalize()
        day = int(om.group(2))
        if 1 <= day <= 31:
            return f"{month} {day}"

    # Numeric date: "8/10", "8/10/2026", "8-10".
    nm = re.search(r"\b(0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])(?:[/-]\d{2,4})?\b", lower)
    if nm:
        return f"{int(nm.group(1))}/{int(nm.group(2))}"

    # Bare ordinal day ("the 15th") with no month named. Only trustworthy
    # when it's basically the whole answer — i.e. a direct reply to "what
    # day works for you?" — so it doesn't swallow unrelated numbers.
    om2 = re.search(r"\b(\d{1,2})(st|nd|rd|th)\b", lower)
    if om2:
        day = int(om2.group(1))
        if 1 <= day <= 31 and len(lower.split()) <= 4:
            return f"the {day}{om2.group(2)}"

    # Weekday abbreviation ("fri", "mon works") — only when the reply is
    # short, i.e. this IS the answer to the date question, not a stray
    # 3-letter word inside an unrelated sentence.
    if len(lower.split()) <= 3:
        for tok in re.findall(r"[a-z]+", lower):
            if tok in _WEEKDAY_ABBR:
                return _WEEKDAY_ABBR[tok]
    return None


# Trigger words that make a bare hour ("3") unambiguous even without am/pm.
_TIME_TRIGGER_RE = re.compile(
    r"\b(at|around|for|about|after|by|like|prefer|works|want|pick|choose|take|book)\b\s*$"
)


def extract_time(text: str) -> Optional[str]:
    """Canonical time labels: 'morning', 'afternoon', 'evening', 'noon', '3 PM',
    or a bare hour ("3", "three") said in direct reply to a time question.
    """
    if not text:
        return None
    lower = text.lower()

    for kw, label in (
        ("morning", "morning"),
        ("afternoon", "afternoon"),
        ("evening", "evening"),
        ("noon", "noon"),
        ("asap", "as soon as possible"),
    ):
        if re.search(rf"\b{kw}\b", lower):
            return label

    hour = minute = None
    meridian = ""
    has_colon_minute = False
    match_start = 0

    m = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)?\b",
        lower,
    )
    if m:
        hour = int(m.group(1))
        has_colon_minute = m.group(2) is not None
        minute = m.group(2) or "00"
        meridian = (m.group(3) or "").lower()
        match_start = m.start()
    else:
        # Word form ("three", "three pm") — digits regex above only matches
        # numerals, so a spoken-word hour used to fall through to None and
        # the same question would repeat forever waiting for a digit that
        # never came.
        for word, num in TIME_WORDS.items():
            wm = re.search(rf"\b{word}\b", lower)
            if wm:
                hour = num
                minute = "00"
                match_start = wm.start()
                mm = re.search(rf"\b{word}\s+(a\.?m\.?|p\.?m\.?|o'?clock)\b", lower)
                meridian = (mm.group(1) if mm else "").lower()
                break

    if hour is None:
        return None

    if "p" in meridian:
        h12 = hour % 12 or 12
        return f"{h12}:{minute} PM"
    if "a" in meridian:
        h12 = hour % 12 or 12
        return f"{h12}:{minute} AM"
    if "o'clock" in meridian:
        return f"{hour % 12 or 12}:{minute}"
    if hour >= 13:
        # Military-style "15" or "15:00"
        return f"{hour % 12 or 12}:{minute} PM"
    if not (1 <= hour <= 12):
        return None

    # Explicit "H:MM" ("3:30") is unambiguous on its own — no lead-in needed.
    if has_colon_minute:
        return f"{hour}:{minute}"

    # Bare hour with no am/pm/colon: accept next to a trigger word ("at 3"),
    # OR when the reply is essentially just the number/word itself — the
    # normal, direct way a caller answers "what time would you prefer?".
    before = lower[max(0, match_start - 24):match_start]
    if _TIME_TRIGGER_RE.search(before):
        return f"{hour}:{minute}"
    if len(lower.split()) <= 3:
        return f"{hour}:{minute}"
    return None


def extract_slots(text: str, *, allow_bare_name: bool = True) -> dict:
    """Extract every slot mentioned in a single utterance."""
    out: dict = {}
    if not text:
        return out
    name = extract_name(text, allow_bare=allow_bare_name)
    if name:
        out["name"] = name
    phone = extract_phone(text)
    if phone:
        out["phone"] = phone
    service = extract_service(text)
    if service:
        out["service"] = service
    date_pref = extract_date(text)
    if date_pref:
        out["date"] = date_pref
    time_pref = extract_time(text)
    if time_pref:
        out["time"] = time_pref
    return out


@dataclass
class ConversationState:
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    service: Optional[str] = None
    date_pref: Optional[str] = None
    time_pref: Optional[str] = None
    dentist: Optional[str] = None
    notes: list = field(default_factory=list)

    stage: str = GREETING
    pending_question: Optional[str] = None
    offered_slots: list = field(default_factory=list)
    chosen_slot: Optional[dict] = None
    pending_confirm: Optional[str] = None
    booking_id: Optional[str] = None
    booking_outcome: Optional[dict] = None

    # Resolved appointment datetime (timezone-aware)
    resolved_appointment_dt: Optional[datetime] = None

    # Clean history: only real user/assistant turns (no internal messages).
    history: list = field(default_factory=list)
    turn_count: int = 0

    # How many times each slot question has been asked, so the agent can vary
    # its wording instead of repeating the exact same sentence verbatim.
    ask_counts: dict = field(default_factory=dict)

    # Diagnostic values for this call
    last_asr_text: str = ""
    last_confidence: Optional[float] = None

    # Set once the agent has said goodbye / closed the call, so we stop
    # re-offering slots or gathering more slots afterwards.
    call_closed: bool = False

    MAX_HISTORY_TURNS = 12

    def apply_extracted(self, extracted: dict, user_text: str = ""):
        name = extracted.get("name") or extracted.get("customer_name")
        if name:
            if not self.customer_name:
                self.customer_name = name
            else:
                lower = (user_text or "").lower()
                if any(p in lower for p in ("my name is actually", "actually my name is", "call me", "change my name", "not my name", "correct my name")):
                    self.customer_name = name
        phone = extracted.get("phone") or extracted.get("customer_phone")
        if phone:
            self.customer_phone = phone
        service = extracted.get("service")
        if service:
            self.service = service
        date = extracted.get("date") or extracted.get("date_pref")
        if date:
            if not self.date_pref or self.stage != CONFIRMING:
                self.date_pref = date
            else:
                lower = (user_text or "").lower()
                if any(p in lower for p in ("change date", "different date", "different day", "reschedule", "instead", "switch to", "prefer", "can we do", "how about")):
                    self.date_pref = date
        time = extracted.get("time") or extracted.get("time_pref")
        if time:
            self.time_pref = time

    def resolve_appointment_dt(self, resolver: "ClinicDateTimeResolver") -> Optional[datetime]:
        """Resolve date_pref + time_pref into a timezone-aware datetime."""
        if not self.date_pref or not self.time_pref:
            return None
        date_dt = resolver.resolve_date(self.date_pref)
        time_obj = resolver.resolve_time(self.time_pref)
        if date_dt and time_obj:
            self.resolved_appointment_dt = resolver.combine(date_dt, time_obj)
            return self.resolved_appointment_dt
        return None

    def is_complete(self) -> bool:
        return all(
            (self.customer_name, self.customer_phone, self.service,
             self.date_pref, self.time_pref)
        )

    def slot_summary(self) -> str:
        parts = []
        if self.customer_name:
            parts.append(f"Name: {self.customer_name}")
        if self.customer_phone:
            parts.append(f"Phone: {self.customer_phone}")
        if self.service:
            parts.append(f"Service: {self.service}")
        if self.date_pref:
            parts.append(f"Date: {self.date_pref}")
        if self.time_pref:
            parts.append(f"Time: {self.time_pref}")
        if self.dentist:
            parts.append(f"Dentist: {self.dentist}")
        return "; ".join(parts) if parts else "none"

    def add_turn(self, user_text: Optional[str], assistant_text: Optional[str]):
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text and not assistant_text:
            return
        if user_text:
            self.history.append({"role": "user", "content": user_text})
        if assistant_text:
            self.history.append({"role": "assistant", "content": assistant_text})
        limit = 2 * self.MAX_HISTORY_TURNS
        if len(self.history) > limit:
            self.history = self.history[-limit:]

    def add_assistant_line(self, text: str):
        text = (text or "").strip()
        if text:
            self.history.append({"role": "assistant", "content": text})
            limit = 2 * self.MAX_HISTORY_TURNS
            if len(self.history) > limit:
                self.history = self.history[-limit:]

    def recent_history(self, max_turns: Optional[int] = None) -> list:
        max_turns = max_turns or self.MAX_HISTORY_TURNS
        return self.history[-(2 * max_turns):]

    def to_intent_context(self) -> dict:
        return {
            "current_intent": self.stage,
            "mentioned_slots": [s for s in (self.date_pref, self.time_pref, self.service) if s],
            "preferred_service": self.service,
            "customer_name": self.customer_name,
            "customer_phone": self.customer_phone,
            "pending_confirmation": self.pending_confirm == "booking",
            "last_question": self.pending_question,
            "user_answered_yes_no": None,
            "conversation_turns": self.turn_count,
        }