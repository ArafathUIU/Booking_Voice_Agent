import json

import aioredis

from app.config import settings

redis = aioredis.from_url(settings.redis_url, decode_responses=True)


async def get_session_context(session_id: str) -> dict:
    data = await redis.get(f"session:{session_id}:context")
    return json.loads(data) if data else {}


async def set_session_context(session_id: str, context: dict):
    await redis.setex(f"session:{session_id}:context", 3600, json.dumps(context))


async def hold_slot_redis(tenant_id: str, slot_id: str, session_id: str, ttl: int = 300):
    key = f"slot:{tenant_id}:{slot_id}"
    return await redis.set(key, session_id, nx=True, ex=ttl)


async def release_slot_redis(tenant_id: str, slot_id: str):
    key = f"slot:{tenant_id}:{slot_id}"
    await redis.delete(key)


async def get_recent_transcript(session_id: str, turns: int = 10) -> list:
    data = await redis.get(f"session:{session_id}:transcript")
    if data:
        return json.loads(data)[-turns:]
    return []


async def cache_transcript_turns(session_id: str, transcript: list):
    await redis.setex(f"session:{session_id}:transcript", 3600, json.dumps(transcript))
