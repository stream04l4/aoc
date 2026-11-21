"""AOC V2.1 P0-2 PostgreSQL -> pgvector (vector(512)) Migration, Verification & Benchmark Tool.

Supports:
  - `--verify`: Inspect PostgreSQL version, collation, extensions (`vector`, `pg_trgm`),
    `memory_embeddings.embedding` column type, 512-dim vector integrity, native `<=>` operator,
    Recall@5 sanity check, and P95 query latency.
  - `--convert`: Convert `memory_embeddings.embedding` in-place from `jsonb` to `vector(512)`
    after verifying `vector` extension is installed and every row has exact 512 dimensions.
    Optionally creates HNSW cosine index (`--create-hnsw`).
  - `--sanitize-dump <src.sql> <dst.sql>`: Filter PG 16.16 `\\restrict`/`\\unrestrict` psql
    meta-commands so dumps restore cleanly on any PG 16.x `psql` client.

NEVER mount an Alpine (musl) PostgreSQL data directory directly into a Debian (glibc)
`pgvector/pgvector:0.8.7-pg16-bookworm` container. Always migrate via logical `pg_dump` +
fresh volume restore.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.storage.database import Database


def sanitize_pg_dump(src_path: Path, dst_path: Path) -> dict[str, Any]:
    """Filter PostgreSQL 16.10+ \\restrict / \\unrestrict lines for cross-minor-version psql portability."""
    removed = 0
    total = 0
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with src_path.open("r", encoding="utf-8", errors="replace") as fin, dst_path.open("w", encoding="utf-8") as fout:
        for line in fin:
            total += 1
            if line.startswith("\\restrict ") or line.startswith("\\unrestrict "):
                removed += 1
                continue
            fout.write(line)
    return {"source": str(src_path), "output": str(dst_path), "lines_total": total, "meta_lines_removed": removed}


async def verify_pgvector_database(db_url: str, *, expected_dim: int = 512) -> dict[str, Any]:
    db = Database(db_url)
    out: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "expected_dim": expected_dim,
        "pg_version": None,
        "collation": None,
        "alembic_version": None,
        "extensions": {},
        "embedding_column": {},
        "table_counts": {},
        "vector_stats": {},
        "native_cosine_probe": {},
        "pgvector_ready": False,
    }
    try:
        async with db.engine.connect() as conn:
            out["pg_version"] = (await conn.execute(text("SELECT version()"))).scalar_one()
            coll = (
                await conn.execute(
                    text("SELECT datcollate, datctype FROM pg_database WHERE datname = current_database()")
                )
            ).mappings().first()
            if coll:
                out["collation"] = dict(coll)
            out["alembic_version"] = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
            exts = (await conn.execute(text("SELECT extname, extversion FROM pg_extension ORDER BY extname"))).mappings().all()
            out["extensions"] = {r["extname"]: r["extversion"] for r in exts}

            col = (
                await conn.execute(
                    text(
                        """
                        SELECT data_type, udt_name, format_type(a.atttypid, a.atttypmod) AS formatted_type
                        FROM information_schema.columns c
                        JOIN pg_class cls ON cls.relname = c.table_name
                        JOIN pg_attribute a ON a.attrelid = cls.oid AND a.attname = c.column_name
                        WHERE c.table_name = 'memory_embeddings' AND c.column_name = 'embedding'
                        LIMIT 1
                        """
                    )
                )
            ).mappings().first()
            if col:
                out["embedding_column"] = dict(col)

            tables = [
                r[0]
                for r in (
                    await conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
                    )
                ).all()
            ]
            for t in tables:
                out["table_counts"][t] = int((await conn.execute(text(f'SELECT count(*) FROM "{t}"'))).scalar_one() or 0)

            models = (
                await conn.execute(
                    text("SELECT id, model_key, dimensions, is_active FROM embedding_model_registry ORDER BY created_at")
                )
            ).mappings().all()
            out["vector_stats"]["registered_models"] = [
                {
                    "id": str(m["id"]),
                    "model_key": m["model_key"],
                    "dimensions": int(m["dimensions"]),
                    "is_active": bool(m["is_active"]),
                }
                for m in models
            ]
            out["vector_stats"]["total_embeddings"] = out["table_counts"].get("memory_embeddings", 0)
            out["vector_stats"]["dim_mismatch_rows"] = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM memory_embeddings WHERE dimensions != :d"),
                        {"d": expected_dim},
                    )
                ).scalar_one()
                or 0
            )

            indexes = (
                await conn.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN ('memory_embeddings', 'memory_index') ORDER BY indexname")
                )
            ).mappings().all()
            out["vector_stats"]["indexes"] = [dict(ix) for ix in indexes]

            # Native pgvector <=> latency & top-1 self-retrieval probe
            has_vector_ext = "vector" in out["extensions"]
            is_vector_col = (out["embedding_column"].get("udt_name") == "vector")
            if has_vector_ext and is_vector_col and out["vector_stats"]["total_embeddings"] > 0:
                sample_rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_id, model_id, embedding::text AS vec_txt
                            FROM memory_embeddings
                            WHERE dimensions = :d
                            ORDER BY created_at DESC
                            LIMIT 20
                            """
                        ),
                        {"d": expected_dim},
                    )
                ).mappings().all()
                latencies_ms: list[float] = []
                top1_hits = 0
                top5_hits = 0
                for sr in sample_rows:
                    t0 = time.perf_counter()
                    res = (
                        await conn.execute(
                            text(
                                """
                                SELECT memory_id, 1.0 - (embedding <=> CAST(:qvec AS vector)) AS cos_sim
                                FROM memory_embeddings
                                WHERE model_id = :mid
                                ORDER BY embedding <=> CAST(:qvec AS vector) ASC
                                LIMIT 5
                                """
                            ),
                            {"qvec": sr["vec_txt"], "mid": sr["model_id"]},
                        )
                    ).mappings().all()
                    latencies_ms.append((time.perf_counter() - t0) * 1000.0)
                    returned_ids = [r["memory_id"] for r in res]
                    if returned_ids and returned_ids[0] == sr["memory_id"]:
                        top1_hits += 1
                    if sr["memory_id"] in returned_ids:
                        top5_hits += 1

                latencies_ms.sort()
                p95_idx = min(len(latencies_ms) - 1, max(0, int(len(latencies_ms) * 0.95) - 1))
                out["native_cosine_probe"] = {
                    "samples": len(sample_rows),
                    "self_recall_at_1": round(top1_hits / len(sample_rows), 4),
                    "self_recall_at_5": round(top5_hits / len(sample_rows), 4),
                    "avg_latency_ms": round(statistics.mean(latencies_ms), 3),
                    "p95_latency_ms": round(latencies_ms[p95_idx], 3),
                }

            out["pgvector_ready"] = bool(
                has_vector_ext
                and "pg_trgm" in out["extensions"]
                and is_vector_col
                and out["vector_stats"]["dim_mismatch_rows"] == 0
            )
    finally:
        await db.dispose()
    return out


