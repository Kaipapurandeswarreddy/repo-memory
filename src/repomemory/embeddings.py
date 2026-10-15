"""Optional embeddings (local-first, TF fallback).

- Default: TF scoring (zero deps, keyless like agentmemory).
- Opt-in: EMBEDDING_PROVIDER=local + `sentence-transformers` installed ->
  MiniLM cosine blended 0.4 with TF.
Never raises when the model is missing; falls back silently.
"""

from __future__ import annotations

import math
import os
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def tf_cosine(a: str, b: str) -> float:
    """TF cosine fallback (0..1)."""
    from collections import Counter
    ca, cb = Counter(_tokens(a)), Counter(_tokens(b))
    if not ca or not cb:
        return 0.0
    dot = sum(ca[t] * cb.get(t, 0) for t in ca)
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


_model = None


def _local_model():
    global _model
    if _model is not None:
        return _model
    if os.environ.get("EMBEDDING_PROVIDER", "").lower() != "local":
        return None
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        return _model
    except Exception:
        return None


def similarity(query: str, doc: str) -> float:
    """Embedding cosine when available, else TF cosine."""
    model = _local_model()
    if model is None:
        return tf_cosine(query, doc)
    try:
        import numpy as np  # type: ignore
        eq, ed = model.encode([query, doc], normalize_embeddings=True)
        return float(np.dot(eq, ed))
    except Exception:
        return tf_cosine(query, doc)


__all__ = ["similarity", "tf_cosine"]
