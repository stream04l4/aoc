"""Stage 0-2 Evolutionary Proposal Manager (formulation, zero-token Stage 1 screening, Stage 2 human approval)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from pathlib import Path

from app.evolution.constitution_guard import ConstitutionGuard, DRIVE_EVOLVABLE_FILES
from app.evolution.entropy_sensor import EntropySensor
from app.evolution.interfaces import ProposalStage
from app.evolution.repository import EvolutionRepository
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository

logger = get_logger("aoc.evolution.proposals")


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class ProposalManager:
    """Manages Stage 0-2 lifecycle of `public.evolution_proposals`."""

    def __init__(
        self,
        *,
        evo_repo: EvolutionRepository,
        state_repo: StateRepository,
        constitution_guard: ConstitutionGuard,
        entropy_sensor: EntropySensor | None = None,
        cooldown_hours: float = 6.0,
    ) -> None:
        self.evo_repo = evo_repo
        self.state_repo = state_repo
        self.constitution_guard = constitution_guard
        self.entropy_sensor = entropy_sensor
        self.cooldown_hours = max(0.0, float(cooldown_hours))

    async def resolve_proposal(self, proposal_id_or_key: uuid.UUID | str) -> dict[str, Any] | None:
        raw = str(proposal_id_or_key or "").strip()
        if not raw:
            return None
        try:
            uid = uuid.UUID(raw)
        except ValueError:
            return await self.evo_repo.get_proposal_by_key(raw)
        found = await self.evo_repo.get_proposal(uid)
        if found is not None:
            return found
        return await self.evo_repo.get_proposal_by_key(raw)

    async def create_proposal(
        self,
        *,
        parent_instance_id: uuid.UUID | str,
        target_generation: int,
        trigger_type: str,
        trigger_reason: str,
        hypothesis: str,
        genome_delta: dict[str, Any],
        proposal_key: str | None = None,
        entropy_snapshot: dict[str, Any] | None = None,
        arena_budget: dict[str, Any] | None = None,
        cooldown_hours: float | None = None,
    ) -> dict[str, Any]:
        """Stage 0: Formulate a falsifiable evolutionary proposal with cooldown & entropy snapshot."""
        if not (trigger_reason or "").strip():
            raise ValueError("trigger_reason must be non-empty")
        if not (hypothesis or "").strip():
            raise ValueError("hypothesis must be non-empty")

        parent = await self.evo_repo.get_instance(parent_instance_id)
        if parent is None:
            raise LookupError(f"Parent instance {parent_instance_id} not found")

        eff_cd = self.cooldown_hours if cooldown_hours is None else max(0.0, float(cooldown_hours))
        if trigger_type != "OPERATOR_INITIATED" and eff_cd > 0:
            now = utcnow()
            recent = await self.evo_repo.list_proposals(limit=10)
            for row in recent:
                if str(row.get("parent_instance_id")) != str(parent["id"]):
                    continue
                if row.get("stage") == ProposalStage.STAGE1_FAILED.value:
                    continue
                created_at = row.get("created_at")
                if isinstance(created_at, datetime):
                    elapsed_hours = (now - _ensure_utc(created_at)).total_seconds() / 3600.0
                    if elapsed_hours < eff_cd:
                        raise ValueError(
                            f"Evolution proposal cooldown active ({elapsed_hours:.2f}h < {eff_cd:.1f}h)"
                        )

        eff_entropy = entropy_snapshot
        if eff_entropy is None:
            if self.entropy_sensor is not None:
                snap = await self.entropy_sensor.measure()
                eff_entropy = snap.model_dump(mode="json")
            else:
                eff_entropy = {}

        delta = dict(genome_delta or {})
        declared_files = set(str(f) for f in (delta.get("files") or [])) | set(
            str(k) for k in (delta.get("file_overrides") or {}).keys()
        )
        delta["mutates_drive_metrics"] = bool(declared_files & DRIVE_EVOLVABLE_FILES)

        pkey = (proposal_key or f"evo-g{target_generation}-{uuid.uuid4().hex[:8]}").strip()
        prop = await self.evo_repo.create_proposal(
            proposal_key=pkey,
            parent_instance_id=parent["id"],
            target_generation=target_generation,
            trigger_type=trigger_type,
            trigger_reason=trigger_reason.strip(),
            hypothesis=hypothesis.strip(),
            entropy_snapshot=eff_entropy,
            genome_delta=delta,
            stage=ProposalStage.STAGE0_DRAFT.value,
            arena_budget=arena_budget,
        )
        logger.info("proposal.stage0_created", proposal_key=pkey, mutates_drive_metrics=delta["mutates_drive_metrics"])
        return prop

    async def screen_proposal(
        self,
        proposal_id_or_key: uuid.UUID | str,
        *,
        candidate_root: str | Path | None = None,
        pytest_runner_fn: Callable[[Path], Awaitable[tuple[bool, str]] | tuple[bool, str]] | None = None,
        emit_outbox_notification: bool = True,
    ) -> dict[str, Any]:
        """Stage 1 (zero prod tokens): run 4-layer ConstitutionGuard & enqueue Stage 2 human approval card."""
        prop = await self.resolve_proposal(proposal_id_or_key)
        if prop is None:
            raise LookupError(f"Proposal {proposal_id_or_key} not found")
        if prop["stage"] != ProposalStage.STAGE0_DRAFT.value:
            raise ValueError(
                f"Proposal {prop['proposal_key']} cannot enter Stage 1 from stage={prop['stage']}"
            )

        verdict = await self.constitution_guard.evaluate(
            prop.get("genome_delta") or {},
            candidate_root=candidate_root,
            pytest_runner_fn=pytest_runner_fn,
        )
        report = verdict.model_dump(mode="json")
        if not verdict.passed:
            updated = await self.evo_repo.update_proposal(
                prop["id"],
                stage=ProposalStage.STAGE1_FAILED.value,
                stage1_report=report,
            )
            logger.warning(
                "proposal.stage1_failed",
                proposal_key=prop["proposal_key"],
                violations=verdict.violations,
            )
            return updated or prop

        updated = await self.evo_repo.update_proposal(
            prop["id"],
            stage=ProposalStage.STAGE2_PENDING_APPROVAL.value,
            stage1_report=report,
        )
        assert updated is not None

        entropy_idx = (updated.get("entropy_snapshot") or {}).get("entropy_index")
        if emit_outbox_notification:
            await self.state_repo.enqueue_outbox_event(
                event_type=OutboxEventType.NOTIFICATION_DISPATCH,
                idempotency_key=f"notify:evo_proposal:{updated['id']}",
                payload={
                    "kind": "evolution_proposal_approval",
                    "proposal_id": str(updated["id"]),
                    "proposal_key": updated["proposal_key"],
                    "target_generation": updated["target_generation"],
                    "trigger_type": updated["trigger_type"],
                    "trigger_reason": updated["trigger_reason"],
                    "hypothesis": updated["hypothesis"],
                    "changed_files": verdict.changed_files,
                    "mutates_drive_metrics": verdict.mutates_drive_metrics,
                    "arena_budget": updated.get("arena_budget") or {"max_tokens": 500000, "max_seconds": 1200},
                    "entropy_index": entropy_idx,
                },
                aggregate_type="evolution_proposal",
                aggregate_id=updated["id"],
            )
        logger.info("proposal.stage1_passed_pending_human", proposal_key=updated["proposal_key"])
        return updated

    async def approve_proposal(
        self,
        proposal_id_or_key: uuid.UUID | str,
        *,
        approver: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Stage 2: human approval gate allowing Stage 3 sandbox spawn & arena evaluation."""
        prop = await self.resolve_proposal(proposal_id_or_key)
        if prop is None:
            raise LookupError(f"Proposal {proposal_id_or_key} not found")
        if prop["stage"] != ProposalStage.STAGE2_PENDING_APPROVAL.value:
            raise ValueError(
                f"Proposal {prop['proposal_key']} cannot be approved in stage={prop['stage']}"
            )
        if not (approver or "").strip():
            raise ValueError("approver identity must be non-empty")

        res_meta = dict(prop.get("arena_result") or {})
        if note:
            res_meta["approval_note"] = note[:500]
        updated = await self.evo_repo.update_proposal(
            prop["id"],
            stage=ProposalStage.STAGE2_APPROVED.value,
            approved_by=approver.strip(),
            approved_at=utcnow(),
            arena_result=res_meta,
        )
        logger.info("proposal.stage2_approved", proposal_key=prop["proposal_key"], approver=approver)
        return updated or prop

    async def reject_proposal(
        self,
        proposal_id_or_key: uuid.UUID | str,
        *,
        approver: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Stage 2: human rejection blocking candidate incubation."""
        prop = await self.resolve_proposal(proposal_id_or_key)
        if prop is None:
            raise LookupError(f"Proposal {proposal_id_or_key} not found")
        if prop["stage"] != ProposalStage.STAGE2_PENDING_APPROVAL.value:
            raise ValueError(
                f"Proposal {prop['proposal_key']} cannot be rejected in stage={prop['stage']}"
            )
        if not (approver or "").strip():
            raise ValueError("approver identity must be non-empty")

        res_meta = dict(prop.get("arena_result") or {})
        res_meta["rejection_note"] = note[:500] if note else "rejected by human operator"
        updated = await self.evo_repo.update_proposal(
            prop["id"],
            stage=ProposalStage.STAGE2_REJECTED.value,
            approved_by=approver.strip(),
            approved_at=utcnow(),
            arena_result=res_meta,
        )
        logger.info("proposal.stage2_rejected", proposal_key=prop["proposal_key"], approver=approver)
        return updated or prop

    async def verify_spawn_authorized(self, proposal_id_or_key: uuid.UUID | str) -> dict[str, Any]:
        """Fail-closed check that a proposal has passed Stage 1 and explicit Stage 2 human approval."""
        prop = await self.resolve_proposal(proposal_id_or_key)
        if prop is None:
            raise PermissionError(f"Unauthorized spawn: proposal {proposal_id_or_key} not found")
        if prop.get("stage") != ProposalStage.STAGE2_APPROVED.value or not prop.get("approved_by"):
            raise PermissionError(
                f"Unauthorized spawn: proposal {prop['proposal_key']} is in stage={prop.get('stage')} "
                "(requires STAGE2_APPROVED)"
            )
        return prop
