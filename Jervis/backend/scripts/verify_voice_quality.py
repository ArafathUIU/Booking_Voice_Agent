"""
Headless regression harness for the refactored voice agent.

Drives the real ConversationManager (deterministic dialogue + tool calls +
optional LLM small-talk) WITHOUT LiveKit / Postgres / TTS, so we can prove the
agent speaks exactly one clean spoken line per turn and never emits code/JSON.

Run from the backend/ directory:

    python scripts/verify_voice_quality.py

Exit code is 0 when everything passes, 1 otherwise.
"""

import asyncio
import os
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"

sys.path.insert(0, str(BACKEND_DIR))


def load_root_env() -> None:
    if ROOT_ENV.exists():
        for line in ROOT_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


load_root_env()

from pipecat.frames.frames import TextFrame, TTSSpeakFrame  # noqa: E402

from app.agents.constants import FALLBACK_SPEECH  # noqa: E402
from app.agents.conversation_manager import ConversationManager  # noqa: E402
from app.agents.response_arbiter import sanitize_for_speech  # noqa: E402

TOOL_NAME_RE = re.compile(
    r"(?i)\b(check_availability|hold_slot|book_appointment|update_intent_context)\b"
)
CODE_KEYWORD_RE = re.compile(r"(?i)\b(def |import |class |console\.|function\(|=>|tool_call)\b")
MARKDOWN_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s|^\s*[-*•]\s+|`+|\*\*")


def probe_text(text: str) -> list:
    problems = []
    if not text:
        problems.append("empty reply")
        return problems
    if "{" in text or "}" in text or "[" in text or "]" in text:
        problems.append("contains brackets/JSON delimiters")
    if re.search(r"<[^>]+>", text):
        problems.append("contains angle-tag markup (e.g. <function=...>)")
    if TOOL_NAME_RE.search(text):
        problems.append("mentions a tool name")
    if CODE_KEYWORD_RE.search(text):
        problems.append("contains code keywords")
    if MARKDOWN_RE.search(text):
        problems.append("contains markdown")
    if text == FALLBACK_SPEECH:
        problems.append("fell back to generic fallback (unspeakable source text)")
    if re.search(r"(?<!\d)\d{2}:\d{2}(?!\d)", text):
        problems.append("contains 24h/ISO time like 15:00")
    return problems


def run_turn(m: ConversationManager, user_text: str) -> str:
    """Mirror one user turn; return the single response that would be spoken."""
    pushed = []

    async def capture(frame, direction=None):
        if isinstance(frame, TTSSpeakFrame):
            pushed.append(frame.text)
        elif isinstance(frame, TextFrame):
            pushed.append(frame.text)
        return frame

    orig = m.push_frame
    m.push_frame = capture
    try:
        asyncio.run(m._run_turn(user_text, None, None))
    finally:
        m.push_frame = orig

    assert len(pushed) == 1, f"expected exactly 1 response, got {len(pushed)}: {pushed}"
    return pushed[0]


def test_sanitizer():
    cases = [
        ("fenced code", "```json\n{\"a\":1}\n```\nHere is the answer.", False),
        ("json object inline", "The result is {\"slots\": [\"09:00\"]}. Call us.", False),
        ("json array", "Available are [09, 10, 11] o'clock.", False),
        ("tool name mention", "I will call check_availability now.", False),
        ("markdown bold/bullets", "* Cleaning\n* Whitening\n**Thanks**", False),
        ("code keyword only", "def foo(): return 1", True),
        ("empty string", "", True),
        ("natural speech", "We are open Monday through Friday from nine to six.", False),
        ("natural fillers", "Um, let me see. Uh, I can offer you four PM, okay?", False),
        ("glued sentences", "Are you booking?I think you mean an appointment.", False),
    ]
    passed = 0
    for label, raw, expect_fallback in cases:
        out = sanitize_for_speech(raw)
        ok = bool(out) and (out == FALLBACK_SPEECH) == expect_fallback
        print(f"  [{'PASS' if ok else 'FAIL'}] sanitizer '{label}': {out!r}")
        passed += int(ok)
    return passed == len(cases)


def test_turn_script():
    """Deterministic booking script: every turn must yield one clean line."""
    m = ConversationManager(session_id="live", tenant_id="live")
    script = [
        ("name", "My name is Sarah."),
        ("phone", "My phone number is 555-123-4567."),
        ("need", "I'd like to book a teeth cleaning."),
        ("when", "How about tomorrow at 3pm?"),
        ("pick", "Yes, the three o'clock works."),
        ("confirm", "Yes, book it."),
        ("done", "Thanks, that's all."),
    ]

    all_ok = True
    for label, user_text in script:
        try:
            speech = run_turn(m, user_text)
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR] '{label}' raised: {e}")
            all_ok = False
            continue
        problems = probe_text(speech)
        if problems:
            print(f"  [FAIL] '{label}' -> {speech!r} problems={problems}")
            all_ok = False
        else:
            print(f"  [PASS] '{label}' -> {speech!r}")

    if not m.state.booking_id:
        print("  [WARN] no booking completed during the script.")
    return all_ok


def test_llm_small_talk():
    """Free-form turn (all slots filled) goes through the real LLM if a Groq
    key is configured."""
    has_key = bool(os.environ.get("GROQ_API_KEY"))
    if not has_key:
        print("  [SKIP] No GROQ_API_KEY found -- LLM small-talk turn skipped.")
        return True

    m = ConversationManager(session_id="live2", tenant_id="live")
    # Everything is already gathered, so the policy can only fall through to the
    # free-form LLM response path.
    m.state.customer_name = "Sara"
    m.state.customer_phone = "5551234567"
    m.state.service = "Teeth Cleaning"
    m.state.date_pref = "tomorrow"
    m.state.time_pref = "3 PM"
    m.state.offered_slots = [{"time": "15:00", "available": True}]
    m.state.chosen_slot = {"time": "15:00", "available": True}
    m.state.pending_confirm = "booking"
    m.state.stage = "confirming"

    speech = run_turn(m, "What a lovely day today, isn't it?")
    problems = probe_text(speech)
    if problems or speech == FALLBACK_SPEECH:
        print(f"  [FAIL] small-talk -> {speech!r} problems={problems}")
        return False
    print(f"  [PASS] small-talk -> {speech!r}")
    return True


def main() -> int:
    results = []
    print("== Voice-Agent Response Quality Harness ==")
    print("\n[1/3] Offline sanitizer checks")
    results.append(("sanitizer", test_sanitizer()))
    print("\n[2/3] Deterministic booking script")
    results.append(("script", test_turn_script()))
    print("\n[3/3] LLM small-talk turn")
    results.append(("llm", test_llm_small_talk()))

    print("\n== Summary ==")
    ok = True
    for name, passed in results:
        print(f"  {name}: {'PASS' if passed else 'FAIL'}")
        ok = ok and passed
    print("== RESULT:", "PASS" if ok else "FAIL", "==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
