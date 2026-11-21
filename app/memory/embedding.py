"""Switchable EmbeddingInterface with local ONNX, remote OpenAI-compatible, and hash fallback providers (V2.0 P1-4).

Design guarantees:
  - Strict model version + dimension tracking via ``embedding_model_registry``.
  - Cross-dimension mixing is hard-forbidden (raises ``EmbeddingDimensionMismatchError``).
  - Pure ``onnxruntime`` + ``tokenizers`` + ``numpy`` (no PyTorch) keeps resident RSS well under 600MB.
  - Provider outages raise ``EmbeddingError`` so callers degrade cleanly without crashing AutonomousLoop.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Sequence

import httpx
import numpy as np
from pydantic import BaseModel

from app.core.config import EmbeddingSettings
from app.memory.local_embeddings import embed_batch as hash_embed_batch
from app.observability.logging import get_logger
from app.storage.repository import StateRepository

logger = get_logger("aoc.memory.embedding")


class EmbeddingError(RuntimeError):
    """Raised when an embedding backend fails or times out."""


class EmbeddingDimensionMismatchError(ValueError):
    """Raised when vectors of incompatible dimensions or models are mixed."""


class EmbeddingModelSpec(BaseModel):
    model_key: str
    provider: str  # local_onnx | remote_openai | hash_fallback
    model_name: str
    model_version: str
    dimensions: int
    normalized: bool = True


class EmbeddingBatchResult(BaseModel):
    model_key: str
    dimensions: int
    vectors: list[list[float]]
    latency_ms: float


# Curated HuggingFace ONNX repositories for local lightweight models (Decision 3 / Task Book 5.4.1)
ONNX_MODEL_CANDIDATES: dict[str, dict[str, Any]] = {
    "intfloat/multilingual-e5-small:onnx-qint8": {
        "repo_id": "Xenova/multilingual-e5-small",
        "onnx_file": "onnx/model_quantized.onnx",
        "tokenizer_file": "tokenizer.json",
        "dimensions": 384,
        "pooling": "mean",
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
    "intfloat/multilingual-e5-small:onnx-fp32": {
        "repo_id": "Xenova/multilingual-e5-small",
        "onnx_file": "onnx/model.onnx",
        "tokenizer_file": "tokenizer.json",
        "dimensions": 384,
        "pooling": "mean",
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
    "BAAI/bge-small-zh-v1.5:onnx-qint8": {
        "repo_id": "Xenova/bge-small-zh-v1.5",
        "onnx_file": "onnx/model_quantized.onnx",
        "tokenizer_file": "tokenizer.json",
        "dimensions": 512,
        "pooling": "cls",
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
        "passage_prefix": "",
    },
    "BAAI/bge-small-zh-v1.5:onnx-fp32": {
        "repo_id": "Xenova/bge-small-zh-v1.5",
        "onnx_file": "onnx/model.onnx",
        "tokenizer_file": "tokenizer.json",
        "dimensions": 512,
        "pooling": "cls",
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
        "passage_prefix": "",
    },
    "BAAI/bge-m3:onnx-qint8": {
        "repo_id": "Xenova/bge-m3",
        "onnx_file": "onnx/model_quantized.onnx",
        "tokenizer_file": "tokenizer.json",
        "dimensions": 1024,
        "pooling": "cls",
        "query_prefix": "",
        "passage_prefix": "",
    },
}


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Strict dimension-checked cosine similarity in [-1.0, 1.0]."""
    if len(a) != len(b):
        raise EmbeddingDimensionMismatchError(
            f"Cannot compute cosine similarity between mismatched dimensions: {len(a)} vs {len(b)}"
        )
    if not a:
        return 0.0
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    sim = float(np.dot(va, vb) / (na * nb))
    if math.isnan(sim) or math.isinf(sim):
        return 0.0
    return max(-1.0, min(1.0, sim))


