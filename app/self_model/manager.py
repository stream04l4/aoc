"""Dynamic Evidence-Driven SelfModelManager (V2.0 P1-7, Task Book Section 5.8).

Guarantees:
  - 100% of capability maturity changes above AVAILABLE are traceable to immutable
    rows in `capability_evidence(profile_id, execution_id)`.
  - Static external Agent capability declarations stay strictly in DECLARED / AVAILABLE
    and NEVER count toward Reproduction capability coverage.
  - Beta-Binomial posterior (alpha, beta, estimated_success_rate, confidence_low/high)
    with conservative default prior (alpha_0=1.0, beta_0=1.0) and minimum sample guard
    (sample_count < minimum_samples NEVER promotes to VALIDATED purely from runs).
  - Every material Self Model update writes a monotonically versioned snapshot to
    `self_model_versions` and enqueues Outbox `self_model.sync_letta` to dynamically
    project into Letta's `self_model` Core Block.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.memory.interfaces import MemoryInterface
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.self_model.interfaces import (
    CapabilityMaturity,
    SelfModelConfig,
    SelfModelInterface,
    SkillStatus,
    compute_beta_binomial_stats,
)
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository

logger = get_logger("aoc.self_model.manager")

ACQUIRED_STATUSES = {CapabilityMaturity.ACQUIRED.value, CapabilityMaturity.VALIDATED.value}


class SelfModelManager(SelfModelInterface):
    def __init__(
        self,
        *,
        organism_id: uuid.UUID,
        repo: StateRepository,
        config: SelfModelConfig | dict[str, Any] | None = None,
        memory_backend: MemoryInterface | None = None,
        skill_registry: Any = None,
    ) -> None:
        self.organism_id = organism_id
        self.repo = repo
        if isinstance(config, SelfModelConfig):
            self.cfg = config
        elif isinstance(config, dict):
            self.cfg = SelfModelConfig.model_validate(config)
        else:
            self.cfg = SelfModelConfig()
        self.memory_backend = memory_backend
        self.skill_registry = skill_registry

    async def sync_declared_capabilities(
        self, agent_capabilities: list[tuple[str, list[str], bool]]
    ) -> int:
        """Record external Agent static capabilities ONLY as DECLARED / AVAILABLE.

        Never downgrades an already ACQUIRED / VALIDATED capability back to AVAILABLE,
        and NEVER promotes a DECLARED/AVAILABLE capability to ACQUIRED.
        """
        cap_health: dict[str, bool] = {}
        for _agent_id, caps, is_healthy in agent_capabilities:
            for c in caps:
                key = str(c).strip()
                if not key:
                    continue
                cap_health[key] = cap_health.get(key, False) or bool(is_healthy)

        updated = 0
        prior = compute_beta_binomial_stats(
            alpha_prior=self.cfg.alpha_prior,
            beta_prior=self.cfg.beta_prior,
            success_weight=0.0,
            failure_weight=0.0,
            sample_count=0,
            confidence_level=self.cfg.confidence_level,
        )
        for key, healthy in sorted(cap_health.items()):
            existing = await self.repo.get_capability_profile(self.organism_id, key)
            target_static = CapabilityMaturity.AVAILABLE.value if healthy else CapabilityMaturity.DECLARED.value
            if existing is None:
                await self.repo.upsert_capability_profile(
                    organism_id=self.organism_id,
                    capability_key=key,
                    maturity_status=target_static,
                    alpha=prior.alpha,
                    beta=prior.beta,
                    sample_count=0,
                    estimated_success_rate=prior.estimated_success_rate,
                    confidence_low=prior.confidence_low,
                    confidence_high=prior.confidence_high,
                )
                updated += 1
            elif existing["maturity_status"] in {
                CapabilityMaturity.DECLARED.value,
                CapabilityMaturity.AVAILABLE.value,
            } and existing["maturity_status"] != target_static:
                await self.repo.upsert_capability_profile(
                    organism_id=self.organism_id,
                    capability_key=key,
                    maturity_status=target_static,
                    alpha=existing["alpha"],
                    beta=existing["beta"],
                    sample_count=existing["sample_count"],
                    estimated_success_rate=existing["estimated_success_rate"],
                    confidence_low=existing["confidence_low"],
                    confidence_high=existing["confidence_high"],
                )
                updated += 1
        return updated

    def _compute_maturity(
        self,
        *,
        sample_count: int,
        verified_successes: int,
        estimated_success_rate: float,
        confidence_low: float,
        validated_skills_count: int,
        current_status: str | None = None,
    ) -> str:
        """Deterministic 5-state maturity transition rule (Task Book 5.8)."""
        # 1. Validated skill backing promotes immediately to VALIDATED
        if validated_skills_count >= 1:
            return CapabilityMaturity.VALIDATED.value

        # 2. Statistical validation: must meet minimum_samples AND confidence_threshold AND >=1 success
        if (
            verified_successes >= 1
            and sample_count >= self.cfg.minimum_samples
            and confidence_low >= self.cfg.confidence_threshold
        ):
            return CapabilityMaturity.VALIDATED.value

        # 3. Repeated empirical failures -> DEPRECATED
        if (
            sample_count >= self.cfg.deprecate_after_failures
            and estimated_success_rate <= self.cfg.deprecate_max_success_rate
        ):
            return CapabilityMaturity.DEPRECATED.value

        # 4. At least one verified AOC success -> ACQUIRED
        if verified_successes >= 1:
            return CapabilityMaturity.ACQUIRED.value

        # 5. Zero AOC verified successes: stay strictly at AVAILABLE or DECLARED
        if current_status == CapabilityMaturity.AVAILABLE.value:
            return CapabilityMaturity.AVAILABLE.value
        return CapabilityMaturity.DECLARED.value

    async def _recompute_profile_from_evidence(
        self,
        *,
        profile_id: uuid.UUID,
        capability_key: str,
        current_status: str,
        session: Any = None,
    ) -> dict[str, Any]:
        """Recompute Beta-Binomial posterior strictly from immutable `capability_evidence` rows."""
        ev_rows = await self.repo.list_capability_evidence(profile_id, limit=5000, session=session)
        succ_w = 0.0
        fail_w = 0.0
        verified_successes = 0
        for ev in ev_rows:
            w = max(0.0, float(ev.get("weight", 1.0)))
            if ev["outcome"] == "verified_success":
                succ_w += w
                verified_successes += 1
            else:
                fail_w += w
        sample_count = len(ev_rows)

        post = compute_beta_binomial_stats(
            alpha_prior=self.cfg.alpha_prior,
            beta_prior=self.cfg.beta_prior,
            success_weight=succ_w,
            failure_weight=fail_w,
            sample_count=sample_count,
            confidence_level=self.cfg.confidence_level,
        )

        validated_skills_count = await self._count_intact_validated_skills_for_cap(
            capability_key, session=session
        )
        new_status = self._compute_maturity(
            sample_count=post.sample_count,
            verified_successes=verified_successes,
            estimated_success_rate=post.estimated_success_rate,
            confidence_low=post.confidence_low,
            validated_skills_count=validated_skills_count,
            current_status=current_status,
        )

        updated = await self.repo.upsert_capability_profile(
            organism_id=self.organism_id,
            capability_key=capability_key,
            maturity_status=new_status,
            alpha=post.alpha,
            beta=post.beta,
            sample_count=post.sample_count,
            estimated_success_rate=post.estimated_success_rate,
            confidence_low=post.confidence_low,
            confidence_high=post.confidence_high,
            session=session,
        )
        updated["validated_skill_count"] = validated_skills_count
        updated["evidence_count"] = sample_count
        return updated

    async def _count_intact_validated_skills_for_cap(
        self, capability_key: str, *, session: Any = None
    ) -> int:
        if self.skill_registry is not None:
            return await self.skill_registry.count_validated_skills(
                capability_key=capability_key, verify_integrity=True, session=session
            )
        skills = await self.repo.list_skills(
            status=SkillStatus.VALIDATED.value, capability_key=capability_key, session=session
        )
        return len(skills)

    async def record_execution_evidence(
        self,
        *,
        capability_key: str,
        execution_id: uuid.UUID | str,
        task_id: uuid.UUID | str,
        verified_success: bool,
        verification_summary: str = "",
        agent_id: str | None = None,
        wall_seconds: float | None = None,
        weight: float = 1.0,
        session: Any = None,
    ) -> dict[str, Any]:
        """Record one verified execution outcome into `capability_evidence` and update Self Model.

        Strictly idempotent on (profile_id, execution_id): duplicate calls do not double-count.
        Can run inside an existing `TaskFinalizer` transaction via `session`.
        """
        cap_key = (str(capability_key or "general_task").strip() or "general_task")[:120]
        exec_uuid = uuid.UUID(str(execution_id))
        task_uuid = uuid.UUID(str(task_id))

        existing = await self.repo.get_capability_profile(self.organism_id, cap_key, session=session)
        if existing is None:
            prior = compute_beta_binomial_stats(
                alpha_prior=self.cfg.alpha_prior,
                beta_prior=self.cfg.beta_prior,
                confidence_level=self.cfg.confidence_level,
            )
            existing = await self.repo.upsert_capability_profile(
                organism_id=self.organism_id,
                capability_key=cap_key,
                maturity_status=CapabilityMaturity.DECLARED.value,
                alpha=prior.alpha,
                beta=prior.beta,
                sample_count=0,
                estimated_success_rate=prior.estimated_success_rate,
                confidence_low=prior.confidence_low,
                confidence_high=prior.confidence_high,
                session=session,
            )

        profile_uuid = uuid.UUID(existing["id"])
        outcome = "verified_success" if verified_success else "verified_failure"
        inserted, ev_id = await self.repo.add_capability_evidence(
            profile_id=profile_uuid,
            execution_id=exec_uuid,
            task_id=task_uuid,
            outcome=outcome,
            weight=weight,
            verification_summary=verification_summary[:1000],
            session=session,
        )

        if not inserted:
            logger.info(
                "self_model.evidence_idempotent_noop",
                capability_key=cap_key,
                execution_id=str(exec_uuid),
            )
            return {
                "inserted": False,
                "evidence_id": str(ev_id) if ev_id else None,
                "profile": existing,
                "version": None,
            }

        profile = await self._recompute_profile_from_evidence(
            profile_id=profile_uuid,
            capability_key=cap_key,
            current_status=existing["maturity_status"],
            session=session,
        )

        # Persist agent empirical routing stats into agent_registry.stats (0007 extension)
        if agent_id:
            await self._update_agent_empirical_stats(
                agent_id=agent_id,
                verified_success=verified_success,
                wall_seconds=wall_seconds,
                session=session,
            )

        version_row = await self.publish_snapshot(
            trigger_execution_id=exec_uuid,
            reason=f"evidence:{cap_key}:{outcome}",
            session=session,
        )
        return {
            "inserted": True,
            "evidence_id": str(ev_id) if ev_id else None,
            "profile": profile,
            "version": version_row,
        }

    async def on_skill_validated(
        self,
        *,
        capability_key: str,
        skill_id: uuid.UUID | str,
        trigger_execution_id: uuid.UUID | str | None = None,
        session: Any = None,
    ) -> dict[str, Any]:
        """Re-evaluate capability profile after a Skill passes isolated validation tests."""
        cap_key = str(capability_key).strip()
        existing = await self.repo.get_capability_profile(self.organism_id, cap_key, session=session)
        if existing is None:
            prior = compute_beta_binomial_stats(
                alpha_prior=self.cfg.alpha_prior,
                beta_prior=self.cfg.beta_prior,
                confidence_level=self.cfg.confidence_level,
            )
            existing = await self.repo.upsert_capability_profile(
                organism_id=self.organism_id,
                capability_key=cap_key,
                maturity_status=CapabilityMaturity.DECLARED.value,
                alpha=prior.alpha,
                beta=prior.beta,
                sample_count=0,
                estimated_success_rate=prior.estimated_success_rate,
                confidence_low=prior.confidence_low,
                confidence_high=prior.confidence_high,
                session=session,
            )

        profile = await self._recompute_profile_from_evidence(
            profile_id=uuid.UUID(existing["id"]),
            capability_key=cap_key,
            current_status=existing["maturity_status"],
            session=session,
        )
        trig = uuid.UUID(str(trigger_execution_id)) if trigger_execution_id else None
        version = await self.publish_snapshot(
            trigger_execution_id=trig,
            reason=f"skill_validated:{cap_key}:{skill_id}",
            session=session,
        )
        return {"profile": profile, "version": version}

    async def _update_agent_empirical_stats(
        self,
        *,
        agent_id: str,
        verified_success: bool,
        wall_seconds: float | None = None,
        session: Any = None,
    ) -> None:
        agents = await self.repo.list_agents()
        current = next((a.get("stats") or {} for a in agents if a["agent_id"] == agent_id), None)
        if current is None:
            return
        ok = int(current.get("verified_successes", 0)) + (1 if verified_success else 0)
        fail = int(current.get("verified_failures", 0)) + (0 if verified_success else 1)
        total = ok + fail
        wall_n = int(current.get("wall_samples", 0))
        wall_sum = float(current.get("wall_seconds_sum", 0.0))
        if wall_seconds is not None and wall_seconds >= 0:
            wall_n += 1
            wall_sum += float(wall_seconds)
        new_stats = {
            "verified_successes": ok,
            "verified_failures": fail,
            "total_executions": total,
            "empirical_success_rate": round(ok / total, 4) if total > 0 else 0.5,
            "wall_samples": wall_n,
            "wall_seconds_sum": round(wall_sum, 3),
            "avg_wall_seconds": round(wall_sum / wall_n, 3) if wall_n > 0 else None,
            "last_updated_at": utcnow().isoformat(),
        }
        await self.repo.update_agent_stats(agent_id, new_stats, session=session)

    async def publish_snapshot(
        self,
        *,
        trigger_execution_id: uuid.UUID | None = None,
        reason: str = "update",
        session: Any = None,
    ) -> dict[str, Any]:
        """Build and persist a new `self_model_versions` row and enqueue Letta Core Block sync."""
        profiles = await self.repo.list_capability_profiles(self.organism_id, session=session)
        skills = await self.repo.list_skills(status=SkillStatus.VALIDATED.value, session=session)
        intact_skills: list[dict[str, Any]] = []
        for sk in skills:
            if self.skill_registry is not None:
                ok, _ = await self.skill_registry.verify_skill_integrity(sk["id"], session=session)
                if not ok:
                    continue
            intact_skills.append(
                {
                    "id": sk["id"],
                    "skill_key": sk["skill_key"],
                    "version": sk["version"],
                    "capability_key": sk["capability_key"],
                    "permission_level": sk["permission_level"],
                    "artifact_checksum": sk["artifact_checksum"],
                }
            )

        acquired_caps = [p["capability_key"] for p in profiles if p["maturity_status"] in ACQUIRED_STATUSES]
        validated_caps = [
            p["capability_key"] for p in profiles if p["maturity_status"] == CapabilityMaturity.VALIDATED.value
        ]
        declared_only = [
            p["capability_key"]
            for p in profiles
            if p["maturity_status"] in {CapabilityMaturity.DECLARED.value, CapabilityMaturity.AVAILABLE.value}
        ]

        cap_lines = [
            (
                f"- {p['capability_key']}: {p['maturity_status']} "
                f"(p={p['estimated_success_rate']:.2f}, 90%CI=[{p['confidence_low']:.2f},{p['confidence_high']:.2f}], "
                f"n={p['sample_count']})"
            )
            for p in profiles
        ]
        skill_lines = [f"- {s['skill_key']}@{s['version']} ({s['capability_key']})" for s in intact_skills]
        summary_text = (
            f"AOC Dynamic Self Model (trigger={reason})\n"
            f"Validated capabilities ({len(validated_caps)}): {', '.join(validated_caps) or 'none'}\n"
            f"Acquired capabilities ({len(acquired_caps)}): {', '.join(acquired_caps) or 'none'}\n"
            f"External declared/available only ({len(declared_only)}, NOT self-acquired): "
            f"{', '.join(declared_only) or 'none'}\n"
            f"Validated skills ({len(intact_skills)}):\n"
            + ("\n".join(skill_lines) if skill_lines else "- none")
            + "\nCapability Posteriors:\n"
            + ("\n".join(cap_lines) if cap_lines else "- none")
        )[:3900]

        snapshot_payload = {
            "reason": reason,
            "acquired_capabilities": acquired_caps,
            "validated_capabilities": validated_caps,
            "declared_only_capabilities": declared_only,
            "validated_skills": intact_skills,
            "capabilities": profiles,
            "generated_at": utcnow().isoformat(),
        }

        version_row = await self.repo.create_self_model_version(
            organism_id=self.organism_id,
            summary_text=summary_text,
            profile_snapshot=snapshot_payload,
            trigger_execution_id=trigger_execution_id,
            session=session,
        )

        if self.cfg.sync_letta_via_outbox:
            await self.repo.enqueue_outbox_event(
                event_type=OutboxEventType.SELF_MODEL_SYNC_LETTA,
                idempotency_key=f"self_model_letta:{version_row['id']}",
                payload={
                    "version_id": version_row["id"],
                    "version_number": version_row["version_number"],
                    "organism_id": str(self.organism_id),
                },
                aggregate_type="self_model",
                aggregate_id=uuid.UUID(version_row["id"]),
                session=session,
            )

        return version_row

    async def sync_version_to_letta(self, version_id: uuid.UUID | str) -> dict[str, Any]:
        """Outbox handler: project a `self_model_versions` snapshot to Letta `self_model` Core Block."""
        v_uuid = uuid.UUID(str(version_id))
        row = await self.repo.get_self_model_version(v_uuid)
        if row is None:
            return {"synced": False, "reason": "version_not_found"}
        if self.memory_backend is None:
            return {"synced": False, "skipped": True, "reason": "no_memory_backend"}
        ok = await self.memory_backend.update_core_block("self_model", row["summary_text"])
        if not ok:
            raise RuntimeError(f"Failed to update Letta self_model core block for version {row['version_number']}")
        await self.repo.mark_self_model_letta_synced(v_uuid)
        logger.info("self_model.letta_synced", version_number=row["version_number"], version_id=str(v_uuid))
        return {"synced": True, "version_id": str(v_uuid), "version_number": row["version_number"]}

    async def get_latest_snapshot(self) -> dict[str, Any]:
        """Return latest persisted `self_model_versions` snapshot (creating v1 baseline if none)."""
        latest = await self.repo.get_latest_self_model_version(self.organism_id)
        if latest is None:
            latest = await self.publish_snapshot(reason="initial_baseline")
        return latest

    async def list_history(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.repo.list_self_model_versions(self.organism_id, limit=limit)

    async def list_capabilities(self) -> list[dict[str, Any]]:
        profiles = await self.repo.list_capability_profiles(self.organism_id)
        out: list[dict[str, Any]] = []
        for p in profiles:
            pid = uuid.UUID(p["id"])
            evs = await self.repo.list_capability_evidence(pid, limit=20)
            v_skills = await self._count_intact_validated_skills_for_cap(p["capability_key"])
            out.append(
                {
                    **p,
                    "evidence_count": p["sample_count"],
                    "validated_skill_count": v_skills,
                    "recent_evidence": evs[:5],
                }
            )
        return out

    async def get_evidence_backed_capabilities(self) -> tuple[list[str], list[str]]:
        """Return (acquired_or_validated_keys, validated_only_keys) strictly from `capability_profiles`.

        DECLARED, AVAILABLE, and DEPRECATED capabilities are NEVER included.
        """
        profiles = await self.repo.list_capability_profiles(self.organism_id)
        acquired: list[str] = []
        validated: list[str] = []
        for p in profiles:
            st = p["maturity_status"]
            if st == CapabilityMaturity.VALIDATED.value:
                # Re-verify if capability was only validated via a skill that got tampered
                if p["sample_count"] == 0 and self.skill_registry is not None:
                    v_count = await self.skill_registry.count_validated_skills(
                        capability_key=p["capability_key"], verify_integrity=True
                    )
                    if v_count == 0:
                        continue
                acquired.append(p["capability_key"])
                validated.append(p["capability_key"])
            elif st == CapabilityMaturity.ACQUIRED.value:
                acquired.append(p["capability_key"])
        return acquired, validated
