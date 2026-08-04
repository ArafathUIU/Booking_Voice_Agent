"""Download/cache the on-device STT (Whisper) and TTS (Kokoro) models.

pipecat processors must NOT be shared across pipelines. Reusing a single
Kokoro/Whisper service instance for every call caused the greeting frame to be
dropped on page reload: the new pipeline was wired to the same service instance
that the previous (still tearing down) pipeline was using, so frames went
nowhere.

Consequently these helpers only guarantee the model files are downloaded and
cached ahead of a call. Each pipeline builds its OWN service instance via
``create_stt_service()`` / ``create_tts_service()``.
"""

import asyncio
import logging
import threading

from app.config import settings

logger = logging.getLogger(__name__)

_WARMED = {"stt": False, "tts": False}
_LOCKS = {"stt": threading.Lock(), "tts": threading.Lock()}


def create_stt_service():
    """Build a fresh Whisper STT service for one pipeline (never shared)."""
    from app.agents.confident_stt import ConfidenceWhisperSTTService

    return ConfidenceWhisperSTTService(
        settings=ConfidenceWhisperSTTService.Settings(model=settings.stt_model, language="en"),
        device=settings.stt_device,
        compute_type=settings.stt_compute_type,
    )


def create_tts_service():
    """Build a fresh Kokoro TTS service for one pipeline (never shared)."""
    from pipecat.services.kokoro.tts import KokoroTTSService

    kwargs = {}
    if settings.kokoro_model_path:
        kwargs["model_path"] = settings.kokoro_model_path
    if settings.kokoro_voices_path:
        kwargs["voices_path"] = settings.kokoro_voices_path

    return KokoroTTSService(
        settings=KokoroTTSService.Settings(voice=settings.tts_voice),
        **kwargs,
    )


def warm_up_stt():
    """Download/cache the Whisper STT model once. Instance is discarded."""
    if _WARMED["stt"]:
        return
    with _LOCKS["stt"]:
        if not _WARMED["stt"]:
            logger.info("Warming up Whisper STT model '%s' ...", settings.stt_model)
            create_stt_service()
            _WARMED["stt"] = True
            logger.info("Whisper STT model '%s' ready.", settings.stt_model)


def warm_up_tts():
    """Download/cache the Kokoro TTS model once. Instance is discarded."""
    if _WARMED["tts"]:
        return
    with _LOCKS["tts"]:
        if not _WARMED["tts"]:
            logger.info("Warming up Kokoro TTS (voice '%s') ...", settings.tts_voice)
            create_tts_service()
            _WARMED["tts"] = True
            logger.info("Kokoro TTS ready.")


async def warm_up_models(*, fatal: bool = False) -> bool:
    """Warm up both models off the event loop. Returns True on full success.

    With ``fatal=True`` a load failure raises (used by the CLI so the operator
    sees the error). With ``fatal=False`` failures are logged but swallowed
    (used by server startup, which must not crash on a slow/blocked download).
    """
    ok = True
    try:
        await asyncio.to_thread(warm_up_stt)
    except Exception:
        logger.exception("STT model warm-up failed (model=%s)", settings.stt_model)
        if fatal:
            raise
        ok = False

    try:
        await asyncio.to_thread(warm_up_tts)
    except Exception:
        logger.exception("TTS model warm-up failed (voice=%s)", settings.tts_voice)
        if fatal:
            raise
        ok = False

    return ok
