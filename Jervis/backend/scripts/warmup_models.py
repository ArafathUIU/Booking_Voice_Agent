"""
One-time model warm-up: download + load the on-device STT (Whisper) and TTS
(Kokoro) models so a live call never stalls on a first-run download.

This is the same warm-up the server now runs automatically in the background at
startup (see app/main.py). Use this CLI when you want the download to happen
eagerly / in the foreground and to see errors clearly:

    cd backend
    python scripts/warmup_models.py

It uses the same environment (STT_MODEL, STT_DEVICE, STT_COMPUTE_TYPE,
TTS_VOICE, KOKORO_MODEL_PATH, KOKORO_VOICES_PATH). Exit code 0 on success.
"""

import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
sys.path.insert(0, str(BACKEND_DIR))

if ROOT_ENV.exists():
    for line in ROOT_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

from app.agents.model_warmup import warm_up_models  # noqa: E402


def main() -> int:
    # fatal=True: a failed download raises so the operator sees the error.
    ok = asyncio.run(warm_up_models(fatal=True))
    if ok:
        print("Warm-up complete. Models are cached and ready for live calls.")
        return 0
    print("Warm-up reported failures (see logs above).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
