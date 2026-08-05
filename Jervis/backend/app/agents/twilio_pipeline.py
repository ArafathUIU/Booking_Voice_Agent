"""Twilio Media Streams pipeline flavor.

Twilio streams the live call audio to our WebSocket endpoint (see
twilio_ws.py). This builds the exact same pipeline as the LiveKit/web flavor
(Whisper STT -> ConversationManager -> Kokoro TTS) but bridged over a
``FastAPIWebsocketTransport`` speaking the Twilio Media Streams protocol
(8kHz mulaw <-> 16kHz PCM handled by ``TwilioFrameSerializer``).
"""

import asyncio
import logging

from fastapi import WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner, PipelineWorker

from app.agents.conversation_manager import ConversationManager
from app.agents.model_warmup import create_stt_service, create_tts_service, warm_up_stt, warm_up_tts
from app.config import settings

logger = logging.getLogger(__name__)


async def build_and_run_twilio_pipeline(
    websocket: WebSocket,
    *,
    stream_sid: str,
    call_sid: str,
    session_id: str,
    tenant_id: str,
    lead_context: dict | None = None,
):
    """Run the booking-agent pipeline over a Twilio Media Streams WebSocket.

    Blocks until the call ends (WebSocket closes), then returns. The caller is
    responsible for finalizing the session/lead afterwards.
    """
    logger.info("Loading STT model: model=%s device=%s", settings.stt_model, settings.stt_device)
    try:
        warm_up_stt()
        stt = create_stt_service()
    except Exception:
        logger.exception("Failed to load Whisper STT model '%s'", settings.stt_model)
        raise

    logger.info("Loading TTS: voice=%s", settings.tts_voice)
    try:
        warm_up_tts()
        tts = create_tts_service()
    except Exception:
        logger.exception("Failed to load Kokoro TTS (voice=%s)", settings.tts_voice)
        raise

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        params=TwilioFrameSerializer.InputParams(
            twilio_sample_rate=8000,
            sample_rate=16000,
            auto_hang_up=True,
        ),
    )
    transport = FastAPIWebsocketTransport(
        websocket,
        params=FastAPIWebsocketParams(
            serializer=serializer,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=8000,
            allowed_origins=[],
        ),
    )

    vad_analyzer = SileroVADAnalyzer()
    vad = VADProcessor(vad_analyzer=vad_analyzer, speech_activity_period=1.5)

    conversation = ConversationManager(
        session_id=session_id,
        tenant_id=tenant_id,
        lead_context=lead_context,
    )

    pipeline = Pipeline(
        processors=[
            transport.input(),
            vad,
            stt,
            conversation,
            tts,
            transport.output(),
        ]
    )

    worker = PipelineWorker(pipeline)
    runner = WorkerRunner()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, websocket):
        logger.info("Twilio stream connected call=%s stream=%s", call_sid, stream_sid)
        await conversation.on_user_joined()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, websocket):
        logger.info("Twilio stream disconnected call=%s", call_sid)
        await runner.end("call ended")

    logger.info("Starting Twilio pipeline session=%s", session_id)
    await runner.run(worker)
    logger.info("Twilio pipeline ended session=%s", session_id)
    await conversation.shutdown()
