import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import text

from app.agents.model_warmup import warm_up_models
from app.api.v1.calls import router as calls_router
from app.api.v1.leads import router as leads_router
from app.api.v1.twilio_ws import router as twilio_ws_router
from app.api.v1.webhooks import router as webhooks_router
from app.config import settings
from app.db.base import Base
from app.db.session import engine, async_session_factory
from app.models.ai_config import AIConfig
from app.models.booking import Booking
from app.models.voice_config import VoiceConfig
from app.websockets import manager as ws_manager


def configure_logging():
    """Make app.* logs visible. The pipecat logger uses its own handler/level, so
    without this, ConversationManager user turns, tool calls and LLM failures are
    silently suppressed and the bot looks like it hangs for no reason."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Levels alone do nothing without a handler: INFO records from app.* were
    # dropped (only WARNING+ leaked via Python's lastResort handler). Attach a
    # stderr handler so user turns, tool calls and LLM round-trips show up in
    # docker compose logs.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)
    for name, level in {
        "app": logging.INFO,
        "app.agents": logging.DEBUG,
        "app.agents.conversation_manager": logging.DEBUG,
    }.items():
        logging.getLogger(name).setLevel(level)


configure_logging()


async def init_db():
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)


async def seed_test_data():
    async with async_session_factory() as session:
        tid = uuid.UUID(settings.tenant_id)

        existing = await session.get(AIConfig, tid)
        if not existing:
            existing = await session.execute(
                text("SELECT id FROM ai_configs WHERE tenant_id = :tid"),
                {"tid": tid},
            )
            existing = existing.scalar_one_or_none()

        if not existing:
            ai = AIConfig(
                id=tid,
                tenant_id=tid,
                system_prompt=(
                    "You are Clara, a friendly and professional booking assistant for a dental clinic. "
                    "Your job is to help customers book appointments, answer questions about services, "
                    "hours, location, and pricing. Be concise, warm, and efficient. "
                    "Always confirm details before booking."
                ),
                llm_provider="groq",
                llm_model=settings.groq_model,
                stt_provider="whisper",
                tts_provider="kokoro",
                temperature=0.7,
                max_tokens=256,
                provider_config={},
            )
            session.add(ai)

        existing_v = await session.get(VoiceConfig, tid)
        if not existing_v:
            existing_v = await session.execute(
                text("SELECT id FROM voice_configs WHERE tenant_id = :tid"),
                {"tid": tid},
            )
            existing_v = existing_v.scalar_one_or_none()

        if not existing_v:
            vc = VoiceConfig(
                id=tid,
                tenant_id=tid,
                provider="kokoro",
                voice_id="af_heart",
                language="en-US",
                stability=0.5,
                similarity=0.75,
                speaking_rate=1.0,
            )
            session.add(vc)

        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_test_data()

    # Outbound lead caller: periodically claims pending leads and dials them via
    # LiveKit SIP. Non-fatal by design; disable with OUTBOUND_CALLER=false.
    _poller_stop = asyncio.Event()
    _poller_task = None
    if settings.outbound_caller:
        from app.scheduler.jobs import outbound_poller

        _poller_task = asyncio.create_task(outbound_poller(_poller_stop))

    # Pre-download/load the on-device Whisper + Kokoro models in the background so
    # the first live call is not stalled (and doesn't silently fail) on the
    # first-run download. Non-fatal by design: failures are logged, not fatal to
    # startup. Disable with AUTO_WARMUP=false.
    if settings.auto_warmup:
        asyncio.create_task(warm_up_models())

    try:
        yield
    finally:
        if _poller_task:
            _poller_stop.set()
            _poller_task.cancel()
            try:
                await _poller_task
            except asyncio.CancelledError:
                pass
        await engine.dispose()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(calls_router)
app.include_router(leads_router)
app.include_router(twilio_ws_router)
app.include_router(webhooks_router)


@app.websocket("/ws/transcript/{session_id}")
async def ws_transcript(websocket: WebSocket, session_id: str):
    await ws_manager.connect(session_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(session_id)


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
async def index():
    resp = FileResponse(STATIC_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.ico")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name}
