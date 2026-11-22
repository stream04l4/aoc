"""V4.0 M0 Strictly Read-Only Skill Asset Audit & Canonicalization Planner.

Inspects all existing `skill_registry` and `skill_validation_results` rows in PostgreSQL
without mutating any data, clusters homogeneous/duplicate skills, classifies one-off
truncated diagnostic scripts vs reusable seed candidates, and proposes canonical
`<domain>.<capability>.<operation>` names.

Usage:
  .venv/bin/python scripts/v4_skill_audit.py --output docs/v4_skill_audit_v3_2.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.storage.database import Database

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "in", "for", "with", "that", "by", "on", "at",
        "create", "build", "run", "make", "write", "validate", "verify", "only", "l1", "l0",
        "isolated", "bounded", "offline", "deterministic", "stdlib", "python", "embed", "embedded",
        "embeds", "trusted", "snapshot", "read", "reads",
    }
)


def _tokenize(text_val: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+", (text_val or "").lower())
    return {t for t in toks if len(t) > 1 and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _is_canonical_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{1,24}\.[a-z][a-z0-9_]{1,24}\.[a-z][a-z0-9_]{1,32}", name or ""))


def _propose_canonical_name(skill_key: str, capability_key: str, description: str) -> tuple[str, str]:
    """Return (proposed_canonical_name, functional_cluster_tag)."""
    blob = f"{skill_key} {description}".lower()
    cap = re.sub(r"[^a-z0-9_]+", "_", (capability_key or "coding").lower()).strip("_") or "coding"

    if "curiosity" in blob and ("ledger" in blob or "gap" in blob or "reconcil" in blob):
        return f"cognition.{cap}.curiosity_gap_reconcile", "curiosity_gap_ledger"
    if "provenance" in blob or "checksum" in blob or "sha256" in blob:
        return f"audit.{cap}.provenance_checksum_check", "provenance_checksum"
    if "skill" in blob and ("audit" in blob or "mapping" in blob or "artifact" in blob):
        return f"self_model.{cap}.skill_artifact_audit", "skill_artifact_audit"
    if "agent" in blob and ("redundan" in blob or "health" in blob):
        return f"runtime.{cap}.agent_health_diagnostic", "agent_health_diag"
    if "observation" in blob and "reconcil" in blob:
        return f"perception.{cap}.observation_reconcile", "observation_reconcile"
    if "assessment" in blob or "inventory" in blob or "extract" in blob:
        return f"cognition.{cap}.assessment_extract", "assessment_extract"
    if "diagnostic" in blob or "diag" in blob:
        return f"system.{cap}.snapshot_diagnostic", "generic_snapshot_diagnostic"
    if "json" in blob:
        return f"data.{cap}.json_reconcile", "json_utility"
    slug = re.sub(r"[^a-z0-9_]+", "_", skill_key.lower()).strip("_")[:28] or "utility"
    return f"general.{cap}.{slug}", "other"


async def collect_skill_audit() -> dict[str, Any]:
    settings = get_settings()
    db = Database(settings.database.url)
    try:
        async with db.session() as s:
            rows = (
                await s.execute(
                    text(
                        """
                        SELECT
                            sr.id,
                            sr.skill_key,
                            sr.version,
                            sr.capability_key,
                            sr.description,
                            sr.artifact_path,
                            sr.artifact_checksum,
                            sr.permission_level,
                            sr.status,
                            sr.source_task_id,
                            sr.source_execution_id,
                            sr.created_at,
                            COUNT(svr.id) AS validation_runs,
                            COALESCE(SUM(CASE WHEN svr.passed THEN 1 ELSE 0 END), 0) AS validation_passes
                        FROM skill_registry sr
                        LEFT JOIN skill_validation_results svr ON svr.skill_id = sr.id
                        GROUP BY sr.id
                        ORDER BY sr.created_at DESC
                        """
                    )
                )
            ).mappings().all()
    finally:
        await db.dispose()

    items: list[dict[str, Any]] = []
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for r in rows:
        skey = str(r["skill_key"])
        cap = str(r["capability_key"])
        desc = str(r["description"] or "")
        canonical, cluster_tag = _propose_canonical_name(skey, cap, desc)
        truncated_sentence = (len(skey) >= 56 and skey.count("_") >= 6) or skey.startswith(("create_", "build_"))
        toks = _tokenize(f"{skey} {desc}")
        rec = {
            "id": str(r["id"]),
            "skill_key": skey,
            "version": str(r["version"]),
            "capability_key": cap,
            "permission_level": str(r["permission_level"]),
            "status": str(r["status"]),
            "has_checksum": bool(r["artifact_checksum"]),
            "artifact_path": r["artifact_path"],
            "validation_runs": int(r["validation_runs"] or 0),
            "validation_passes": int(r["validation_passes"] or 0),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "is_canonical_name": _is_canonical_name(skey),
            "looks_like_truncated_goal_sentence": truncated_sentence,
            "proposed_canonical_name": canonical,
            "cluster": cluster_tag,
            "_tokens": toks,
        }
        items.append(rec)
        clusters[cluster_tag].append(rec)

    # Decide recommended action per skill:
    # Within each functional cluster, pick the best VALIDATED skill with highest validation passes as seed candidate;
    # mark homogeneous siblings as merge_archive (DEPRECATED with superseded_by) and failing DRAFTs as archive_draft.
    cluster_summaries: list[dict[str, Any]] = []
    action_counts: Counter[str] = Counter()

    for cname, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        validated = [m for m in members if m["status"] == "VALIDATED" and m["validation_passes"] > 0]
        validated.sort(key=lambda m: (m["validation_passes"], m["created_at"] or ""), reverse=True)
        seed = validated[0] if validated else None
        for m in members:
            if seed is not None and m["id"] == seed["id"]:
                m["recommended_action"] = "promote_as_canonical_seed"
                m["superseded_by_seed_id"] = None
            elif m["status"] == "VALIDATED" and seed is not None:
                m["recommended_action"] = "merge_and_deprecate_duplicate"
                m["superseded_by_seed_id"] = seed["id"]
            else:
                m["recommended_action"] = "archive_failed_or_oneoff_draft"
                m["superseded_by_seed_id"] = seed["id"] if seed else None
            action_counts[m["recommended_action"]] += 1

        # Compute intra-cluster average Jaccard lexical similarity
        sims: list[float] = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                sims.append(_jaccard(members[i]["_tokens"], members[j]["_tokens"]))
        cluster_summaries.append(
            {
                "cluster": cname,
                "proposed_canonical_name": members[0]["proposed_canonical_name"],
                "total_skills": len(members),
                "validated_count": len(validated),
                "draft_count": sum(1 for m in members if m["status"] == "DRAFT"),
                "avg_intra_cluster_similarity": round(sum(sims) / len(sims), 4) if sims else 1.0,
                "seed_skill_id": seed["id"] if seed else None,
                "seed_skill_key": seed["skill_key"] if seed else None,
            }
        )

    for m in items:
        m.pop("_tokens", None)

    total = len(items)
    validated_total = sum(1 for m in items if m["status"] == "VALIDATED")
    non_canonical_total = sum(1 for m in items if not m["is_canonical_name"])
    truncated_total = sum(1 for m in items if m["looks_like_truncated_goal_sentence"])
    seed_count = action_counts.get("promote_as_canonical_seed", 0)
    redundant_validated = action_counts.get("merge_and_deprecate_duplicate", 0)

    return {
        "schema_version": "v4_skill_audit_v1",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "summary": {
            "total_skills": total,
            "validated_skills": validated_total,
            "draft_skills": sum(1 for m in items if m["status"] == "DRAFT"),
            "deprecated_skills": sum(1 for m in items if m["status"] == "DEPRECATED"),
            "non_canonical_names": non_canonical_total,
            "truncated_goal_sentence_names": truncated_total,
            "functional_clusters": len(cluster_summaries),
            "recommended_canonical_seeds": seed_count,
            "redundant_validated_to_merge": redundant_validated,
            "drafts_to_archive": action_counts.get("archive_failed_or_oneoff_draft", 0),
            "projected_dedup_reduction_ratio": (
                round(1.0 - (seed_count / total), 4) if total > 0 else 0.0
            ),
            "action_breakdown": dict(action_counts),
        },
        "clusters": cluster_summaries,
        "skills": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AOC V4.0 M0 read-only Skill Asset Audit")
    parser.add_argument("--output", type=str, default="", help="Optional JSON output file path")
    args = parser.parse_args()
    report = asyncio.run(collect_skill_audit())
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
        print(f"Wrote V4.0 M0 skill audit report to {out_path}")
        print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
