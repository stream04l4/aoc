"""Bounded RSI Skill Evolution Engine (V4.0 M2).

Strictly lives OUTSIDE the immutable kernel (`app/core/`, `app/drives/`, `app/affect/`).
Implements:
  1. Canonical skill naming (`<domain>.<capability>.<operation>`) preventing truncated-sentence bloat.
  2. Joint semantic + schema + capability deduplication.
  3. Bounded `v1 -> vNext` isolated evolution with dual verification:
       - Gate A: Base regression test suite executed against `vNext` artifact (`python -I`).
       - Gate B: `vNext` new boundary test suite executed in isolated sandbox (`validate_skill`).
       - Benchmark comparison: objective improvement required (assertions / schema / latency / bugfix)
         with automatic rollback if any regression occurs.
  4. Safe version rollback restoring `parent_version` (with SHA-256 verification).
  5. Deterministic skill pruning & duplicate cluster merging (preserving 100% of history).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import sanitize_env
from app.core.enums import PermissionLevel
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.self_model.interfaces import SkillStatus
from app.self_model.skills import SkillGovernanceError, SkillRegistryManager, scan_skill_code_safety
from app.storage.repository import StateRepository

logger = get_logger("aoc.skills.evolution")

_CANONICAL_RE = re.compile(r"^[a-z][a-z0-9_]{1,24}\.[a-z][a-z0-9_]{1,24}\.[a-z][a-z0-9_]{1,32}$")

_NOISE_WORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "in", "for", "with", "that", "by", "on", "at", "as",
        "create", "build", "run", "make", "write", "validate", "verify", "implement", "reusable",
        "only", "l1", "l0", "l2", "isolated", "bounded", "offline", "deterministic", "stdlib",
        "python", "utility", "script", "tool", "embed", "embedded", "embeds", "trusted", "read", "reads",
    }
)

_PERM_RANK = {
    PermissionLevel.L0.value: 0,
    PermissionLevel.L1.value: 1,
    PermissionLevel.L2.value: 2,
    PermissionLevel.L3.value: 3,
}


def is_canonical_skill_name(name: str | None) -> bool:
    return bool(name and _CANONICAL_RE.match(str(name).strip()))


def _clean_tokens(text_val: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", (text_val or "").lower())
    return [t for t in raw if len(t) > 1 and t not in _NOISE_WORDS]


def _jaccard_tokens(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def canonicalize_skill_name(
    raw_name: str,
    capability_key: str = "coding",
    description: str = "",
) -> str:
    """Deterministically normalize any raw skill name/title into `<domain>.<capability>.<operation>`."""
    candidate = str(raw_name or "").strip().lower()
    if is_canonical_skill_name(candidate):
        return candidate

    cap = re.sub(r"[^a-z0-9_]+", "_", (capability_key or "coding").strip().lower()).strip("_")[:24] or "coding"
    if not cap[0].isalpha():
        cap = f"cap_{cap}"[:24]

    blob = f"{candidate} {description}".lower()

    # High-signal domain + operation clustering (matches real AOC functional clusters)
    if "curiosity" in blob and ("ledger" in blob or "gap" in blob or "reconcil" in blob):
        return f"cognition.{cap}.curiosity_gap_reconcile"
    if "provenance" in blob or "checksum" in blob or "sha256" in blob:
        return f"audit.{cap}.provenance_checksum_check"
    if "skill" in blob and ("audit" in blob or "mapping" in blob or "artifact" in blob):
        return f"self_model.{cap}.skill_artifact_audit"
    if "agent" in blob and ("redundan" in blob or "health" in blob):
        return f"runtime.{cap}.agent_health_diagnostic"
    if "observation" in blob and "reconcil" in blob:
        return f"perception.{cap}.observation_reconcile"

    # Infer domain from keywords
    if any(k in blob for k in ("git", "repo", "commit", "diff", "branch")):
        domain = "repository"
    elif any(k in blob for k in ("rss", "arxiv", "web", "paper", "research", "feed", "search")):
        domain = "research"
    elif any(k in blob for k in ("json", "csv", "yaml", "kv", "parse", "count", "format", "normalize", "metric")):
        domain = "data"
    elif any(k in blob for k in ("memory", "letta", "vector", "embed", "recall")):
        domain = "memory"
    elif any(k in blob for k in ("system", "disk", "cpu", "diag", "log", "health")):
        domain = "system"
    elif any(k in blob for k in ("debug", "trace", "test", "bug", "fix", "patch")):
        domain = "engineering"
    else:
        domain = "general"

    # Extract concise operation slug (up to 3 meaningful tokens, max 32 chars)
    toks = _clean_tokens(candidate) or _clean_tokens(description)
    dedup_toks: list[str] = []
    for t in toks:
        if t not in dedup_toks and t != cap and t != domain:
            dedup_toks.append(t)
        if len(dedup_toks) >= 3:
            break
    op = "_".join(dedup_toks)[:32].strip("_") or "utility"
    if not op[0].isalpha():
        op = f"op_{op}"[:32]
    return f"{domain}.{cap}.{op}"


def bump_semver(version: str, change_type: str = "minor") -> str:
    """Increment semantic version (patch | minor | major)."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(version or "1.0.0").strip())
    major, minor, patch = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (1, 0, 0)
    kind = str(change_type or "minor").strip().lower()
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


