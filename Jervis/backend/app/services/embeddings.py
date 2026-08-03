import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Loaded sentence-transformers model: all-MiniLM-L6-v2")
        except Exception:
            logger.exception("Failed to load sentence-transformers model")
            raise
    return _model


def embed_text(text: str) -> Optional[List[float]]:
    model = _get_model()
    try:
        embedding = model.encode(text, normalize_embeddings=True)
        return embedding.tolist()
    except Exception:
        logger.exception("Failed to embed text")
        return None


def embed_batch(texts: List[str]) -> Optional[List[List[float]]]:
    model = _get_model()
    try:
        embeddings = model.encode(texts, normalize_embeddings=True)
        return [e.tolist() for e in embeddings]
    except Exception:
        logger.exception("Failed to embed batch")
        return None