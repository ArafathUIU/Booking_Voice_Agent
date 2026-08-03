import logging

from livekit.api import (
    CreateSIPOutboundTrunkRequest,
    CreateSIPParticipantRequest,
    ListSIPOutboundTrunkRequest,
    LiveKitAPI,
    SIPOutboundTrunkInfo,
)

from app.config import settings

logger = logging.getLogger(__name__)

_TRUNK_NAME = "jervis-twilio-outbound"


def _open_api() -> LiveKitAPI:
    return LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


async def _ensure_outbound_trunk(api: LiveKitAPI) -> str:
    """Return the configured trunk id, creating it once if SIP_TRUNK_ID unset.

    In production you normally create the trunk in the LiveKit Cloud console and
    set SIP_TRUNK_ID. For local dev we can auto-create it from the Twilio trunk
    hostname + credentials configured via SIP_TRUNK_HOSTNAME / _USERNAME / _PASSWORD.
    """
    if settings.sip_trunk_id:
        return settings.sip_trunk_id

    if not settings.sip_trunk_hostname:
        raise RuntimeError(
            "SIP_TRUNK_ID or SIP_TRUNK_HOSTNAME must be set to dial out"
        )

    resp = await api.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
    for trunk in resp.items:
        if trunk.name == _TRUNK_NAME:
            return trunk.sip_trunk_id

    created = await api.sip.create_outbound_trunk(
        CreateSIPOutboundTrunkRequest(
            trunk=SIPOutboundTrunkInfo(
                name=_TRUNK_NAME,
                address=settings.sip_trunk_hostname,
                auth_username=settings.sip_trunk_auth_username,
                auth_password=settings.sip_trunk_auth_password,
            )
        )
    )
    logger.info("Created LiveKit outbound SIP trunk: %s", created.sip_trunk_id)
    return created.sip_trunk_id


async def dial_outbound(
    room_name: str,
    to_number: str,
    ringing_timeout: int | None = None,
    max_call_duration: int | None = None,
    participant_identity: str | None = None,
) -> str:
    """Dial a phone number and bridge it into ``room_name`` via LiveKit SIP.

    Returns the SIP participant id.
    """
    async with _open_api() as api:
        trunk_id = await _ensure_outbound_trunk(api)
        request = CreateSIPParticipantRequest(
            room_name=room_name,
            sip_trunk_id=trunk_id,
            sip_call_to=to_number,
            ringing_timeout=ringing_timeout or settings.sip_ringing_timeout,
            max_call_duration=max_call_duration or settings.sip_max_call_duration,
            participant_identity=participant_identity,
        )
        info = await api.sip.create_sip_participant(request)
        logger.info(
            "SIP dial placed to=%s room=%s sip_participant=%s",
            to_number, room_name, info.participant_id,
        )
        return info.participant_id


async def hangup_sip_participant(
    room_name: str,
    participant_identity: str,
):
    """End a call by removing the SIP participant from the room."""
    async with _open_api() as api:
        try:
            await api.room.remove_participant(
                room=room_name,
                identity=participant_identity,
            )
        except Exception as e:  # noqa: BLE001 - best-effort cleanup
            logger.warning("Failed to remove SIP participant %s: %s", participant_identity, e)