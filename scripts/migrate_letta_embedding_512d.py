"""AOC V2.1 P0-3 Letta Agent 512d Embedding Alignment Script.

Eliminates the legacy 1024-dim zero-padding workaround and temporary `172.18.0.1:9200`
forwarder by inspecting (default: --dry-run) or updating (--execute) the Letta Agent's
`embedding_config` to native 512 dimensions on `http://aoc-core:9200/v1`.

Usage:
    .venv/bin/python scripts/migrate_letta_embedding_512d.py --dry-run
    .venv/bin/python scripts/migrate_letta_embedding_512d.py --execute \
        --endpoint http://aoc-core:9200/v1 --model aoc-local-embed-512 --dim 512
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() else None
if PROJECT_ROOT and str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings


async def align_letta_embedding(
    *,
    letta_base_url: str,
    agent_name: str,
    target_endpoint: str,
    target_model: str,
    target_dim: int,
    execute: bool = False,
) -> dict[str, Any]:
    base = letta_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{base}/v1/agents/", params={"name": agent_name})
        r.raise_for_status()
        agents = r.json()
        if not agents:
            return {"found": False, "agent_name": agent_name, "mode": "execute" if execute else "dry_run"}

        agent = agents[0]
        agent_id = agent["id"]
        cur_cfg = dict(agent.get("embedding_config") or {})
        desired_cfg = {
            **cur_cfg,
            "embedding_endpoint_type": cur_cfg.get("embedding_endpoint_type", "openai"),
            "embedding_endpoint": target_endpoint,
            "embedding_model": target_model,
            "embedding_dim": target_dim,
        }
        needs_update = (
            cur_cfg.get("embedding_endpoint") != target_endpoint
            or cur_cfg.get("embedding_model") != target_model
            or int(cur_cfg.get("embedding_dim") or 0) != target_dim
        )

        result: dict[str, Any] = {
            "found": True,
            "mode": "execute" if execute else "dry_run",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "current_embedding_config": cur_cfg,
            "desired_embedding_config": desired_cfg,
            "needs_update": needs_update,
            "updated": False,
        }

        if execute and needs_update:
            pr = await client.patch(f"{base}/v1/agents/{agent_id}", json={"embedding_config": desired_cfg})
            pr.raise_for_status()
            result["updated"] = True
            result["post_update_embedding_config"] = pr.json().get("embedding_config")

        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Align Letta Agent embedding_config to native 512d.")
    parser.add_argument("--letta-url", default="")
    parser.add_argument("--agent-name", default="")
    parser.add_argument("--endpoint", default="http://aoc-core:9200/v1")
    parser.add_argument("--model", default="aoc-local-embed-512")
    parser.add_argument("--dim", type=int, default=512)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    res = asyncio.run(
        align_letta_embedding(
            letta_base_url=args.letta_url or s.memory.letta_base_url,
            agent_name=args.agent_name or s.memory.agent_name,
            target_endpoint=args.endpoint,
            target_model=args.model,
            target_dim=args.dim,
            execute=bool(args.execute) and not bool(args.dry_run),
        )
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
