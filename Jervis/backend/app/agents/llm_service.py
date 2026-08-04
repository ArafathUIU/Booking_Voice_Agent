"""Natural-language generation service.

The LLM is used for *free-form* turns only (small talk, thanks, open questions).
Booking policy, slot extraction, tool execution, and confirmation wording are all
deterministic — the LLM never sees tools and never decides what happens next.

Context is compact: the system prompt plus a short internal "state" block plus
the recent clean dialogue window. No internal messages (timeouts, retries, tool
results) ever enter the prompt history.

RAG: for general questions, relevant knowledge chunks are retrieved and injected
into the system context so answers stay accurate and clinic-specific.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI, RateLimitError

from app.agents.constants import SYSTEM_PROMPT
from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str = ""
    timed_out: bool = False
    error: str = ""


def _trim_to_last_sentence(text: str) -> str:
    """Cut a truncated reply back to its last complete sentence."""
    if not text:
        return text
    matches = list(re.finditer(r"[.!?](?:\s|$)", text))
    if not matches:
        return text
    end = matches[-1].end()
    return text[:end].strip()


async def _build_rag_context(state) -> str:
    """Retrieve relevant knowledge chunks and return as context string."""
    try:
        from app.db.session import async_session_factory
        from app.db.vector_models import KnowledgeChunk
        from app.services.knowledge_base import KnowledgeBaseService

        query = state.last_asr_text or ""
        if not query:
            return ""

        async with async_session_factory() as db:
            svc = KnowledgeBaseService(db, settings.tenant_id)
            chunks = await svc.search(query, top_k=3)
            if not chunks:
                return ""
            parts = []
            for chunk in chunks:
                parts.append(f"[{chunk.title}] {chunk.content}")
            return "\n\n".join(parts)
    except Exception:
        logger.debug("RAG retrieval failed, continuing without context", exc_info=True)
        return ""


class LLMService:
    def __init__(self):
        self._client = AsyncOpenAI(
            api_key=settings.groq_api_key or "gsk_placeholder",
            base_url="https://api.groq.com/openai/v1",
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )

    async def build_messages(self, state, user_text: str) -> list:
        internal = [
            "[Internal state — never read aloud]",
            f"Stage: {state.stage}",
            f"Known details: {state.slot_summary()}",
            f"Last question asked: {state.pending_question or 'none'}",
            "Reply in short plain spoken English.",
            "Always put a normal space after every period, comma, and question "
            "mark, exactly like a human typing a text message. Never write two "
            "sentences touching each other with no space (wrong: "
            "\"Sure.You'd like\" — right: \"Sure. You'd like\").",
        ]

        rag_context = await _build_rag_context(state)
        if rag_context:
            internal.append(
                "Use the following clinic information to answer the caller's question. "
                "If the question is about booking, defer to the system. "
                f"Clinic info: {rag_context}"
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + "\n".join(internal)},
        ]
        messages.extend(state.recent_history(settings.llm_history_turns))
        messages.append({"role": "user", "content": user_text})
        return messages

    async def generate(self, state, user_text: str) -> LLMResult:
        messages = await self.build_messages(state, user_text)
        try:
            async with asyncio.timeout(settings.llm_timeout_s):
                response = await self._client.chat.completions.create(
                    model=settings.groq_model,
                    messages=messages,
                    temperature=settings.llm_temperature,
                    max_tokens=settings.llm_max_tokens,
                )
            choice = response.choices[0]
            text = (choice.message.content or "").strip()
            if choice.finish_reason == "length":
                text = _trim_to_last_sentence(text)
            return LLMResult(text=text)
        except RateLimitError as e:
            logger.warning("Groq rate limited (429): %s", e)
            return LLMResult(timed_out=True, error="rate_limit")
        except TimeoutError:
            logger.warning("Groq LLM call timed out after %ss", settings.llm_timeout_s)
            return LLMResult(timed_out=True, error="timeout")
        except Exception:
            logger.exception("LLM call failed")
            return LLMResult(timed_out=True, error="error")
