import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.services.call_service import CallService
from app.utils.livekit_client import create_room

router = APIRouter(prefix="/api/v1/calls", tags=["calls"])

logger = logging.getLogger(__name__)

# Track the most recent agent task per tenant so a page reload (new room) cancels
# the previous agent instead of leaving several bots running until idle timeout.
_active_agent_tasks: dict[str, asyncio.Task] = {}


class RoomRequest(BaseModel):
    customer_name: str | None = None
    customer_phone: str | None = None


class RoomResponse(BaseModel):
    session_id: str
    room_name: str
    browser_token: str
    agent_token: str
    ws_url: str


class OutboundRequest(BaseModel):
    phone: str
    customer_name: str | None = None
    purpose: str | None = None


class OutboundResponse(BaseModel):
    lead_id: str
    status: str


@router.post("/outbound", response_model=OutboundResponse)
async def trigger_outbound_call(
    body: OutboundRequest,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Manually queue and immediately start an outbound call for a lead.

    Creates the lead from the request, then hands it to the same orchestrator the
    scheduler uses. Returns as soon as the call is queued; the actual dial
    (agent-join -> SIP dial -> ringing wait) runs in the background.
    """
    from app.services.lead_service import LeadService

    phone = body.phone.strip()
    if not phone:
        raise HTTPException(status_code=422, detail="phone is required")

    svc = LeadService(db)
    lead = await svc.create_lead(
        tenant_id=settings.tenant_id,
        customer_name=body.customer_name,
        phone=phone,
        purpose=body.purpose,
    )
    await db.commit()
    lead_id = str(lead.id)

    bg.add_task(_run_outbound, lead_id=lead_id)
    return OutboundResponse(lead_id=lead_id, status="queued")


async def _run_outbound(lead_id: str):
    from app.services.outbound_service import start_outbound_call

    try:
        result = await start_outbound_call(lead_id)
        logger.info("Outbound call result for lead %s: %s", lead_id, result)
    except Exception:  # noqa: BLE001
        logger.exception("Outbound call failed for lead %s", lead_id)


@router.post("/room", response_model=RoomResponse)
async def create_call_room(
    body: RoomRequest,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    room_name, browser_token, agent_token = await create_room()

    svc = CallService(db)
    session = await svc.create_session(
        tenant_id=settings.tenant_id,
        room_name=room_name,
        customer_name=body.customer_name,
        customer_phone=body.customer_phone,
    )

    # Commit here, BEFORE scheduling the agent. FastAPI runs BackgroundTasks
    # before the `get_db` dependency teardown commits, so the INSERT would stay
    # uncommitted (invisible) for the entire call otherwise — which is why
    # per-turn persistence and the final `ended` status never took effect.
    await db.commit()

    # Cancel the previous agent for this tenant (e.g. an old page reload) so we
    # never run multiple bots in parallel talking over each other.
    previous = _active_agent_tasks.get(settings.tenant_id)
    if previous and not previous.done():
        logger.info(
            "Cancelling previous agent task %s for tenant %s",
            previous.get_name() if hasattr(previous, "get_name") else id(previous),
            settings.tenant_id,
        )
        previous.cancel()

    bg.add_task(
        _spawn_agent_worker,
        room_name=room_name,
        agent_token=agent_token,
        session_id=str(session.id),
        tenant_id=settings.tenant_id,
        ws_url=settings.livekit_url,
    )

    return RoomResponse(
        session_id=str(session.id),
        room_name=room_name,
        browser_token=browser_token,
        agent_token=agent_token,
        ws_url=settings.livekit_public_url,
    )


async def _spawn_agent_worker(
    room_name: str,
    agent_token: str,
    session_id: str,
    tenant_id: str,
    ws_url: str = "ws://localhost:7880",
):
    from app.agents.worker import run_agent_worker

    # Record this task so a newer room can cancel it if the page is reloaded.
    _active_agent_tasks[tenant_id] = asyncio.current_task()
    try:
        await run_agent_worker(
            room_name=room_name,
            agent_token=agent_token,
            session_id=session_id,
            tenant_id=tenant_id,
            ws_url=ws_url,
        )
    finally:
        if _active_agent_tasks.get(tenant_id) is asyncio.current_task():
            _active_agent_tasks.pop(tenant_id, None)
