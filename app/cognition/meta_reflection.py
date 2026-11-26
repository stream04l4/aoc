"""Periodic Meta-Reflection Critic & Self-Diagnosis Manager (V5.2 M4).

Runs on pure deterministic triggers (0 LLM calls when quiet):
  1. >= interval_hours (default 12h) since last reflection (with minimum activity)
  2. >= failure_threshold (default 5) failed/abandoned goals in the last 12h (with anti-spam cooldown)

Produces at most 3 strictly bounded outlets:
  - L1 Playbook hot-evolution revision (safety-scanned & SHA-256 deduplicated via PlaybookManager)
  - L1 Skill Refactor Queue (at most 1 verified existing VALIDATED skill per reflection)
  - L2 System Upgrade Proposal Card via HumanBridge (rate-limited to <= 1 per 24h, plain Simplified Chinese)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select

from app.cognition.gateway import LLMGateway, LLMGatewayInterface
from app.cognition.interfaces import BrainInterface, MetaReflectionContext, MetaReflectionOutput
from app.observability.logging import get_logger
from app.observability.topic_diversity import TopicDiversityAnalyzer
from app.perception.interfaces import utcnow
from app.storage.models import ActionPlanRow, EventLog, SkillInvocationRow
from app.storage.repository import StateRepository

logger = get_logger("aoc.meta_reflection")

_DEFAULT_L2_OPTIONS: list[dict[str, str]] = [
    {
        "label": "采纳并生成变更 (推荐)",
        "instruction": "采纳该系统改进建议，准备对应代码变更方案供复核",
    },
    {
        "label": "延后观察",
        "instruction": "暂时延后该系统改进，继续观察一个周期",
    },
    {
        "label": "驳回并记录原因",
        "instruction": "驳回该系统改进建议，保留现有实现并记录原因",
    },
]


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        return _ensure_utc(dt)
    except ValueError:
        return None


def _normalize_l2_options(raw_options: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(raw_options, list):
        for item in raw_options:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            instr = str(item.get("instruction") or label).strip()
            if not label:
                continue
            if "其它" in label or "其他" in label or label.lower() == "other":
                continue
            out.append({"label": label[:32], "instruction": instr[:240]})
            if len(out) >= 4:
                break
    if len(out) < 2:
        return [dict(o) for o in _DEFAULT_L2_OPTIONS]
    return out


class MetaReflectionManager:
    """Rule-gated periodic self-diagnosis critic for V5.2 M4."""

    def __init__(
        self,
        *,
        repo: StateRepository,
        brain: BrainInterface,
        playbook_manager: Any = None,
        human_bridge: Any = None,
        llm_gateway: LLMGatewayInterface | None = None,
        enabled: bool = False,
        interval_hours: int = 12,
        failure_threshold: int = 5,
        l2_cooldown_hours: int = 24,
        failure_cooldown_hours: float = 2.0,
    ) -> None:
        self.repo = repo
        self.brain = brain
        self.playbook_manager = playbook_manager
        self.human_bridge = human_bridge
        self.llm_gateway = llm_gateway
        self.enabled = bool(enabled)
        self.interval_hours = max(1, int(interval_hours))
        self.failure_threshold = max(1, int(failure_threshold))
        self.l2_cooldown_hours = max(1, int(l2_cooldown_hours))
        self.failure_cooldown_hours = max(0.1, float(failure_cooldown_hours))

    async def get_state(self) -> dict[str, Any]:
        rows = await self.repo.list_world_items(kind="meta_reflection_state")
        row = next((r for r in rows if r.get("key") == "primary"), None)
        if row is None or not isinstance(row.get("value"), dict):
            return {}
        return dict(row["value"])

    async def list_pending_skill_refactors(self, limit: int = 3) -> list[dict[str, Any]]:
        rows = await self.repo.list_world_items(kind="skill_refactor_queue", status="open")
        items: list[dict[str, Any]] = []
        for r in rows:
            val = dict(r.get("value") or {}) if isinstance(r.get("value"), dict) else {}
            if not val:
                continue
            items.append(
                {
                    "queue_key": r.get("key"),
                    "skill_key": val.get("skill_key"),
                    "canonical_name": val.get("canonical_name") or val.get("skill_key"),
                    "current_version": val.get("current_version"),
                    "reason": val.get("reason", ""),
                    "target_generalization": val.get("target_generalization", ""),
                    "priority": int(val.get("priority") or 1),
                    "enqueued_at": val.get("enqueued_at") or str(r.get("created_at") or ""),
                }
            )
        items.sort(key=lambda x: (-int(x.get("priority") or 1), str(x.get("enqueued_at") or "")))
        return items[: max(0, int(limit))]

    async def mark_skill_refactor_consumed(
        self,
        skill_key_or_canonical: str,
        *,
        execution_id: str | None = None,
        status: str = "completed",
    ) -> int:
        target = str(skill_key_or_canonical or "").strip()
        if not target:
            return 0
        rows = await self.repo.list_world_items(kind="skill_refactor_queue", status="open")
        consumed = 0
        now_iso = _ensure_utc(utcnow()).isoformat()
        for r in rows:
            val = dict(r.get("value") or {}) if isinstance(r.get("value"), dict) else {}
            r_key = str(r.get("key") or "")
            if (
                r_key == target
                or r_key == f"refactor:{target}"
                or str(val.get("skill_key") or "") == target
                or str(val.get("canonical_name") or "") == target
            ):
                val["consumed_at"] = now_iso
                if execution_id:
                    val["consumed_execution_id"] = str(execution_id)
                await self.repo.upsert_world_item(
                    kind="skill_refactor_queue",
                    key=r_key,
                    value=val,
                    status=status,
                    source="skill_refactor_consumed",
                )
                consumed += 1
        return consumed

    async def should_trigger(self, now: datetime | None = None) -> tuple[bool, str, dict[str, Any]]:
        """Pure-rule gate (0 LLM calls). Returns (should_run, reason, diagnostic_metrics)."""
        cur_now = _ensure_utc(now or utcnow())
        if not self.enabled:
            return False, "disabled", {"enabled": False}

        state = await self.get_state()
        last_dt = _parse_dt(state.get("last_reflection_at"))
        lookback_hours = max(24, self.interval_hours * 2)
        recent_goals = await self.repo.list_recent_goals(
            since=cur_now - timedelta(hours=lookback_hours),
            limit=120,
        )

        fail_since = cur_now - timedelta(hours=12)
        failed_12h = 0
        earliest_goal_dt: datetime | None = None
        for g in recent_goals:
            c_dt = _parse_dt(g.get("created_at"))
            u_dt = _parse_dt(g.get("updated_at")) or c_dt
            if c_dt is not None and (earliest_goal_dt is None or c_dt < earliest_goal_dt):
                earliest_goal_dt = c_dt
            st = str(g.get("status") or "").upper()
            if st in {"FAILED", "ABANDONED"} and (u_dt is None or u_dt >= fail_since):
                failed_12h += 1

        hours_since_last = (
            round((cur_now - last_dt).total_seconds() / 3600.0, 2) if last_dt is not None else None
        )
        metrics: dict[str, Any] = {
            "enabled": True,
            "recent_goals_count": len(recent_goals),
            "failed_abandoned_12h": failed_12h,
            "failure_threshold": self.failure_threshold,
            "interval_hours": self.interval_hours,
            "last_reflection_at": state.get("last_reflection_at"),
            "hours_since_last": hours_since_last,
        }

        if failed_12h >= self.failure_threshold:
            cooldown_ok = (
                last_dt is None
                or (cur_now - last_dt).total_seconds() >= self.failure_cooldown_hours * 3600.0
            )
            if cooldown_ok:
                return True, "failure_spike", metrics
            return False, "failure_spike_cooldown", metrics

        if last_dt is not None:
            if (
                (cur_now - last_dt).total_seconds() >= self.interval_hours * 3600.0
                and len(recent_goals) >= 1
            ):
                return True, "interval_elapsed", metrics
        elif (
            len(recent_goals) >= 3
            and earliest_goal_dt is not None
            and (cur_now - earliest_goal_dt).total_seconds() >= self.interval_hours * 3600.0
        ):
            return True, "interval_elapsed", metrics

        return False, "not_due", metrics

    async def build_diagnostic_bundle(
        self,
        *,
        now: datetime | None = None,
        trigger_reason: str = "interval_elapsed",
    ) -> MetaReflectionContext:
        cur_now = _ensure_utc(now or utcnow())
        win_hours = max(24, self.interval_hours)
        since = cur_now - timedelta(hours=win_hours)

        recent_goals = await self.repo.list_recent_goals(since=since, limit=120)
        goal_stats: dict[str, int] = {
            "total": len(recent_goals),
            "achieved": 0,
            "failed": 0,
            "abandoned": 0,
            "active": 0,
        }
        failed_goals: list[dict[str, Any]] = []
        for g in recent_goals:
            st = str(g.get("status") or "").upper()
            if st == "ACHIEVED":
                goal_stats["achieved"] += 1
            elif st == "FAILED":
                goal_stats["failed"] += 1
            elif st == "ABANDONED":
                goal_stats["abandoned"] += 1
            elif st == "ACTIVE":
                goal_stats["active"] += 1
            if st in {"FAILED", "ABANDONED"} and len(failed_goals) < 10:
                failed_goals.append(
                    {
                        "title": str(g.get("title") or "")[:160],
                        "drive_type": g.get("drive_type"),
                        "status": st,
                        "related_key": g.get("related_key"),
                    }
                )

        recent_error_events: list[dict[str, Any]] = []
        execution_mode_counts: dict[str, int] = {"codex": 0, "brain_research": 0, "skill_first": 0}
        recent_invocations_7d = 0

        db = getattr(self.repo, "db", None)
        if db is not None:
            try:
                async with db.session() as s:
                    err_rows = (
                        await s.execute(
                            select(EventLog)
                            .where(
                                EventLog.created_at >= since,
                                EventLog.level.in_(["error", "warning", "ERROR", "WARNING"]),
                            )
                            .order_by(desc(EventLog.created_at))
                            .limit(10)
                        )
                    ).scalars().all()
                    for er in err_rows:
                        recent_error_events.append(
                            {
                                "event_type": er.event_type,
                                "level": er.level,
                                "message": str(er.message or "")[:200],
                            }
                        )

                    plan_rows = (
                        await s.execute(
                            select(ActionPlanRow)
                            .where(ActionPlanRow.created_at >= since)
                            .order_by(desc(ActionPlanRow.created_at))
                            .limit(120)
                        )
                    ).scalars().all()
                    for pr in plan_rows:
                        pl = pr.plan if isinstance(pr.plan, dict) else {}
                        mode = str(pl.get("execution_mode") or "codex").strip().lower()
                        if mode not in execution_mode_counts:
                            execution_mode_counts[mode] = 0
                        execution_mode_counts[mode] += 1

                    inv_rows = (
                        await s.execute(
                            select(SkillInvocationRow)
                            .where(SkillInvocationRow.created_at >= (cur_now - timedelta(days=7)))
                            .limit(200)
                        )
                    ).scalars().all()
                    recent_invocations_7d = len(inv_rows)
            except Exception as exc:
                logger.warning("meta_reflection.db_bundle_partial", error=str(exc))

        validated = await self.repo.list_skills(status="VALIDATED", limit=100)
        validated_skills_summary: list[dict[str, Any]] = []
        zero_reuse_skills: list[dict[str, Any]] = []
        for sk in validated:
            entry = {
                "skill_key": sk.get("skill_key"),
                "canonical_name": sk.get("canonical_name") or sk.get("skill_key"),
                "version": sk.get("version"),
                "reuse_count": int(sk.get("reuse_count") or 0),
                "invocation_count": int(sk.get("invocation_count") or 0),
                "description": str(sk.get("description") or "")[:140],
            }
            if len(validated_skills_summary) < 15:
                validated_skills_summary.append(entry)
            if int(sk.get("reuse_count") or 0) == 0 and len(zero_reuse_skills) < 10:
                zero_reuse_skills.append(entry)

        drafts = await self.repo.list_skills(status="DRAFT", limit=100)
        stale_cutoff = cur_now - timedelta(hours=48)
        stale_draft_count = 0
        for d in drafts:
            upd = _parse_dt(d.get("updated_at") or d.get("created_at"))
            if upd is not None and upd < stale_cutoff:
                stale_draft_count += 1

        pb_items = await self.repo.list_world_items(kind="external_playbook")
        active_playbooks: list[dict[str, Any]] = []
        for pb in pb_items[:10]:
            val = pb.get("value") if isinstance(pb.get("value"), dict) else {}
            active_playbooks.append(
                {
                    "domain": val.get("domain") or pb.get("key"),
                    "active_version": val.get("active_version"),
                    "updated_at": val.get("updated_at"),
                }
            )

        topic_summary: dict[str, Any] = {}
        if db is not None:
            try:
                td = await TopicDiversityAnalyzer(db).analyze(custom_hours=24)
                topic_summary = dict(td.get("summary") or {})
            except Exception as exc:
                logger.warning("meta_reflection.topic_diversity_degraded", error=str(exc))

        state = await self.get_state()
        last_l2_dt = _parse_dt(state.get("last_l2_proposal_at"))
        l2_allowed = (
            last_l2_dt is None
            or (cur_now - last_l2_dt).total_seconds() >= self.l2_cooldown_hours * 3600.0
        )

        return MetaReflectionContext(
            now_iso=cur_now.isoformat(),
            trigger_reason=trigger_reason,
            window_hours=win_hours,
            goal_stats={
                **goal_stats,
                "validated_skill_count": len(validated),
                "zero_reuse_skill_count": len(zero_reuse_skills),
                "recent_skill_invocations_7d": recent_invocations_7d,
            },
            failed_goals=failed_goals,
            recent_error_events=recent_error_events,
            execution_mode_counts=execution_mode_counts,
            validated_skills_summary=validated_skills_summary,
            zero_reuse_skills=zero_reuse_skills,
            stale_draft_count=stale_draft_count,
            active_playbooks=active_playbooks,
            topic_diversity_summary=topic_summary,
            l2_proposal_allowed=l2_allowed,
            previous_reflection_summary=str(state.get("last_diagnosis_summary") or "")[:300],
        )

    async def run_if_due(
        self,
        *,
        now: datetime | None = None,
        cycle_id: uuid.UUID | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Evaluate rule gate and execute bounded self-diagnosis + L1/L2 outlets if due."""
        cur_now = _ensure_utc(now or utcnow())
        if force:
            due, reason, metrics = True, "forced", {"forced": True, "enabled": self.enabled}
        else:
            due, reason, metrics = await self.should_trigger(now=cur_now)

        if not due:
            return {"triggered": False, "reason": reason, "metrics": metrics}

        ctx = await self.build_diagnostic_bundle(now=cur_now, trigger_reason=reason)
        prev_state = await self.get_state()

        if isinstance(self.llm_gateway, LLMGateway):
            with self.llm_gateway.scope(cycle_id=cycle_id, purpose="meta_reflection"):
                output: MetaReflectionOutput = await self.brain.reflect_on_system(ctx)
        else:
            output = await self.brain.reflect_on_system(ctx)

        # Outlet 1: L1 Playbook hot-evolution revision (safety-scanned & SHA-256 protected)
        playbook_outcome: dict[str, Any] = {"saved": False, "skipped_reason": "none"}
        if isinstance(output.playbook_revision, dict):
            dom = str(output.playbook_revision.get("domain") or "").strip()
            content = str(output.playbook_revision.get("content") or "").strip()
            summary = str(output.playbook_revision.get("change_summary") or "Meta-reflection update").strip()
            if self.playbook_manager is None:
                playbook_outcome = {"saved": False, "skipped_reason": "playbook_manager_unavailable"}
            elif dom and content:
                playbook_outcome = await self.playbook_manager.save_revision(
                    domain=dom,
                    content=content,
                    change_summary=summary,
                    source="meta_reflection",
                )

        # Outlet 2: L1 Skill Refactor Queue (max 1 verified existing VALIDATED skill per reflection)
        refactor_outcome: dict[str, Any] = {"enqueued": False, "skipped_reason": "none"}
        if isinstance(output.skill_refactor_item, dict):
            req_sk = str(output.skill_refactor_item.get("skill_key") or "").strip()
            req_canon = str(output.skill_refactor_item.get("canonical_name") or "").strip()
            if req_sk or req_canon:
                validated_rows = await self.repo.list_skills(status="VALIDATED", limit=200)
                matched = next(
                    (
                        r
                        for r in validated_rows
                        if (req_canon and (r.get("canonical_name") == req_canon or r.get("skill_key") == req_canon))
                        or (req_sk and (r.get("skill_key") == req_sk or r.get("canonical_name") == req_sk))
                    ),
                    None,
                )
                if matched is None:
                    refactor_outcome = {
                        "enqueued": False,
                        "skipped_reason": "unknown_or_unvalidated_skill",
                        "requested": req_canon or req_sk,
                    }
                else:
                    canon_name = str(matched.get("canonical_name") or matched.get("skill_key"))
                    queue_key = f"refactor:{canon_name}"
                    item_payload = {
                        "skill_key": matched["skill_key"],
                        "canonical_name": canon_name,
                        "skill_id": matched.get("id"),
                        "current_version": matched.get("version"),
                        "reason": str(output.skill_refactor_item.get("reason") or output.diagnosis_summary)[:400],
                        "target_generalization": str(
                            output.skill_refactor_item.get("target_generalization")
                            or output.skill_refactor_item.get("reason")
                            or "generalize schema and error handling"
                        )[:400],
                        "priority": 1,
                        "enqueued_at": cur_now.isoformat(),
                        "cycle_id": str(cycle_id) if cycle_id else None,
                    }
                    await self.repo.upsert_world_item(
                        kind="skill_refactor_queue",
                        key=queue_key,
                        value=item_payload,
                        status="open",
                        source="meta_reflection",
                    )
                    refactor_outcome = {
                        "enqueued": True,
                        "queue_key": queue_key,
                        "skill_key": matched["skill_key"],
                        "canonical_name": canon_name,
                    }

        # Outlet 3: L2 System Upgrade Proposal Card via HumanBridge (strict <= 1 per 24h)
        l2_outcome: dict[str, Any] = {"sent": False, "skipped_reason": "none"}
        new_last_l2_at = prev_state.get("last_l2_proposal_at")
        if isinstance(output.l2_upgrade_proposal, dict):
            l2_title = str(output.l2_upgrade_proposal.get("title") or "").strip()
            l2_msg = str(output.l2_upgrade_proposal.get("message") or "").strip()
            if l2_title and l2_msg:
                last_l2_dt = _parse_dt(new_last_l2_at)
                l2_allowed = (
                    last_l2_dt is None
                    or (cur_now - last_l2_dt).total_seconds() >= self.l2_cooldown_hours * 3600.0
                )
                if not l2_allowed:
                    l2_outcome = {"sent": False, "skipped_reason": "l2_24h_cooldown"}
                elif self.human_bridge is None:
                    l2_outcome = {"sent": False, "skipped_reason": "human_bridge_unavailable"}
                else:
                    rollback_plan = str(output.l2_upgrade_proposal.get("rollback_plan") or "").strip()
                    if rollback_plan and "回滚" not in l2_msg:
                        l2_msg = f"{l2_msg}\n\n回滚方案：{rollback_plan}"
                    options = _normalize_l2_options(output.l2_upgrade_proposal.get("options"))
                    topic_key = str(
                        output.l2_upgrade_proposal.get("topic_key")
                        or f"meta_l2_{cur_now.strftime('%Y%m%d_%H%M')}"
                    )[:96]
                    outreach_res = await self.human_bridge.maybe_send_outreach(
                        kind="ask_human",
                        topic_key=topic_key,
                        title=l2_title[:160],
                        message=l2_msg[:1200],
                        reason=str(output.l2_upgrade_proposal.get("reason") or output.diagnosis_summary)[:300],
                        expected_benefit=str(output.l2_upgrade_proposal.get("expected_benefit") or "")[:300],
                        options=options,
                        cycle_id=cycle_id,
                    )
                    l2_outcome = dict(outreach_res or {})
                    if outreach_res.get("sent"):
                        new_last_l2_at = cur_now.isoformat()

        reflection_count = int(prev_state.get("reflection_count") or 0) + 1
        new_state = {
            "last_reflection_at": cur_now.isoformat(),
            "last_trigger_reason": reason,
            "last_diagnosis_summary": str(output.diagnosis_summary or "")[:600],
            "last_root_causes": [str(c)[:160] for c in (output.root_causes or [])[:5]],
            "last_confidence": float(output.confidence),
            "last_l2_proposal_at": new_last_l2_at,
            "reflection_count": reflection_count,
            "last_playbook_outcome": playbook_outcome,
            "last_refactor_outcome": refactor_outcome,
            "last_l2_outcome": l2_outcome,
        }
        await self.repo.upsert_world_item(
            kind="meta_reflection_state",
            key="primary",
            value=new_state,
            status="active",
            source="meta_reflection",
        )
        await self.repo.log_decision(
            decision_type="meta_reflection",
            selected=reason,
            alternatives=["interval_elapsed", "failure_spike"],
            reason=str(output.diagnosis_summary or reason)[:500],
            payload={
                "metrics": metrics,
                "root_causes": output.root_causes,
                "confidence": output.confidence,
                "playbook_outcome": playbook_outcome,
                "refactor_outcome": refactor_outcome,
                "l2_outcome": l2_outcome,
            },
            cycle_id=cycle_id,
        )
        await self.repo.log_event(
            "meta_reflection.completed",
            message=f"Meta-reflection ({reason}): {str(output.diagnosis_summary or '')[:160]}",
            payload={
                "trigger_reason": reason,
                "playbook_saved": bool(playbook_outcome.get("saved")),
                "refactor_enqueued": bool(refactor_outcome.get("enqueued")),
                "l2_sent": bool(l2_outcome.get("sent")),
            },
            cycle_id=cycle_id,
        )
        return {
            "triggered": True,
            "reason": reason,
            "diagnosis_summary": output.diagnosis_summary,
            "root_causes": output.root_causes,
            "playbook_outcome": playbook_outcome,
            "refactor_outcome": refactor_outcome,
            "l2_outcome": l2_outcome,
            "reflection_count": reflection_count,
        }