def _count_assertions(code: str) -> int:
    text = code or ""
    return len(re.findall(r"\b(assert\b|assertEqual|assertTrue|assertFalse|assertIn|assertRaises)\b", text))


class SkillDuplicateMatch(BaseModel):
    matched_skill_id: str
    matched_skill_key: str
    matched_canonical_name: str | None = None
    matched_version: str
    similarity: float = 0.0
    reason: str = ""


class SkillEvolutionResult(BaseModel):
    evolved: bool = False
    promoted: bool = False
    rolled_back: bool = False
    awaiting_human_approval: bool = False
    base_skill_id: str
    base_version: str
    new_skill_id: str | None = None
    new_version: str | None = None
    canonical_name: str = ""
    regression_passed: bool = False
    new_tests_passed: bool = False
    base_latency_ms: float | None = None
    new_latency_ms: float | None = None
    reason: str = ""


class SkillEvolutionEngine:
    """External bounded RSI evolution engine for VALIDATED Skill packages."""

    def __init__(
        self,
        *,
        repo: StateRepository,
        skill_registry: SkillRegistryManager,
        max_versions_per_canonical: int = 10,
        dedup_threshold: float = 0.65,
        max_latency_regression_ratio: float = 1.75,
    ) -> None:
        self.repo = repo
        self.skill_registry = skill_registry
        self.max_versions_per_canonical = max(2, int(max_versions_per_canonical))
        self.dedup_threshold = max(0.3, min(0.95, float(dedup_threshold)))
        self.max_latency_regression_ratio = max(1.1, float(max_latency_regression_ratio))

    async def find_duplicate_skill(
        self,
        *,
        canonical_name: str,
        capability_key: str,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> SkillDuplicateMatch | None:
        """Joint canonical + schema + lexical semantic deduplication against active VALIDATED skills."""
        validated = await self.repo.list_skills(status=SkillStatus.VALIDATED.value, limit=200)
        cand_toks = set(_clean_tokens(f"{canonical_name} {description}"))
        cand_req = set((input_schema or {}).get("required") or [])

        best_match: SkillDuplicateMatch | None = None
        for sk in validated:
            sk_canon = sk.get("canonical_name") or canonicalize_skill_name(
                sk["skill_key"], sk.get("capability_key", "coding"), sk.get("description", "")
            )
            if sk_canon == canonical_name:
                return SkillDuplicateMatch(
                    matched_skill_id=sk["id"],
                    matched_skill_key=sk["skill_key"],
                    matched_canonical_name=sk_canon,
                    matched_version=sk["version"],
                    similarity=1.0,
                    reason="exact_canonical_name_match",
                )
            if sk.get("capability_key") != capability_key:
                continue
            sk_toks = set(_clean_tokens(f"{sk_canon} {sk['skill_key']} {sk.get('description', '')}"))
            lex_sim = _jaccard_tokens(cand_toks, sk_toks)
            sk_req = set((sk.get("input_schema") or {}).get("required") or [])
            schema_bonus = 0.15 if (cand_req and sk_req and cand_req == sk_req) else 0.0
            combined = min(1.0, lex_sim + schema_bonus)
            if combined >= self.dedup_threshold and (best_match is None or combined > best_match.similarity):
                best_match = SkillDuplicateMatch(
                    matched_skill_id=sk["id"],
                    matched_skill_key=sk["skill_key"],
                    matched_canonical_name=sk_canon,
                    matched_version=sk["version"],
                    similarity=round(combined, 4),
                    reason=f"semantic_schema_match(sim={combined:.3f})",
                )
        return best_match

    async def _run_test_code_against_artifact(
        self,
        *,
        artifact_path: Path,
        test_code: str,
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, float, str]:
        """Run a test script against a given artifact in a fresh isolated `python -I` temp directory."""
        t0 = time.perf_counter()
        if not test_code.strip():
            return False, 0.0, "empty_test_code"
        try:
            with tempfile.TemporaryDirectory(prefix="aoc-skill-reg-") as tmpdir:
                tmp_path = Path(tmpdir)
                dst = tmp_path / artifact_path.name
                shutil.copy2(artifact_path, dst)
                runner = tmp_path / "_reg_test.py"
                preamble = f"import sys as _aoc_sys; _aoc_sys.path.insert(0, {str(tmp_path)!r})\n"
                runner.write_text(preamble + test_code, encoding="utf-8")
                env = sanitize_env(
                    {
                        "AOC_SKILL_WORKDIR": str(tmp_path),
                        "AOC_SKILL_ARTIFACT": str(dst),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                )
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    str(runner),
                    cwd=str(tmp_path),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
                    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                    out_txt = out_b.decode("utf-8", errors="replace")
                    err_txt = err_b.decode("utf-8", errors="replace")
                    return proc.returncode == 0, elapsed_ms, (out_txt + "\n" + err_txt).strip()[:1200]
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                    return False, round((time.perf_counter() - t0) * 1000.0, 2), f"timeout>{timeout_seconds}s"
        except Exception as exc:
            return False, round((time.perf_counter() - t0) * 1000.0, 2), f"runner_error:{exc}"

    async def evolve_skill(
        self,
        base_skill_id: uuid.UUID | str,
        *,
        new_artifact_code: str,
        new_test_code: str,
        change_type: str = "minor",
        change_summary: str = "",
        description: str | None = None,
        new_input_schema: dict[str, Any] | None = None,
        new_output_schema: dict[str, Any] | None = None,
        new_permission_level: str | None = None,
        approved_l2: bool = False,
        source_task_id: uuid.UUID | str | None = None,
        source_execution_id: uuid.UUID | str | None = None,
    ) -> SkillEvolutionResult:
        """Bounded RSI evolution: create vNext, run base regression + new tests, compare, promote or auto-rollback."""
        base_uuid = uuid.UUID(str(base_skill_id))
        base = await self.repo.get_skill(base_uuid)
        if base is None:
            raise ValueError(f"Base skill {base_skill_id} not found")

        canonical = base.get("canonical_name") or canonicalize_skill_name(
            base["skill_key"], base["capability_key"], base.get("description", "")
        )
        base_perm = str(base.get("permission_level") or PermissionLevel.L1.value).upper()
        target_perm = str(new_permission_level or base_perm).upper()

        # 1. Hard RSI safety guards: no permission escalation, no L3, bounded version cap
        if target_perm == PermissionLevel.L3.value or _PERM_RANK.get(target_perm, 3) > _PERM_RANK.get(base_perm, 1):
            return SkillEvolutionResult(
                evolved=False,
                rolled_back=True,
                base_skill_id=str(base_uuid),
                base_version=base["version"],
                canonical_name=canonical,
                reason=f"Permission escalation forbidden during RSI ({base_perm} -> {target_perm})",
            )

        existing_versions = await self.repo.list_skill_versions(canonical, limit=self.max_versions_per_canonical + 5)
        if len(existing_versions) >= self.max_versions_per_canonical:
            return SkillEvolutionResult(
                evolved=False,
                rolled_back=True,
                base_skill_id=str(base_uuid),
                base_version=base["version"],
                canonical_name=canonical,
                reason=f"RSI version budget reached ({len(existing_versions)}/{self.max_versions_per_canonical})",
            )

        # Static safety scan on BOTH artifact and test code before touching disk/DB
        art_viols = scan_skill_code_safety(new_artifact_code)
        tst_viols = scan_skill_code_safety(new_test_code)
        if art_viols or tst_viols:
            return SkillEvolutionResult(
                evolved=False,
                rolled_back=True,
                base_skill_id=str(base_uuid),
                base_version=base["version"],
                canonical_name=canonical,
                reason=f"Static safety scan blocked vNext: {', '.join(art_viols + tst_viols)}",
            )

        # Pick unused next semver
        used_vers = {v["version"] for v in existing_versions}
        next_ver = bump_semver(base["version"], change_type)
        while next_ver in used_vers:
            next_ver = bump_semver(next_ver, "patch")

        art_filename = Path(base["artifact_path"]).name if base.get("artifact_path") else "skill.py"
        try:
            new_row = await self.skill_registry.register_skill(
                skill_key=base["skill_key"],
                canonical_name=canonical,
                version=next_ver,
                parent_version=base["version"],
                capability_key=base["capability_key"],
                description=description or base.get("description") or "",
                artifact_code=new_artifact_code,
                artifact_filename=art_filename,
                test_spec={"kind": "python_assertions", "code": new_test_code, "expected_exit_code": 0},
                permission_level=target_perm,
                input_schema=new_input_schema if new_input_schema is not None else (base.get("input_schema") or {}),
                output_schema=new_output_schema if new_output_schema is not None else (base.get("output_schema") or {}),
                source_task_id=source_task_id,
                source_execution_id=source_execution_id,
                evolution_meta={
                    "parent_skill_id": str(base_uuid),
                    "parent_version": base["version"],
                    "change_type": change_type,
                    "change_summary": change_summary[:400],
                },
            )
        except SkillGovernanceError as exc:
            return SkillEvolutionResult(
                evolved=False,
                rolled_back=True,
                base_skill_id=str(base_uuid),
                base_version=base["version"],
                canonical_name=canonical,
                reason=f"Governance rejected vNext: {exc}",
            )

        new_uuid = uuid.UUID(new_row["id"])
        new_art_path = Path(new_row["artifact_path"])

        # 2. Gate A: Run BASE skill's original regression test suite against vNext artifact!
        base_test_code = str((base.get("test_spec") or {}).get("code") or "").strip()
        reg_passed = True
        reg_ms: float | None = None
        base_ms: float | None = None
        reg_excerpt = ""
        if base_test_code:
            reg_passed, reg_ms, reg_excerpt = await self._run_test_code_against_artifact(
                artifact_path=new_art_path,
                test_code=base_test_code,
            )
            if base.get("artifact_path") and Path(base["artifact_path"]).is_file():
                _b_ok, base_ms, _ = await self._run_test_code_against_artifact(
                    artifact_path=Path(base["artifact_path"]),
                    test_code=base_test_code,
                )

        if not reg_passed:
            await self.repo.update_skill_metadata(
                new_uuid,
                status=SkillStatus.DEPRECATED.value,
                evolution_meta={
                    "decision": "auto_rolled_back",
                    "rollback_reason": "base_regression_failed",
                    "regression_excerpt": reg_excerpt[:500],
                },
            )
            await self.repo.log_event(
                "skill.evolution_rolled_back",
                message=f"Auto-rolled back {canonical}@{next_ver}: base regression test failed",
                payload={"base_id": str(base_uuid), "new_id": str(new_uuid), "reason": "base_regression_failed"},
            )
            return SkillEvolutionResult(
                evolved=True,
                promoted=False,
                rolled_back=True,
                base_skill_id=str(base_uuid),
                base_version=base["version"],
                new_skill_id=str(new_uuid),
                new_version=next_ver,
                canonical_name=canonical,
                regression_passed=False,
                new_tests_passed=False,
                base_latency_ms=base_ms,
                new_latency_ms=reg_ms,
                reason="Auto-rolled back: vNext broke base regression test suite",
            )

        # 3. Gate B: Validate vNext's own new test suite in isolated sandbox (enforces L2 approval policy too!)
        val_res = await self.skill_registry.validate_skill(new_uuid, approved_l2_promotion=approved_l2)
        if not val_res["passed"]:
            await self.repo.update_skill_metadata(
                new_uuid,
                status=SkillStatus.DEPRECATED.value,
                evolution_meta={"decision": "auto_rolled_back", "rollback_reason": "new_tests_failed"},
            )
            return SkillEvolutionResult(
                evolved=True,
                promoted=False,
                rolled_back=True,
                base_skill_id=str(base_uuid),
                base_version=base["version"],
                new_skill_id=str(new_uuid),
                new_version=next_ver,
                canonical_name=canonical,
                regression_passed=True,
                new_tests_passed=False,
                base_latency_ms=base_ms,
                new_latency_ms=reg_ms,
                reason="Auto-rolled back: vNext failed new assertion tests",
            )

        # L2 skill passed tests but awaits human approval -> keep base active, leave vNext in DRAFT
        if val_res.get("awaiting_human_approval"):
            return SkillEvolutionResult(
                evolved=True,
                promoted=False,
                rolled_back=False,
                awaiting_human_approval=True,
                base_skill_id=str(base_uuid),
                base_version=base["version"],
                new_skill_id=str(new_uuid),
                new_version=next_ver,
                canonical_name=canonical,
                regression_passed=True,
                new_tests_passed=True,
                base_latency_ms=base_ms,
                new_latency_ms=reg_ms,
                reason="vNext passed regression & new tests; awaiting human L2 approval",
            )

        # 4. Objective Improvement & Latency Regression Guard
        base_asserts = _count_assertions(base_test_code)
        new_asserts = _count_assertions(new_test_code)
        schema_enriched = bool(
            (new_input_schema and not base.get("input_schema"))
            or (new_output_schema and not base.get("output_schema"))
        )
        code_changed = new_row.get("artifact_checksum") != base.get("artifact_checksum")
        base_degraded = (
            int(base.get("reuse_count") or 0) >= 2
            and (int(base.get("reuse_success_count") or 0) / max(1, int(base.get("reuse_count") or 1))) < 0.8
        )
        faster = bool(base_ms and reg_ms and reg_ms < base_ms * 0.95)
        latency_regressed = bool(base_ms and base_ms >= 40.0 and reg_ms and reg_ms > base_ms * self.max_latency_regression_ratio)

        has_objective_improvement = (
            new_asserts > base_asserts
            or schema_enriched
            or faster
            or base_degraded
            or (code_changed and new_asserts >= base_asserts and bool(change_summary.strip()))
        )

        if not has_objective_improvement or latency_regressed:
            rb_reason = "latency_regression" if latency_regressed else "no_objective_improvement_over_base"
            await self.repo.update_skill_metadata(
                new_uuid,
                status=SkillStatus.DEPRECATED.value,
                evolution_meta={
                    "decision": "auto_rolled_back",
                    "rollback_reason": rb_reason,
                    "base_assertions": base_asserts,
                    "new_assertions": new_asserts,
                    "base_latency_ms": base_ms,
                    "new_latency_ms": reg_ms,
                },
            )
            await self.skill_registry._notify_skills_changed()
            return SkillEvolutionResult(
                evolved=True,
                promoted=False,
                rolled_back=True,
                base_skill_id=str(base_uuid),
                base_version=base["version"],
                new_skill_id=str(new_uuid),
                new_version=next_ver,
                canonical_name=canonical,
                regression_passed=True,
                new_tests_passed=True,
                base_latency_ms=base_ms,
                new_latency_ms=reg_ms,
                reason=f"Auto-rolled back: {rb_reason}",
            )

        # 5. Promote vNext & supersede base atomically (keeping base history intact for instant rollback!)
        await self.repo.update_skill_metadata(
            new_uuid,
            status=SkillStatus.VALIDATED.value,
            canonical_name=canonical,
            evolution_meta={
                "decision": "promoted",
                "promoted_at": utcnow().isoformat(),
                "base_assertions": base_asserts,
                "new_assertions": new_asserts,
                "base_latency_ms": base_ms,
                "new_latency_ms": reg_ms,
            },
        )
        await self.repo.update_skill_metadata(
            base_uuid,
            status=SkillStatus.DEPRECATED.value,
            canonical_name=canonical,
            superseded_by=new_uuid,
            evolution_meta={"superseded_at": utcnow().isoformat(), "superseded_by_version": next_ver},
        )
        await self.skill_registry._notify_skills_changed()
        await self.repo.log_event(
            "skill.evolution_promoted",
            message=f"Promoted {canonical} {base['version']} -> {next_ver} (assertions {base_asserts}->{new_asserts})",
            payload={
                "canonical_name": canonical,
                "base_id": str(base_uuid),
                "new_id": str(new_uuid),
                "base_version": base["version"],
                "new_version": next_ver,
            },
        )
        return SkillEvolutionResult(
            evolved=True,
            promoted=True,
            rolled_back=False,
            base_skill_id=str(base_uuid),
            base_version=base["version"],
            new_skill_id=str(new_uuid),
            new_version=next_ver,
            canonical_name=canonical,
            regression_passed=True,
            new_tests_passed=True,
            base_latency_ms=base_ms,
            new_latency_ms=reg_ms,
            reason=f"Promoted {canonical} {base['version']} -> {next_ver}",
        )

    async def rollback_skill_version(
        self, skill_id: uuid.UUID | str, *, reason: str = "rollback_to_parent"
    ) -> dict[str, Any]:
        """Deprecate a problematic version and cleanly restore its integrity-verified parent_version."""
        sid = uuid.UUID(str(skill_id))
        cur = await self.repo.get_skill(sid)
        if cur is None:
            raise ValueError(f"Skill {skill_id} not found")

        parent_ver = cur.get("parent_version")
        parent_id_meta = (cur.get("evolution_meta") or {}).get("parent_skill_id")
        parent: dict[str, Any] | None = None
        if parent_id_meta:
            parent = await self.repo.get_skill(uuid.UUID(str(parent_id_meta)))
        if parent is None and parent_ver:
            parent = await self.repo.get_skill_by_name(
                cur.get("canonical_name") or cur["skill_key"], version=parent_ver, validated_only=False
            )
        if parent is None:
            raise ValueError(f"No parent version found to roll back skill {cur['skill_key']}@{cur['version']}")

        parent_uuid = uuid.UUID(parent["id"])
        intact, integrity_reason = await self.skill_registry.verify_skill_integrity(parent_uuid)
        if not intact:
            raise SkillGovernanceError(f"Cannot roll back to parent {parent['version']}: integrity failed ({integrity_reason})")

        await self.repo.update_skill_metadata(
            sid,
            status=SkillStatus.DEPRECATED.value,
            evolution_meta={"rolled_back_at": utcnow().isoformat(), "rollback_reason": reason},
        )
        await self.repo.update_skill_metadata(
            parent_uuid,
            status=SkillStatus.VALIDATED.value,
            clear_superseded_by=True,
            evolution_meta={"restored_at": utcnow().isoformat(), "restored_from_version": cur["version"]},
        )
        await self.skill_registry._notify_skills_changed()
        await self.repo.log_event(
            "skill.version_rolled_back",
            message=f"Rolled back {cur.get('canonical_name') or cur['skill_key']} from {cur['version']} to {parent['version']}: {reason}",
            payload={"deprecated_id": str(sid), "restored_id": str(parent_uuid), "reason": reason},
        )
        return {
            "rolled_back": True,
            "deprecated_skill_id": str(sid),
            "deprecated_version": cur["version"],
            "restored_skill_id": str(parent_uuid),
            "restored_version": parent["version"],
            "reason": reason,
        }

    async def prune_and_deduplicate_skills(
        self,
        *,
        max_zero_reuse_age_days: float = 14.0,
        min_reuse_success_rate: float = 0.5,
        min_reuse_samples: int = 3,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Canonicalize legacy names, merge duplicate VALIDATED clusters, and deprecate low-value skills."""
        ts = now or utcnow()
        all_skills = await self.repo.list_skills(limit=500)
        by_canonical: dict[str, list[dict[str, Any]]] = {}

        canonicalized_count = 0
        for sk in all_skills:
            canon = sk.get("canonical_name")
            if not is_canonical_skill_name(canon):
                canon = canonicalize_skill_name(sk["skill_key"], sk["capability_key"], sk.get("description", ""))
                canonicalized_count += 1
                if not dry_run:
                    await self.repo.update_skill_metadata(uuid.UUID(sk["id"]), canonical_name=canon)
                sk = {**sk, "canonical_name": canon}
            by_canonical.setdefault(canon, []).append(sk)

        kept_seeds: list[dict[str, Any]] = []
        merged_duplicates: list[dict[str, Any]] = []
        archived_drafts: list[dict[str, Any]] = []
        deprecated_low_value: list[dict[str, Any]] = []

        for canon, members in by_canonical.items():
            validated = [m for m in members if m["status"] == SkillStatus.VALIDATED.value]
            validated.sort(
                key=lambda m: (
                    int(m.get("reuse_success_count") or 0),
                    int(m.get("invocation_count") or 0),
                    m.get("updated_at") or datetime.min.replace(tzinfo=ts.tzinfo),
                ),
                reverse=True,
            )
            seed = validated[0] if validated else None
            if seed is not None:
                # Check if seed itself has chronic low reuse success or extreme zero-use age
                rc = int(seed.get("reuse_count") or 0)
                rsc = int(seed.get("reuse_success_count") or 0)
                created = seed.get("created_at")
                if isinstance(created, datetime) and created.tzinfo is None and ts.tzinfo is not None:
                    created = created.replace(tzinfo=ts.tzinfo)
                too_old_unused = (
                    int(seed.get("invocation_count") or 0) == 0
                    and isinstance(created, datetime)
                    and (ts - created) > timedelta(days=max_zero_reuse_age_days)
                )
                poor_reuse = rc >= min_reuse_samples and (rsc / max(1, rc)) < min_reuse_success_rate
                if poor_reuse or too_old_unused:
                    reason = "low_reuse_success_rate" if poor_reuse else "stale_zero_reuse"
                    deprecated_low_value.append({"id": seed["id"], "canonical_name": canon, "reason": reason})
                    if not dry_run:
                        await self.repo.update_skill_metadata(
                            uuid.UUID(seed["id"]),
                            status=SkillStatus.DEPRECATED.value,
                            evolution_meta={"deprecated_reason": reason, "deprecated_at": ts.isoformat()},
                        )
                else:
                    kept_seeds.append({"id": seed["id"], "canonical_name": canon, "version": seed["version"]})

            for m in members:
                if seed is not None and m["id"] == seed["id"]:
                    continue
                if m["status"] == SkillStatus.VALIDATED.value and seed is not None:
                    merged_duplicates.append(
                        {"id": m["id"], "skill_key": m["skill_key"], "canonical_name": canon, "superseded_by": seed["id"]}
                    )
                    if not dry_run:
                        await self.repo.update_skill_metadata(
                            uuid.UUID(m["id"]),
                            status=SkillStatus.DEPRECATED.value,
                            canonical_name=canon,
                            superseded_by=uuid.UUID(seed["id"]),
                            evolution_meta={"merged_into_seed": seed["id"], "merged_at": ts.isoformat()},
                        )
                elif m["status"] == SkillStatus.DRAFT.value:
                    archived_drafts.append({"id": m["id"], "skill_key": m["skill_key"], "canonical_name": canon})
                    if not dry_run:
                        await self.repo.update_skill_metadata(
                            uuid.UUID(m["id"]),
                            status=SkillStatus.DEPRECATED.value,
                            canonical_name=canon,
                            superseded_by=uuid.UUID(seed["id"]) if seed else None,
                            evolution_meta={"archived_draft_at": ts.isoformat()},
                        )

        if not dry_run:
            await self.skill_registry._notify_skills_changed()

        return {
            "dry_run": dry_run,
            "total_inspected": len(all_skills),
            "canonicalized_count": canonicalized_count,
            "kept_seeds_count": len(kept_seeds),
            "merged_duplicates_count": len(merged_duplicates),
            "archived_drafts_count": len(archived_drafts),
            "deprecated_low_value_count": len(deprecated_low_value),
            "kept_seeds": kept_seeds,
            "merged_duplicates": merged_duplicates,
            "deprecated_low_value": deprecated_low_value,
        }
