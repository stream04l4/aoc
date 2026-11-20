"""Lightweight deterministic OpenAI-compatible embeddings for self-hosted Letta pgvector.

Provides L2-normalized 1024-d feature-hashed embeddings (word unigrams + bigrams +
character 3-grams) with <1ms latency and zero extra GPU/RAM overhead. Can be replaced
at any time by setting `memory.embedding_endpoint` to any OpenAI-compatible embedding server.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable

_WORD_RE = re.compile(r"[a-zA-Z0-9_\u4e00-\u9fff]+")


def embed_text(text: str, dim: int = 1024) -> list[float]:
    vec = [0.0] * dim
    norm_text = (text or "").lower().strip()
    if not norm_text:
        vec[0] = 1.0
        return vec

    words = _WORD_RE.findall(norm_text)
    features: list[tuple[str, float]] = []
    for w in words:
        features.append((f"w:{w}", 1.5))
        if len(w) >= 3:
            for i in range(len(w) - 2):
                features.append((f"c:{w[i:i+3]}", 0.5))
    for i in range(len(words) - 1):
        features.append((f"b:{words[i]}_{words[i+1]}", 1.0))

    if not features:
        features.append((f"raw:{norm_text[:64]}", 1.0))

    for feat, weight in features:
        h = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if (h[4] & 1) == 0 else -1.0
        vec[idx] += sign * weight

    mag = math.sqrt(sum(v * v for v in vec))
    if mag > 0:
        vec = [v / mag for v in vec]
    else:
        vec[0] = 1.0
    return vec


def embed_batch(texts: Iterable[str], dim: int = 1024) -> list[list[float]]:
    return [embed_text(t, dim=dim) for t in texts]