def l2_normalize(arr: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(arr, axis=-1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return arr / norms


class EmbeddingInterface(ABC):
    @property
    @abstractmethod
    def spec(self) -> EmbeddingModelSpec: ...

    @abstractmethod
    async def embed_texts(self, texts: list[str], *, is_query: bool = False) -> EmbeddingBatchResult: ...

    @abstractmethod
    async def health(self) -> bool: ...

    async def close(self) -> None:
        return None


class DeterministicHashEmbeddingProvider(EmbeddingInterface):
    """Zero-dependency deterministic hash provider for fast offline unit tests."""

    def __init__(
        self,
        *,
        model_key: str = "aoc-hash-embed:v1",
        dimensions: int = 128,
    ) -> None:
        self._spec = EmbeddingModelSpec(
            model_key=model_key,
            provider="hash_fallback",
            model_name=model_key.split(":")[0],
            model_version=model_key.split(":")[1] if ":" in model_key else "v1",
            dimensions=dimensions,
            normalized=True,
        )

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    async def embed_texts(self, texts: list[str], *, is_query: bool = False) -> EmbeddingBatchResult:
        t0 = time.perf_counter()
        vecs = hash_embed_batch(texts, dim=self._spec.dimensions)
        return EmbeddingBatchResult(
            model_key=self._spec.model_key,
            dimensions=self._spec.dimensions,
            vectors=vecs,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
        )

    async def health(self) -> bool:
        return True


class RemoteOpenAIEmbeddingProvider(EmbeddingInterface):
    """OpenAI-compatible /v1/embeddings client (supports remote BAAI/bge-m3 or vLLM embedding server)."""

    def __init__(
        self,
        *,
        endpoint: str,
        model_name: str,
        dimensions: int,
        model_key: str | None = None,
        model_version: str = "remote",
        api_key: str = "",
        timeout_seconds: float = 15.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._spec = EmbeddingModelSpec(
            model_key=model_key or f"{model_name}:{model_version}",
            provider="remote_openai",
            model_name=model_name,
            model_version=model_version,
            dimensions=dimensions,
            normalized=True,
        )

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def embed_texts(self, texts: list[str], *, is_query: bool = False) -> EmbeddingBatchResult:
        if not texts:
            return EmbeddingBatchResult(
                model_key=self._spec.model_key, dimensions=self._spec.dimensions, vectors=[], latency_ms=0.0
            )
        t0 = time.perf_counter()
        url = f"{self.endpoint}/embeddings"
        payload = {"model": self._spec.model_name, "input": texts}
        try:
            if self._http_client is not None:
                resp = await self._http_client.post(url, json=payload, headers=self._headers(), timeout=self.timeout_seconds)
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            body = resp.json()
            data = sorted(body.get("data", []), key=lambda item: int(item.get("index", 0)))
            vectors: list[list[float]] = []
            for row in data:
                vec = [float(x) for x in row["embedding"]]
                if len(vec) != self._spec.dimensions:
                    raise EmbeddingDimensionMismatchError(
                        f"Remote embedding dim {len(vec)} != expected {self._spec.dimensions}"
                    )
                arr = l2_normalize(np.asarray(vec, dtype=np.float32))
                vectors.append(arr.tolist())
            if len(vectors) != len(texts):
                raise EmbeddingError(f"Expected {len(texts)} embeddings, received {len(vectors)}")
            return EmbeddingBatchResult(
                model_key=self._spec.model_key,
                dimensions=self._spec.dimensions,
                vectors=vectors,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )
        except EmbeddingDimensionMismatchError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Remote embedding request failed ({url}): {exc}") from exc

    async def health(self) -> bool:
        try:
            res = await self.embed_texts(["health check"], is_query=True)
            return len(res.vectors) == 1 and len(res.vectors[0]) == self._spec.dimensions
        except Exception:
            return False


class LocalOnnxEmbeddingProvider(EmbeddingInterface):
    """Lightweight CPU ONNX neural embedding provider (pure onnxruntime + tokenizers, <600MB RSS)."""

    def __init__(
        self,
        *,
        model_key: str = "intfloat/multilingual-e5-small:onnx-qint8",
        model_name: str = "intfloat/multilingual-e5-small",
        model_version: str = "onnx-qint8",
        dimensions: int = 384,
        cache_dir: str = "/home/ubuntu/.cache/aoc-embeddings",
        max_length: int = 256,
        onnx_threads: int = 2,
        repo_id: str | None = None,
        onnx_file: str | None = None,
        tokenizer_file: str | None = None,
        pooling: str | None = None,
        query_prefix: str | None = None,
        passage_prefix: str | None = None,
    ) -> None:
        preset = ONNX_MODEL_CANDIDATES.get(model_key, {})
        self.repo_id = repo_id or str(preset.get("repo_id") or model_name)
        self.onnx_file = onnx_file or str(preset.get("onnx_file") or "onnx/model_quantized.onnx")
        self.tokenizer_file = tokenizer_file or str(preset.get("tokenizer_file") or "tokenizer.json")
        self.pooling = pooling or str(preset.get("pooling") or "mean")
        self.query_prefix = query_prefix if query_prefix is not None else str(preset.get("query_prefix", ""))
        self.passage_prefix = passage_prefix if passage_prefix is not None else str(preset.get("passage_prefix", ""))
        self.cache_dir = Path(cache_dir)
        self.max_length = max(32, int(max_length))
        self.onnx_threads = max(1, int(onnx_threads))
        eff_dim = int(dimensions or preset.get("dimensions", 384))
        self._spec = EmbeddingModelSpec(
            model_key=model_key,
            provider="local_onnx",
            model_name=model_name,
            model_version=model_version,
            dimensions=eff_dim,
            normalized=True,
        )
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: set[str] = set()
        self._load_lock = asyncio.Lock()

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    def _ensure_loaded_sync(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise EmbeddingError(f"ONNX embedding dependencies missing: {exc}") from exc

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            try:
                tok_path = hf_hub_download(
                    repo_id=self.repo_id,
                    filename=self.tokenizer_file,
                    cache_dir=str(self.cache_dir),
                    local_files_only=True,
                )
                onnx_path = hf_hub_download(
                    repo_id=self.repo_id,
                    filename=self.onnx_file,
                    cache_dir=str(self.cache_dir),
                    local_files_only=True,
                )
            except Exception:
                tok_path = hf_hub_download(
                    repo_id=self.repo_id,
                    filename=self.tokenizer_file,
                    cache_dir=str(self.cache_dir),
                )
                onnx_path = hf_hub_download(
                    repo_id=self.repo_id,
                    filename=self.onnx_file,
                    cache_dir=str(self.cache_dir),
                )
        except Exception as exc:
            raise EmbeddingError(f"Failed to download/load ONNX model '{self.repo_id}': {exc}") from exc

        tok = Tokenizer.from_file(tok_path)
        tok.enable_truncation(max_length=self.max_length)
        tok.enable_padding(length=None)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.onnx_threads
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        sess = ort.InferenceSession(onnx_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self._input_names = {inp.name for inp in sess.get_inputs()}
        self._tokenizer = tok
        self._session = sess
        logger.info(
            "embedding.onnx_loaded",
            model_key=self._spec.model_key,
            repo_id=self.repo_id,
            dimensions=self._spec.dimensions,
            pooling=self.pooling,
        )

    def _embed_sync(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        self._ensure_loaded_sync()
        prefix = self.query_prefix if is_query else self.passage_prefix
        prepared = [f"{prefix}{(t or '').strip()}" if prefix else (t or "").strip() for t in texts]
        prepared = [p if p else " " for p in prepared]

        encodings = self._tokenizer.encode_batch(prepared)
        input_ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)

        feeds: dict[str, np.ndarray] = {}
        if "input_ids" in self._input_names:
            feeds["input_ids"] = input_ids
        if "attention_mask" in self._input_names:
            feeds["attention_mask"] = attention_mask
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.asarray([e.type_ids for e in encodings], dtype=np.int64)

        outputs = self._session.run(None, feeds)
        hidden = np.asarray(outputs[0], dtype=np.float32)

        if hidden.ndim == 2:
            pooled = hidden
        elif self.pooling == "cls":
            pooled = hidden[:, 0, :]
        else:
            # Mean pooling over non-padding tokens
            mask_exp = np.expand_dims(attention_mask.astype(np.float32), axis=-1)
            summed = np.sum(hidden * mask_exp, axis=1)
            counts = np.clip(np.sum(mask_exp, axis=1), a_min=1e-9, a_max=None)
            pooled = summed / counts

        if pooled.shape[-1] != self._spec.dimensions:
            raise EmbeddingDimensionMismatchError(
                f"ONNX model output dim {pooled.shape[-1]} != configured {self._spec.dimensions}"
            )
        normed = l2_normalize(pooled)
        return [row.tolist() for row in normed]

    async def embed_texts(self, texts: list[str], *, is_query: bool = False) -> EmbeddingBatchResult:
        if not texts:
            return EmbeddingBatchResult(
                model_key=self._spec.model_key, dimensions=self._spec.dimensions, vectors=[], latency_ms=0.0
            )
        t0 = time.perf_counter()
        if self._session is None:
            async with self._load_lock:
                await asyncio.to_thread(self._ensure_loaded_sync)
        vecs = await asyncio.to_thread(self._embed_sync, texts, is_query=is_query)
        return EmbeddingBatchResult(
            model_key=self._spec.model_key,
            dimensions=self._spec.dimensions,
            vectors=vecs,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
        )

    async def health(self) -> bool:
        try:
            res = await self.embed_texts(["health probe"], is_query=True)
            return len(res.vectors) == 1 and len(res.vectors[0]) == self._spec.dimensions
        except Exception as exc:
            logger.warning("embedding.onnx_health_failed", model_key=self._spec.model_key, error=str(exc))
            return False


def build_embedding_provider(cfg: EmbeddingSettings) -> EmbeddingInterface:
    prov = (cfg.provider or "local_onnx").strip().lower()
    if prov == "remote_openai":
        return RemoteOpenAIEmbeddingProvider(
            endpoint=cfg.remote_endpoint,
            model_name=cfg.remote_model,
            dimensions=cfg.dimensions,
            model_key=cfg.model_key or f"{cfg.remote_model}:remote",
            model_version=cfg.model_version or "remote",
            api_key=cfg.remote_api_key.get_secret_value() if cfg.remote_api_key else "",
            timeout_seconds=cfg.timeout_seconds,
        )
    if prov == "hash_fallback":
        return DeterministicHashEmbeddingProvider(
            model_key=cfg.model_key or f"aoc-hash-{cfg.dimensions}:v1",
            dimensions=cfg.dimensions,
        )
    return LocalOnnxEmbeddingProvider(
        model_key=cfg.model_key,
        model_name=cfg.model_name,
        model_version=cfg.model_version,
        dimensions=cfg.dimensions,
        cache_dir=cfg.cache_dir,
        max_length=cfg.max_length,
        onnx_threads=cfg.onnx_threads,
        query_prefix=cfg.query_prefix,
        passage_prefix=cfg.passage_prefix,
    )


class EmbeddingService:
    """Coordinates active EmbeddingInterface, embedding_model_registry, and memory_embeddings."""

    def __init__(self, repo: StateRepository, provider: EmbeddingInterface) -> None:
        self.repo = repo
        self.provider = provider
        self._active_model_id: uuid.UUID | None = None
        self._healthy: bool = True

    @property
    def active_model_id(self) -> uuid.UUID | None:
        return self._active_model_id

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self.provider.spec

    async def ensure_registered(self) -> uuid.UUID:
        sp = self.provider.spec
        row = await self.repo.upsert_embedding_model(
            model_key=sp.model_key,
            provider=sp.provider,
            model_name=sp.model_name,
            model_version=sp.model_version,
            dimensions=sp.dimensions,
            normalized=sp.normalized,
            activate=True,
        )
        self._active_model_id = uuid.UUID(row["id"])
        return self._active_model_id

    async def switch_provider(self, new_provider: EmbeddingInterface) -> uuid.UUID:
        """Switch active embedding model cleanly; existing embeddings under old model_id remain intact."""
        self.provider = new_provider
        self._active_model_id = None
        return await self.ensure_registered()

    async def embed_query(self, query: str) -> tuple[uuid.UUID, list[float]]:
        model_id = self._active_model_id or await self.ensure_registered()
        try:
            res = await self.provider.embed_texts([query], is_query=True)
            self._healthy = True
            return model_id, res.vectors[0]
        except Exception as exc:
            self._healthy = False
            raise EmbeddingError(str(exc)) from exc

    async def embed_memory_record(self, memory_id: uuid.UUID) -> dict[str, Any]:
        """Compute and persist embedding for one memory_index row under the active model_id."""
        row = await self.repo.get_memory_by_id(memory_id)
        if row is None:
            return {"memory_id": str(memory_id), "status": "not_found"}

        model_id = self._active_model_id or await self.ensure_registered()
        text_to_embed = f"{row['title']}\n{row['summary']}".strip()
        try:
            res = await self.provider.embed_texts([text_to_embed], is_query=False)
            vec = res.vectors[0]
            if len(vec) != self.provider.spec.dimensions:
                raise EmbeddingDimensionMismatchError(
                    f"Vector dim {len(vec)} != model dim {self.provider.spec.dimensions}"
                )
            await self.repo.upsert_memory_embedding(
                memory_id=memory_id,
                model_id=model_id,
                dimensions=len(vec),
                embedding=vec,
                content_hash=row["content_hash"],
            )
            await self.repo.update_memory_index(memory_id, embedding_status="embedded")
            self._healthy = True
            return {
                "memory_id": str(memory_id),
                "model_id": str(model_id),
                "dimensions": len(vec),
                "status": "embedded",
                "latency_ms": res.latency_ms,
            }
        except Exception as exc:
            self._healthy = False
            await self.repo.update_memory_index(memory_id, embedding_status="failed")
            logger.warning("embedding.memory_failed", memory_id=str(memory_id), error=str(exc))
            raise EmbeddingError(str(exc)) from exc

    async def reindex_missing_for_active_model(self, limit: int = 200) -> dict[str, int]:
        """Embed all memories lacking an embedding under the currently active model_id (supports model upgrade)."""
        model_id = self._active_model_id or await self.ensure_registered()
        missing = await self.repo.list_memories_missing_embedding(model_id=model_id, limit=limit)
        embedded = 0
        failed = 0
        for row in missing:
            try:
                await self.embed_memory_record(uuid.UUID(row["memory_id"]))
                embedded += 1
            except Exception:
                failed += 1
        return {"model_id": str(model_id), "candidates": len(missing), "embedded": embedded, "failed": failed}
