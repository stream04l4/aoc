"""V4.0 M2 Legacy Skill Canonicalization & Deduplication Utility.

Defaults to `--dry-run` (strictly read-only). Pass `--apply` only after Alembic 0010 is applied
to canonicalize truncated-sentence legacy skills, merge homogeneous VALIDATED clusters
(preserving all IDs and history via `superseded_by`), and archive failed DRAFTs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_self_model_config, get_settings
from app.self_model.skills import SkillRegistryManager
from app.skills.evolution import SkillEvolutionEngine
from app.storage.database import Database
from app.storage.repository import StateRepository


async def run_cleanup(apply_changes: bool) -> dict:
    settings = get_settings()
    db = Database(settings.database.url)
    try:
        repo = StateRepository(db)
        sm_cfg = get_self_model_config()
        skills = SkillRegistryManager(repo=repo, governance=None, config=sm_cfg)
        evo = SkillEvolutionEngine(repo=repo, skill_registry=skills)
        return await evo.prune_and_deduplicate_skills(dry_run=(not apply_changes))
    finally:
        await db.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="AOC V4.0 M2 Skill Canonicalization & Deduplication")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup mutations (default is --dry-run)")
    args = parser.parse_args()
    summary = asyncio.run(run_cleanup(apply_changes=bool(args.apply)))
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
