import asyncio
import logging

from app.agents.pipeline import build_and_run_pipeline

logger = logging.getLogger(__name__)


async def run_agent_worker(
    room_name: str,
    agent_token: str,
    session_id: str,
    tenant_id: str,
    ws_url: str = "ws://localhost:7880",
    lead_context: dict | None = None,
):
    logger.info(
        "Agent worker starting: room=%s session=%s tenant=%s",
        room_name,
        session_id,
        tenant_id,
    )
    try:
        await build_and_run_pipeline(
            room_name=room_name,
            agent_token=agent_token,
            session_id=session_id,
            tenant_id=tenant_id,
            ws_url=ws_url,
            lead_context=lead_context,
        )
    except asyncio.CancelledError:
        logger.info("Agent worker cancelled: %s", session_id)
    except Exception as e:
        logger.exception("Agent worker error: %s", e)
