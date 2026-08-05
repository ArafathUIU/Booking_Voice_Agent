import logging

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.transports.livekit.transport import LiveKitTransport, LiveKitParams
from pipecat.workers.runner import WorkerRunner, PipelineWorker

from app.agents.conversation_manager import ConversationManager
from app.agents.livekit_audio_patch import apply_livekit_audio_patch
from app.agents.model_warmup import (
    create_stt_service,
    create_tts_service,
    warm_up_stt,
    warm_up_tts,
)
from app.config import settings

logger = logging.getLogger(__name__)


async def build_and_run_pipeline(
    room_name: str,
    agent_token: str,
    session_id: str,
    tenant_id: str,
    ws_url: str = "ws://localhost:7880",
    lead_context: dict | None = None,
):
    apply_livekit_audio_patch()

    logger.info(
        "Loading STT model: model=%s device=%s compute_type=%s",
        settings.stt_model, settings.stt_device, settings.stt_compute_type,
    )
    try:
        warm_up_stt()
        stt = create_stt_service()
    except Exception as e:
        logger.exception("Failed to load Whisper STT model '%s'", settings.stt_model)
        logger.error(
            "This model is downloaded from HuggingFace on first use. Check network "
            "access, or set STT_MODEL to a smaller model (e.g. tiny) to reduce "
            "the download/RAM, then restart."
        )
        raise

    logger.info("Loading TTS: voice=%s", settings.tts_voice)
    try:
        warm_up_tts()
        tts = create_tts_service()
    except Exception as e:
        logger.exception("Failed to load Kokoro TTS (voice=%s)", settings.tts_voice)
        logger.error(
            "Kokoro downloads ~300MB from GitHub on first use into "
            "~/.cache/pipecat/kokoro-onnx. Check network access or pre-download with "
            "backend/scripts/warmup_models.py, then restart."
        )
        raise

    transport = LiveKitTransport(
        url=ws_url,
        token=agent_token,
        room_name=room_name,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=True,
            audio_out_sample_rate=24000,
        ),
    )

    vad_analyzer = SileroVADAnalyzer()
    # speech_activity_period: how long speech must stop before we treat the
    # utterance as finished. Too short and a normal thinking-pause (recalling
    # a date, spelling a name) gets read as "done" -> the bot jumps in and
    # re-asks the question on top of the caller, sometimes more than once.
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

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant_id):
        logger.info("User joined room=%s participant=%s", room_name, participant_id)
        await conversation.on_user_joined()

    runner = WorkerRunner()
    logger.info("Starting pipeline for room=%s", room_name)
    await runner.run(worker)
    logger.info("Pipeline ended for room=%s", room_name)
    await conversation.shutdown()