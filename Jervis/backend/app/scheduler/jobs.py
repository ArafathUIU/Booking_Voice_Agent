import asyncio
import logging

from app.config import settings
from app.db.session import async_session_factory
from app.services.lead_service import LeadService

logger = logging.getLogger(__name__)


async def outbound_poller(stop: asyncio.Event):
    """Background loop: find due pending leads and hand them to the dialer.

    The actual claim (pending -> dialing) happens atomically inside
    ``start_outbound_call``, so even though this loop keeps running while a call
    is in flight, no lead can ever be dialed twice (the manual /outbound
    endpoint races this loop through the same claim).
    """
    logger.info(
        "Outbound poller started (interval=%ss, max_attempts=%s)",
        settings.outbound_poll_interval,
        settings.outbound_max_attempts,
    )
    while not stop.is_set():
        try:
            lead_id = await find_next_pending()
            if lead_id:
                from app.services.outbound_service import start_outbound_call

                logger.info("Poller found pending lead %s, starting outbound call", lead_id)
                asyncio.create_task(start_outbound_call(lead_id))
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            logger.exception("Outbound poller iteration failed")

        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.outbound_poll_interval
            )
        except asyncio.TimeoutError:
            pass
    logger.info("Outbound poller stopped")


async def find_next_pending() -> str | None:
    async with async_session_factory() as db:
        svc = LeadService(db)
        lead = await svc.find_pending(settings.tenant_id)
        await db.commit()
        return lead["id"] if lead else None