async def convert_jsonb_to_pgvector(
    db_url: str,
    *,
    expected_dim: int = 512,
    create_hnsw: bool = True,
) -> dict[str, Any]:
    """Convert `memory_embeddings.embedding` from jsonb -> vector(512) safely inside a transaction."""
    db = Database(db_url)
    summary: dict[str, Any] = {
        "converted": False,
        "previous_type": None,
        "new_type": None,
        "rows_validated": 0,
        "hnsw_created": False,
    }
    try:
        async with db.engine.begin() as conn:
            has_avail = (
                await conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"))
            ).scalar_one_or_none()
            if has_avail is None:
                raise RuntimeError("pgvector extension is not available in this PostgreSQL server")

            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

            col = (
                await conn.execute(
                    text(
                        """
                        SELECT udt_name, format_type(a.atttypid, a.atttypmod) AS formatted_type
                        FROM information_schema.columns c
                        JOIN pg_class cls ON cls.relname = c.table_name
                        JOIN pg_attribute a ON a.attrelid = cls.oid AND a.attname = c.column_name
                        WHERE c.table_name = 'memory_embeddings' AND c.column_name = 'embedding'
                        LIMIT 1
                        """
                    )
                )
            ).mappings().first()
            if col is None:
                raise RuntimeError("Table memory_embeddings.embedding column not found")
            summary["previous_type"] = col["formatted_type"]

            bad_dim = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM memory_embeddings WHERE dimensions != :d"),
                        {"d": expected_dim},
                    )
                ).scalar_one()
                or 0
            )
            if bad_dim > 0:
                raise RuntimeError(f"Found {bad_dim} rows in memory_embeddings with dimensions != {expected_dim}")

            if col["udt_name"] == "jsonb":
                bad_len = int(
                    (
                        await conn.execute(
                            text("SELECT count(*) FROM memory_embeddings WHERE jsonb_array_length(embedding) != :d"),
                            {"d": expected_dim},
                        )
                    ).scalar_one()
                    or 0
                )
                if bad_len > 0:
                    raise RuntimeError(f"Found {bad_len} jsonb rows whose array length != {expected_dim}")

                await conn.execute(
                    text(
                        f"""
                        ALTER TABLE memory_embeddings
                        ALTER COLUMN embedding TYPE vector({expected_dim})
                        USING (embedding::text)::vector({expected_dim})
                        """
                    )
                )
                summary["converted"] = True
            elif col["formatted_type"] == "vector":
                await conn.execute(
                    text(
                        f"""
                        ALTER TABLE memory_embeddings
                        ALTER COLUMN embedding TYPE vector({expected_dim})
                        USING embedding::vector({expected_dim})
                        """
                    )
                )
                summary["converted"] = True

            summary["rows_validated"] = int(
                (await conn.execute(text("SELECT count(*) FROM memory_embeddings"))).scalar_one() or 0
            )
            if create_hnsw:
                await conn.execute(
                    text(
                        """
                        CREATE INDEX IF NOT EXISTS ix_mem_emb_hnsw_cosine
                        ON memory_embeddings USING hnsw (embedding vector_cosine_ops)
                        """
                    )
                )
                summary["hnsw_created"] = True

            await conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_memory_index_trgm
                    ON memory_index USING gin ((title || ' ' || summary) gin_trgm_ops)
                    """
                )
            )
            new_col = (
                await conn.execute(
                    text(
                        """
                        SELECT format_type(a.atttypid, a.atttypmod) AS formatted_type
                        FROM information_schema.columns c
                        JOIN pg_class cls ON cls.relname = c.table_name
                        JOIN pg_attribute a ON a.attrelid = cls.oid AND a.attname = c.column_name
                        WHERE c.table_name = 'memory_embeddings' AND c.column_name = 'embedding'
                        LIMIT 1
                        """
                    )
                )
            ).scalar_one()
            summary["new_type"] = new_col
    finally:
        await db.dispose()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="AOC V2.1 pgvector migration & verification tool.")
    parser.add_argument("--db-url", default="", help="Target PostgreSQL SQLAlchemy async URL")
    parser.add_argument("--dim", type=int, default=512, help="Expected vector dimensions (default: 512)")
    parser.add_argument("--verify", action="store_true", help="Verify pgvector status, vectors, and cosine latency")
    parser.add_argument("--convert", action="store_true", help="Convert jsonb/untyped vector -> vector(512) + HNSW")
    parser.add_argument("--no-hnsw", action="store_true", help="Skip HNSW index creation during --convert")
    parser.add_argument("--sanitize-dump", nargs=2, metavar=("SRC", "DST"), help="Strip \\restrict lines from pg_dump")
    args = parser.parse_args()

    if args.sanitize_dump:
        res = sanitize_pg_dump(Path(args.sanitize_dump[0]), Path(args.sanitize_dump[1]))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    db_url = args.db_url or get_settings().database.url
    if args.convert:
        conv = asyncio.run(convert_jsonb_to_pgvector(db_url, expected_dim=args.dim, create_hnsw=not args.no_hnsw))
        ver = asyncio.run(verify_pgvector_database(db_url, expected_dim=args.dim))
        print(json.dumps({"convert": conv, "verify": ver}, ensure_ascii=False, indent=2))
        return 0 if ver["pgvector_ready"] else 2

    ver = asyncio.run(verify_pgvector_database(db_url, expected_dim=args.dim))
    print(json.dumps(ver, ensure_ascii=False, indent=2))
    return 0 if ver["pgvector_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
