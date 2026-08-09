"""JSON-lines conversation/event log for debugging the Twilio voice flow.

Writes one compact JSON object per line to ``logs/voice_events.jsonl`` (inside
the mounted ``backend/`` volume, so it is visible on the host at
``backend/logs/voice_events.jsonl``). Every step of a call — TwiML served,
each spoken turn, status callbacks, dial results, errors — is appended with a
UTC timestamp so we can reconstruct exactly what Twilio did and when.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_DIR = Path(os.environ.get("VOICE_LOG_DIR", Path(__file__).resolve().parents[2] / "logs"))
_LOG_FILE = _LOG_DIR / "voice_events.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_event(event_type: str, **fields) -> None:
    """Append one JSON line. Never raises — logging must not break the call."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": _now_iso(),
            "event": event_type,
            **fields,
        }
        with _LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("voice event log write failed (event=%s)", event_type)
