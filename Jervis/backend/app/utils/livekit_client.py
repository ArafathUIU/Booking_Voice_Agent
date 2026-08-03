from uuid import uuid4

from livekit.api import (
    LiveKitAPI,
    CreateRoomRequest,
    AccessToken,
    VideoGrants,
)

from app.config import settings


async def create_room() -> tuple[str, str, str]:
    print("LiveKit URL:", settings.livekit_url)
    print("API Key:", settings.livekit_api_key)

    room_name = f"call-{uuid4().hex[:8]}"

    async with LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    ) as api:
        await api.room.create_room(
            CreateRoomRequest(name=room_name)
        )

    browser_token = generate_token(room_name, "user", "Customer")
    agent_token = generate_token(room_name, "agent", "AI Assistant")

    return room_name, browser_token, agent_token

def generate_token(
    room_name: str,
    identity: str,
    name: str,
) -> str:

    token = (
        AccessToken(
            settings.livekit_api_key,
            settings.livekit_api_secret,
        )
        .with_identity(identity)
        .with_name(name)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
    )

    return token.to_jwt()