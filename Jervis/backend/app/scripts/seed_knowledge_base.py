import asyncio
import logging
import sys

from app.db.session import async_session_factory
from app.db.vector_models import KnowledgeChunk, Base
from app.services.knowledge_base import KnowledgeBaseService, KB_CATEGORIES
from app.config import settings
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def init_vector_tables(db):
    await db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    await db.commit()


async def seed_kb():
    async with async_session_factory() as db:
        await init_vector_tables(db)
        await db.commit()

        tenant_id = settings.tenant_id
        svc = KnowledgeBaseService(db, tenant_id)

        total = 0
        for category, items in KB_CATEGORIES.items():
            for item in items:
                await svc.add_chunk(
                    category=category,
                    title=item["title"],
                    content=item["content"],
                )
                total += 1

        await db.commit()
        logger.info("Knowledge base seeded with %d chunks across %d categories", total, len(KB_CATEGORIES))
        return total


async def list_chunks():
    async with async_session_factory() as db:
        from sqlalchemy import select
        from app.db.vector_models import KnowledgeChunk

        stmt = select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == settings.tenant_id)
        results = await db.execute(stmt)
        chunks = list(results.scalars().all())
        logger.info("Found %d chunks in knowledge base", len(chunks))
        for chunk in chunks:
            logger.info("  [%s] %s: %s...", chunk.category, chunk.title, chunk.content[:80])


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        await list_chunks()
    else:
        await seed_kb()


if __name__ == "__main__":
    asyncio.run(main())