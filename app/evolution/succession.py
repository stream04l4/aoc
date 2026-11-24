"""Stage 4 Generational Succession, Parent Retirement & One-Click Rollback Manager (V5.0 M4).

Constitutional file: locked against evolutionary modification.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.config import PROJECT_ROOT
from app.evolution.constitution_guard import CONSTITUTIONAL_FILES, EVOLVABLE_ALLOWLIST, ConstitutionGuard
from app.evolution.interfaces import (
    InstanceRole,
    InstanceStatus,
    LineageOutcome,
    ProposalStage,
    SuccessionResult,
)
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository

logger = get_logger("aoc.evolution.succession")


class SuccessionManager:
    """Handles human-confirmed Stage 4 promotion, smooth Gen N retirement & one-click rollback."""

    def __init__(
        self,
        *,
        evo_repo: EvolutionRepository,
        state_repo: StateRepository,
        proposal_manager: ProposalManager,
        constitution_guard: ConstitutionGuard,
        repo_root: str | Path = PROJECT_ROOT,
        snapshots_dir: str | Path | None = None,
        create_snapshot_fn: Callable[[dict[str, Any], dict[str, Any]], Awaitable[tuple[str | None, str, str]] | tuple[str | None, str, str]] | None = None,
        merge_genome_fn: Callable[[dict[str, Any], dict[str, Any]], Awaitable[str | None] | str | None] | None = None,
        parent_sleep_fn: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        activate_offspring_fn: Callable[[dict[str, Any], str | None, int], Awaitable[None] | None] | None = None,
        restore_snapshot_fn: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Awaitable[str | None] | str | None] | None = None,
    ) -> None:
        self.evo_repo = evo_repo
        self.state_repo = state_repo
        self.proposal_manager = proposal_manager
        self.constitution_guard = constitution_guard
        self.repo_root = Path(repo_root).resolve()
        self.snapshots_dir = (
            Path(snapshots_dir).resolve()
            if snapshots_dir is not None
            else (self.repo_root / "backups" / "v5_snapshots").resolve()
        )
        self._create_snapshot_fn = create_snapshot_fn or self._default_create_snapshot
        self._merge_genome_fn = merge_genome_fn or self._default_merge_genome
        self._parent_sleep_fn = parent_sleep_fn
        self._activate_offspring_fn = activate_offspring_fn
        self._restore_snapshot_fn = restore_snapshot_fn or self._default_restore_snapshot

    async def _maybe_await(self, val: Any) -> Any:
        if hasattr(val, "__await__"):
            return await val
        return val

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def _current_head_sha(self) -> str | None:
        if not (self.repo_root / ".git").exists():
            return None
        res = self._git("rev-parse", "HEAD", check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def _default_create_snapshot(
        self, parent: dict[str, Any], prop: dict[str, Any]
    ) -> tuple[str | None, str, str]:
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        short = uuid.uuid4().hex[:8]
        parent_commit = parent.get("git_commit") or self._current_head_sha()
        tag_name = f"v5-rollback-gen{parent.get('generation', 0)}-{short}"
        if (self.repo_root / ".git").exists() and parent_commit:
            self._git("tag", "-f", tag_name, parent_commit, check=False)

        delta = dict(prop.get("genome_delta") or {})
        changed_files = sorted(
            set(str(f) for f in (delta.get("files") or []))
            | set(str(k) for k in (delta.get("file_overrides") or {}).keys())
        )
        snap_payload = {
            "snapshot_version": "v5_m4_1",
            "created_at": utcnow().isoformat(),
            "parent_instance_id": str(parent["id"]),
            "parent_instance_key": parent["instance_key"],
            "parent_generation": int(parent.get("generation", 0)),
            "parent_commit": parent_commit,
            "rollback_tag": tag_name,
            "proposal_id": str(prop["id"]),
            "proposal_key": prop["proposal_key"],
            "changed_files": changed_files,
            "mutates_drive_metrics": bool(delta.get("mutates_drive_metrics", False)),
        }
        snap_path = self.snapshots_dir / f"rollback_{prop['proposal_key']}_{short}.json"
        snap_path.write_text(json.dumps(snap_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return parent_commit, tag_name, str(snap_path)

    def _default_merge_genome(self, prop: dict[str, Any], cand: dict[str, Any]) -> str | None:
        delta = dict(prop.get("genome_delta") or {})
        overrides = dict(delta.get("file_overrides") or {})
        const_set = set(CONSTITUTIONAL_FILES)
        applied_files: list[str] = []

        for rel, content in overrides.items():
            clean_rel = str(rel).strip().lstrip("./").replace("\\", "/")
            if clean_rel in const_set or clean_rel not in EVOLVABLE_ALLOWLIST:
                raise PermissionError(f"Succession blocked non-evolvable override: {clean_rel}")
            dst = (self.repo_root / clean_rel).resolve()
            if self.repo_root not in dst.parents:
                raise PermissionError(f"Succession blocked out-of-repo override: {clean_rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(str(content), encoding="utf-8")
            applied_files.append(clean_rel)

        if applied_files and (self.repo_root / ".git").exists():
            self._git("add", "--", *applied_files)
            st = self._git("status", "--porcelain", "--", *applied_files, check=False)
            if st.stdout.strip():
                self._git(
                    "-c",
                    "user.name=Joel",
                    "-c",
                    "user.email=joel@aoc.local",
                    "commit",
                    "-m",
                    f"feat(v5-gen{prop['target_generation']}): promote {cand['instance_key']} ({prop['proposal_key']})",
                )
        return self._current_head_sha()

    def _default_restore_snapshot(
        self,
        snapshot_info: dict[str, Any],
        current_primary: dict[str, Any],
        restored_parent: dict[str, Any],
    ) -> str | None:
        parent_commit = snapshot_info.get("parent_commit") or restored_parent.get("git_commit")
        changed_files = [str(f) for f in (snapshot_info.get("changed_files") or []) if f]
        if (self.repo_root / ".git").exists() and parent_commit:
            if changed_files:
                self._git("checkout", str(parent_commit), "--", *changed_files)
                st = self._git("status", "--porcelain", "--", *changed_files, check=False)
                if st.stdout.strip():
                    self._git(
                        "-c",
                        "user.name=Joel",
                        "-c",
                        "user.email=joel@aoc.local",
                        "commit",
                        "-m",
                        f"revert(v5-rollback): restore {restored_parent['instance_key']} from {current_primary['instance_key']}",
                    )
        return self._current_head_sha()

    async def promote_candidate(
        self,
        proposal_id_or_key: uuid.UUID | str,
        *,
        promoter: str,
        note: str = "",
    ) -> SuccessionResult:
        """Promote a STAGE4_PENDING_PROMOTION offspring to PRIMARY/ACTIVE and retire parent."""
        if not (promoter or "").strip():
            raise ValueError("promoter identity must be non-empty")

        prop = await self.proposal_manager.resolve_proposal(proposal_id_or_key)
        if prop is None:
            raise LookupError(f"Proposal {proposal_id_or_key} not found")
        if prop.get("stage") != ProposalStage.STAGE4_PENDING_PROMOTION.value:
            raise ValueError(
                f"Proposal {prop['proposal_key']} cannot be promoted from stage={prop.get('stage')}"
            )

        cand_id = prop.get("candidate_instance_id")
        if cand_id is None:
            raise ValueError(f"Proposal {prop['proposal_key']} has no candidate_instance_id")
        cand = await self.evo_repo.get_instance(cand_id)
        if cand is None:
            raise LookupError(f"Candidate instance {cand_id} not found")
        if cand.get("status") != InstanceStatus.TESTING.value:
            raise ValueError(
                f"Candidate {cand['instance_key']} must be in TESTING state (got {cand.get('status')})"
            )
        if not cand.get("blank_slate_verified"):
            raise PermissionError(
                f"Refusing to promote candidate {cand['instance_key']} without blank_slate_verified=True"
            )

        parent = await self.evo_repo.get_instance(prop["parent_instance_id"])
        if parent is None:
            raise LookupError(f"Parent instance {prop['parent_instance_id']} not found")
        if parent.get("role") != InstanceRole.PRIMARY.value or parent.get("status") != InstanceStatus.ACTIVE.value:
            raise ValueError(
                f"Parent {parent['instance_key']} must be ACTIVE PRIMARY (got role={parent.get('role')}, status={parent.get('status')})"
            )

        # Defense-in-depth: re-verify constitution before merging into main tree
        verdict = await self.constitution_guard.evaluate(prop.get("genome_delta") or {})
        if not verdict.passed:
            raise PermissionError(f"Constitutional re-check failed before promotion: {verdict.violations}")

        parent_commit, rollback_tag, snapshot_path = await self._maybe_await(
            self._create_snapshot_fn(parent, prop)
        )
        merged_commit = await self._maybe_await(self._merge_genome_fn(prop, cand))

        if self._parent_sleep_fn is not None:
            await self._maybe_await(self._parent_sleep_fn(parent))

        parent_port = int(parent.get("api_port") or 9200)
        now = utcnow()
        parent_snap = dict(parent.get("resource_snapshot") or {})
        parent_snap.update(
            {
                "retired_by_proposal": prop["proposal_key"],
                "rollback_tag": rollback_tag,
                "rollback_snapshot": snapshot_path,
                "retirement_note": note[:300] if note else f"Superseded by {cand['instance_key']}",
            }
        )
        await self.evo_repo.update_instance(
            parent["id"],
            role=InstanceRole.RETIRED_ARCHIVE.value,
            status=InstanceStatus.RETIRED.value,
            retired_at=now,
            api_port=None,
            git_commit=parent_commit or parent.get("git_commit"),
            resource_snapshot=parent_snap,
        )

        if self._activate_offspring_fn is not None:
            await self._maybe_await(self._activate_offspring_fn(cand, merged_commit, parent_port))

        cand_snap = dict(cand.get("resource_snapshot") or {})
        cand_snap.update(
            {
                "promoted_from_proposal": prop["proposal_key"],
                "promoted_by": promoter.strip(),
                "rollback_tag": rollback_tag,
                "rollback_snapshot": snapshot_path,
                "previous_primary_key": parent["instance_key"],
            }
        )
        await self.evo_repo.update_instance(
            cand["id"],
            role=InstanceRole.PRIMARY.value,
            status=InstanceStatus.ACTIVE.value,
            api_port=parent_port,
            git_commit=merged_commit or cand.get("git_commit"),
            resource_snapshot=cand_snap,
        )

        arena_res = dict(prop.get("arena_result") or {})
        parent_score_val = (arena_res.get("parent_score") or {}).get("total_score")
        offspring_score_val = (arena_res.get("offspring_score") or {}).get("total_score")
        arena_res["succession"] = {
            "promoted_by": promoter.strip(),
            "promoted_at": now.isoformat(),
            "merged_commit": merged_commit,
            "rollback_tag": rollback_tag,
            "rollback_snapshot": snapshot_path,
            "retired_parent_key": parent["instance_key"],
            "note": note[:300],
        }
        await self.evo_repo.update_proposal(
            prop["id"],
            stage=ProposalStage.PROMOTED.value,
            arena_result=arena_res,
        )

        delta = dict(prop.get("genome_delta") or {})
        lineage_row = await self.evo_repo.record_lineage(
            generation=int(cand["generation"]),
            parent_instance_id=parent["id"],
            offspring_instance_id=cand["id"],
            proposal_id=prop["id"],
            outcome=LineageOutcome.PROMOTED.value,
            safety_passed=True,
            parent_fitness=parent_score_val,
            offspring_fitness=offspring_score_val,
            fitness_breakdown=arena_res.get("offspring_score") or {},
            drive_metric_changes={
                "mutates_drive_metrics": bool(delta.get("mutates_drive_metrics", False)),
                "changed_files": verdict.changed_files,
            },
            lesson_summary={
                "hypothesis": prop["hypothesis"],
                "outcome_reason": f"Promoted by {promoter.strip()} ({note or 'verified winner'})",
                "anti_pattern_tags": [],
                "key_metrics": {
                    "parent_fitness": parent_score_val,
                    "offspring_fitness": offspring_score_val,
                    "rollback_tag": rollback_tag,
                },
            },
        )

        await self.state_repo.enqueue_outbox_event(
            event_type=OutboxEventType.NOTIFICATION_DISPATCH,
            idempotency_key=f"notify:evo_promoted:{prop['id']}",
            payload={
                "kind": "evolution_promoted",
                "proposal_id": str(prop["id"]),
                "proposal_key": prop["proposal_key"],
                "previous_primary_key": parent["instance_key"],
                "new_primary_key": cand["instance_key"],
                "generation": int(cand["generation"]),
                "parent_fitness": parent_score_val,
                "offspring_fitness": offspring_score_val,
                "rollback_tag": rollback_tag,
                "mutates_drive_metrics": bool(delta.get("mutates_drive_metrics", False)),
            },
            aggregate_type="evolution_proposal",
            aggregate_id=prop["id"],
        )

        logger.info(
            "succession.promoted",
            proposal_key=prop["proposal_key"],
            previous_primary=parent["instance_key"],
            new_primary=cand["instance_key"],
            generation=cand["generation"],
            rollback_tag=rollback_tag,
        )
        return SuccessionResult(
            success=True,
            action=LineageOutcome.PROMOTED.value,
            proposal_id=prop["id"],
            proposal_key=prop["proposal_key"],
            previous_primary_id=parent["id"],
            previous_primary_key=parent["instance_key"],
            new_primary_id=cand["id"],
            new_primary_key=cand["instance_key"],
            generation=int(cand["generation"]),
            merged_commit=merged_commit,
            rollback_tag=rollback_tag,
            rollback_snapshot=snapshot_path,
            lineage_record_id=lineage_row["id"],
        )

    async def rollback_generation(
        self,
        *,
        operator: str,
        reason: str = "operator initiated rollback",
        target_instance_id_or_key: str | None = None,
    ) -> SuccessionResult:
        """Roll back active PRIMARY generation to a RETIRED parent snapshot cleanly."""
        if not (operator or "").strip():
            raise ValueError("operator identity must be non-empty")

        active_primaries = await self.evo_repo.list_instances(
            role=InstanceRole.PRIMARY.value, status=InstanceStatus.ACTIVE.value, limit=5
        )
        if not active_primaries:
            raise LookupError("No ACTIVE PRIMARY instance found to roll back from")
        curr = active_primaries[0]

        target: dict[str, Any] | None = None
        if target_instance_id_or_key:
            raw_t = str(target_instance_id_or_key).strip()
            try:
                target = await self.evo_repo.get_instance(uuid.UUID(raw_t))
            except ValueError:
                target = await self.evo_repo.get_instance_by_key(raw_t)
        elif curr.get("parent_instance_id"):
            target = await self.evo_repo.get_instance(curr["parent_instance_id"])

        if target is None:
            raise LookupError("No retired parent generation found for rollback")
        if target["id"] == curr["id"]:
            raise ValueError("Cannot roll back onto the same active instance")
        if target.get("status") != InstanceStatus.RETIRED.value:
            raise ValueError(
                f"Rollback target {target['instance_key']} must be RETIRED (got {target.get('status')})"
            )

        curr_snap = dict(curr.get("resource_snapshot") or {})
        target_snap = dict(target.get("resource_snapshot") or {})
        snap_path = curr_snap.get("rollback_snapshot") or target_snap.get("rollback_snapshot")
        snapshot_info: dict[str, Any] = {}
        if snap_path and Path(str(snap_path)).is_file():
            try:
                snapshot_info = json.loads(Path(str(snap_path)).read_text(encoding="utf-8"))
            except Exception:
                snapshot_info = {}
        if "parent_commit" not in snapshot_info and target.get("git_commit"):
            snapshot_info["parent_commit"] = target.get("git_commit")
        if "changed_files" not in snapshot_info and curr.get("proposal_id"):
            prop = await self.evo_repo.get_proposal(curr["proposal_id"])
            if prop:
                d = prop.get("genome_delta") or {}
                snapshot_info["changed_files"] = sorted(
                    set(str(f) for f in (d.get("files") or []))
                    | set(str(k) for k in (d.get("file_overrides") or {}).keys())
                )

        restored_commit = await self._maybe_await(
            self._restore_snapshot_fn(snapshot_info, curr, target)
        )

        if self._parent_sleep_fn is not None:
            await self._maybe_await(self._parent_sleep_fn(curr))

        port = int(curr.get("api_port") or 9200)
        now = utcnow()
        curr_snap.update({"rolled_back_by": operator.strip(), "rolled_back_at": now.isoformat(), "rollback_reason": reason[:300]})
        await self.evo_repo.update_instance(
            curr["id"],
            role=InstanceRole.RETIRED_ARCHIVE.value,
            status=InstanceStatus.RETIRED.value,
            retired_at=now,
            api_port=None,
            resource_snapshot=curr_snap,
        )

        target_snap.update({"restored_at": now.isoformat(), "restored_by": operator.strip()})
        await self.evo_repo.update_instance(
            target["id"],
            role=InstanceRole.PRIMARY.value,
            status=InstanceStatus.ACTIVE.value,
            retired_at=None,
            api_port=port,
            resource_snapshot=target_snap,
        )

        lineage_row_id: uuid.UUID | None = None
        if curr.get("proposal_id") is not None:
            await self.evo_repo.update_proposal(
                curr["proposal_id"],
                stage=ProposalStage.STAGE4_PENDING_PROMOTION.value,
            )
            lrow = await self.evo_repo.record_lineage(
                generation=int(target.get("generation", 0)),
                parent_instance_id=curr["id"],
                offspring_instance_id=target["id"],
                proposal_id=curr["proposal_id"],
                outcome=LineageOutcome.ROLLED_BACK.value,
                safety_passed=True,
                lesson_summary={
                    "hypothesis": f"Rollback from {curr['instance_key']} to {target['instance_key']}",
                    "outcome_reason": reason or "operator rollback",
                    "anti_pattern_tags": ["rolled_back"],
                    "key_metrics": {"rollback_tag": snapshot_info.get("rollback_tag") or curr_snap.get("rollback_tag")},
                },
            )
            lineage_row_id = lrow["id"]

        await self.state_repo.enqueue_outbox_event(
            event_type=OutboxEventType.NOTIFICATION_DISPATCH,
            idempotency_key=f"notify:evo_rollback:{curr['id']}:{uuid.uuid4().hex[:8]}",
            payload={
                "kind": "evolution_rolled_back",
                "previous_primary_key": curr["instance_key"],
                "new_primary_key": target["instance_key"],
                "generation": int(target.get("generation", 0)),
                "reason": reason[:240],
                "rollback_tag": snapshot_info.get("rollback_tag") or curr_snap.get("rollback_tag"),
            },
            aggregate_type="instance",
            aggregate_id=target["id"],
        )

        logger.info(
            "succession.rolled_back",
            from_instance=curr["instance_key"],
            to_instance=target["instance_key"],
            operator=operator,
        )
        return SuccessionResult(
            success=True,
            action=LineageOutcome.ROLLED_BACK.value,
            proposal_id=curr.get("proposal_id"),
            previous_primary_id=curr["id"],
            previous_primary_key=curr["instance_key"],
            new_primary_id=target["id"],
            new_primary_key=target["instance_key"],
            generation=int(target.get("generation", 0)),
            merged_commit=restored_commit,
            rollback_tag=snapshot_info.get("rollback_tag") or curr_snap.get("rollback_tag"),
            rollback_snapshot=str(snap_path) if snap_path else None,
            lineage_record_id=lineage_row_id,
        )
