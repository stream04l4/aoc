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

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select

from app.cognition.gateway import LLMGateway, LLMGatewayInterface
from app.cognition.interfaces import BrainInterface, MetaReflectionContext, MetaReflectionOutput
from app.drives.interfaces import clamp01
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
        attribution_enabled: bool = False,
        attribution_min_hours: float = 6.0,
        attribution_min_cycles: int = 5,
        regression_delta: float = 0.08,
        l2_bridge_enabled: bool = False,
        self_edit_manager: Any = None,
        self_edit_enabled: bool = False,
        self_edit_auto_approve_enabled: bool = False,
        self_edit_policy_mode: str = "strict",
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
        self.attribution_enabled = bool(attribution_enabled)
        self.attribution_min_hours = max(0.0, float(attribution_min_hours))
        self.attribution_min_cycles = max(1, int(attribution_min_cycles))
        self.regression_delta = max(0.01, float(regression_delta))
        self.l2_bridge_enabled = bool(l2_bridge_enabled)
        self.self_edit_manager = self_edit_manager
        self.self_edit_enabled = bool(self_edit_enabled)
        self.self_edit_auto_approve_enabled = bool(self_edit_auto_approve_enabled)
        self.self_edit_policy_mode = str(self_edit_policy_mode or "strict").strip().lower() or "strict"

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

    async def _collect_failure_cluster_telemetry(
        self,
        *,
        since: datetime,
        total_goals: int,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Collect `llm.schema_retry` & `orchestrator.attempt_failed` event clusters from DB or repo (V5.3 M3)."""
        events_window: list[dict[str, Any]] = []
        recent_error_events: list[dict[str, Any]] = []
        db = getattr(self.repo, "db", None)
        if db is not None:
            try:
                async with db.session() as s:
                    rows = (
                        await s.execute(
                            select(EventLog)
                            .where(EventLog.created_at >= since)
                            .order_by(desc(EventLog.created_at))
                            .limit(300)
                        )
                    ).scalars().all()
                    for er in rows:
                        events_window.append(
                            {
                                "event_type": er.event_type,
                                "level": er.level,
                                "message": str(er.message or "")[:200],
                                "payload": er.payload if isinstance(er.payload, dict) else {},
                            }
                        )
            except Exception as exc:
                logger.warning("meta_reflection.event_telemetry_db_degraded", error=str(exc))
        elif hasattr(self.repo, "list_events"):
            try:
                raw_evs = await self.repo.list_events(limit=200)
                for ev in raw_evs:
                    ev_dt = _parse_dt(ev.get("at") or ev.get("created_at"))
                    if ev_dt is not None and ev_dt < since:
                        continue
                    events_window.append(
                        {
                            "event_type": ev.get("event_type"),
                            "level": ev.get("level", "info"),
                            "message": str(ev.get("message") or "")[:200],
                            "payload": ev.get("payload") if isinstance(ev.get("payload"), dict) else {},
                        }
                    )
            except Exception as exc:
                logger.warning("meta_reflection.event_telemetry_repo_degraded", error=str(exc))

        retry_total = 0
        retry_by_cat: dict[str, int] = {}
        retry_by_op: dict[str, int] = {}
        attempt_total = 0
        attempt_by_bucket: dict[str, int] = {}
        attempt_by_agent: dict[str, int] = {}

        for ev in events_window:
            etype = str(ev.get("event_type") or "")
            lvl = str(ev.get("level") or "").lower()
            pl = ev.get("payload") or {}
            if lvl in {"error", "warning"} and len(recent_error_events) < 12:
                recent_error_events.append(
                    {
                        "event_type": etype,
                        "level": ev.get("level"),
                        "message": ev.get("message", ""),
                    }
                )
            if etype == "llm.schema_retry":
                retry_total += 1
                cat = str(pl.get("category") or "validation_error")
                op = str(pl.get("operation") or "unknown")
                retry_by_cat[cat] = retry_by_cat.get(cat, 0) + 1
                retry_by_op[op] = retry_by_op.get(op, 0) + 1
            elif etype == "orchestrator.attempt_failed":
                attempt_total += 1
                bkt = str(pl.get("failure_bucket") or "other")
                ag = str(pl.get("agent_id") or "unknown")
                attempt_by_bucket[bkt] = attempt_by_bucket.get(bkt, 0) + 1
                attempt_by_agent[ag] = attempt_by_agent.get(ag, 0) + 1

        llm_stats = {
            "total_24h": retry_total,
            "by_category": retry_by_cat,
            "by_operation": retry_by_op,
            "retry_rate": round(retry_total / max(1, int(total_goals)), 4),
        }
        attempt_clusters = {
            "total_24h": attempt_total,
            "by_bucket": attempt_by_bucket,
            "by_agent": attempt_by_agent,
        }
        return llm_stats, attempt_clusters, recent_error_events

    @staticmethod
    def _build_metrics_snapshot(
        *,
        goal_stats: dict[str, Any],
        execution_mode_counts: dict[str, int],
        llm_schema_retry_stats: dict[str, Any],
        attempt_failure_clusters: dict[str, Any],
        validated_count: int,
        zero_reuse_count: int,
        now_dt: datetime,
    ) -> dict[str, Any]:
        ach = int(goal_stats.get("achieved") or 0)
        fld = int(goal_stats.get("failed") or 0)
        abn = int(goal_stats.get("abandoned") or 0)
        term = ach + fld + abn
        fail_rate = round((fld + abn) / term, 4) if term > 0 else 0.0
        reused = max(0, validated_count - zero_reuse_count)
        reuse_ratio = round(reused / validated_count, 4) if validated_count > 0 else 0.0
        total_modes = sum(int(v or 0) for v in execution_mode_counts.values())
        br_cnt = int(execution_mode_counts.get("brain_research") or 0)
        br_ratio = round(br_cnt / total_modes, 4) if total_modes > 0 else 0.0
        retry_cnt = int(llm_schema_retry_stats.get("total_24h") or 0)
        retry_rate = float(llm_schema_retry_stats.get("retry_rate") or 0.0)
        timeout_cnt = int((attempt_failure_clusters.get("by_bucket") or {}).get("timeout") or 0)
        return {
            "goal_failure_rate": fail_rate,
            "terminal_goals_count": term,
            "llm_schema_retry_count": retry_cnt,
            "llm_schema_retry_rate": retry_rate,
            "attempt_timeout_count": timeout_cnt,
            "skill_reuse_ratio": reuse_ratio,
            "brain_research_ratio": br_ratio,
            "recorded_at": now_dt.isoformat(),
        }

    async def evaluate_previous_reflection_impact(
        self,
        state: dict[str, Any],
        current_metrics: dict[str, Any],
        *,
        now: datetime | None = None,
        cycle_id: uuid.UUID | None = None,
    ) -> dict[str, Any] | None:
        """Compare post-reflection metrics against prior baseline; auto-revert degraded L1 playbook (V5.3 M3)."""
        if not self.attribution_enabled:
            return None
        prev_baseline = state.get("baseline_metrics")
        prev_at_raw = state.get("last_reflection_at")
        prev_at = _parse_dt(prev_at_raw)
        if not isinstance(prev_baseline, dict) or prev_at is None:
            return None
        if prev_at_raw and state.get("last_attribution_for") == prev_at_raw:
            cached = state.get("last_attribution")
            return dict(cached) if isinstance(cached, dict) else None

        cur_now = _ensure_utc(now or utcnow())
        elapsed_h = max(0.0, (cur_now - prev_at).total_seconds() / 3600.0)
        since_goals = await self.repo.list_recent_goals(since=prev_at, limit=120)
        term_since_prev = sum(
            1 for g in since_goals if str(g.get("status") or "").upper() in {"ACHIEVED", "FAILED", "ABANDONED"}
        )
        if elapsed_h < self.attribution_min_hours and term_since_prev < self.attribution_min_cycles:
            return {
                "verdict": "pending",
                "elapsed_hours": round(elapsed_h, 2),
                "terminal_goals_since_prev": term_since_prev,
            }

        prev_fail = float(prev_baseline.get("goal_failure_rate") or 0.0)
        cur_fail = float(current_metrics.get("goal_failure_rate") or 0.0)
        delta_fail = round(cur_fail - prev_fail, 4)

        prev_reuse = float(prev_baseline.get("skill_reuse_ratio") or 0.0)
        cur_reuse = float(current_metrics.get("skill_reuse_ratio") or 0.0)
        delta_reuse = round(cur_reuse - prev_reuse, 4)

        prev_retry_rate = float(prev_baseline.get("llm_schema_retry_rate") or 0.0)
        cur_retry_rate = float(current_metrics.get("llm_schema_retry_rate") or 0.0)
        prev_retry_cnt = int(prev_baseline.get("llm_schema_retry_count") or 0)
        cur_retry_cnt = int(current_metrics.get("llm_schema_retry_count") or 0)
        if prev_retry_rate > 0:
            retry_ratio = round(cur_retry_rate / prev_retry_rate, 3)
        elif prev_retry_cnt > 0:
            retry_ratio = round(cur_retry_cnt / prev_retry_cnt, 3)
        else:
            retry_ratio = 2.0 if cur_retry_cnt >= 6 else 1.0

        regressed = (
            delta_fail > self.regression_delta
            or (retry_ratio >= 2.0 and cur_retry_cnt >= 5)
        )
        improved = (not regressed) and (delta_fail <= -0.05 or (delta_reuse >= 0.05 and delta_fail <= 0.02))
        verdict = "regressed" if regressed else ("improved" if improved else "neutral")

        reverted_playbook: dict[str, Any] | None = None
        pb_dom = str(state.get("last_applied_playbook_domain") or "").strip()
        if verdict == "regressed" and pb_dom and self.playbook_manager is not None and hasattr(
            self.playbook_manager, "rollback_to_parent"
        ):
            try:
                rb = await self.playbook_manager.rollback_to_parent(
                    pb_dom,
                    reason=(
                        f"meta_reflection_regression (fail_rate {prev_fail:.2f}->{cur_fail:.2f}, "
                        f"retry_ratio={retry_ratio:.2f})"
                    ),
                )
                reverted_playbook = rb
                if rb.get("rolled_back"):
                    await self.repo.log_event(
                        "meta_reflection.playbook_auto_reverted",
                        level="warning",
                        message=(
                            f"Auto-reverted playbook '{pb_dom}' v{rb.get('from_version')}->"
                            f"v{rb.get('to_version')} due to meta-reflection regression"
                        ),
                        payload=rb,
                        cycle_id=cycle_id,
                    )
            except Exception as exc:
                logger.warning("meta_reflection.auto_revert_degraded", domain=pb_dom, error=str(exc))

        return {
            "verdict": verdict,
            "evaluated_at": cur_now.isoformat(),
            "prev_reflection_at": prev_at_raw,
            "elapsed_hours": round(elapsed_h, 2),
            "terminal_goals_since_prev": term_since_prev,
            "delta_goal_failure_rate": delta_fail,
            "delta_skill_reuse_ratio": delta_reuse,
            "retry_ratio": retry_ratio,
            "reverted_playbook": reverted_playbook,
        }

    async def build_diagnostic_bundle(
        self,
        *,
        now: datetime | None = None,
        trigger_reason: str = "interval_elapsed",
        previous_reflection_impact: dict[str, Any] | None = None,
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

        execution_mode_counts: dict[str, int] = {"codex": 0, "brain_research": 0, "skill_first": 0}
        recent_invocations_7d = 0

        llm_schema_retry_stats, attempt_failure_clusters, recent_error_events = (
            await self._collect_failure_cluster_telemetry(
                since=since,
                total_goals=max(len(recent_goals), 1),
            )
        )

        db = getattr(self.repo, "db", None)
        if db is not None:
            try:
                async with db.session() as s:
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

        allowlist_sample: list[str] = []
        if self.l2_bridge_enabled:
            try:
                from app.evolution.constitution_guard import EVOLVABLE_ALLOWLIST

                allowlist_sample = sorted(EVOLVABLE_ALLOWLIST)[:12]
            except Exception:
                allowlist_sample = []

        eff_impact = (
            previous_reflection_impact
            if previous_reflection_impact is not None
            else (state.get("last_attribution") if isinstance(state.get("last_attribution"), dict) else None)
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
            llm_schema_retry_stats=llm_schema_retry_stats,
            attempt_failure_clusters=attempt_failure_clusters,
            previous_reflection_impact=eff_impact,
            l2_evolution_bridge_enabled=self.l2_bridge_enabled,
            evolvable_allowlist_sample=allowlist_sample,
            self_edit_auto_approve_enabled=bool(self.self_edit_enabled and self.self_edit_auto_approve_enabled),
            self_edit_policy_mode=self.self_edit_policy_mode,
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
        current_metrics = self._build_metrics_snapshot(
            goal_stats=ctx.goal_stats,
            execution_mode_counts=ctx.execution_mode_counts,
            llm_schema_retry_stats=ctx.llm_schema_retry_stats,
            attempt_failure_clusters=ctx.attempt_failure_clusters,
            validated_count=int(ctx.goal_stats.get("validated_skill_count") or 0),
            zero_reuse_count=int(ctx.goal_stats.get("zero_reuse_skill_count") or 0),
            now_dt=cur_now,
        )
        attribution_impact = await self.evaluate_previous_reflection_impact(
            prev_state,
            current_metrics,
            now=cur_now,
            cycle_id=cycle_id,
        )
        if attribution_impact is not None:
            ctx = ctx.model_copy(update={"previous_reflection_impact": attribution_impact})

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

        # Outlet 3: L2 System Upgrade -> SelfEditManager (V5.4 autonomous) OR HumanBridge Card (strict <= 1 per 24h)
        l2_outcome: dict[str, Any] = {"sent": False, "skipped_reason": "none"}
        new_last_l2_at = prev_state.get("last_l2_proposal_at")
        if isinstance(output.l2_upgrade_proposal, dict):
            l2_title = str(output.l2_upgrade_proposal.get("title") or "").strip()
            l2_msg = str(output.l2_upgrade_proposal.get("message") or "").strip()
            l2_desired = str(output.l2_upgrade_proposal.get("desired_change") or l2_msg or "").strip()
            has_pc = isinstance(output.l2_upgrade_proposal.get("proposed_change"), dict)
            if l2_title and (l2_msg or l2_desired or has_pc):
                last_l2_dt = _parse_dt(new_last_l2_at)
                l2_allowed = (
                    last_l2_dt is None
                    or (cur_now - last_l2_dt).total_seconds() >= self.l2_cooldown_hours * 3600.0
                )
                if not l2_allowed:
                    l2_outcome = {"sent": False, "skipped_reason": "l2_24h_cooldown"}
                elif (
                    self.self_edit_manager is not None
                    and self.self_edit_enabled
                    and self.self_edit_auto_approve_enabled
                ):
                    rollback_plan = str(output.l2_upgrade_proposal.get("rollback_plan") or "").strip()
                    raw_pc = output.l2_upgrade_proposal.get("proposed_change")
                    raw_files = output.l2_upgrade_proposal.get("target_files")
                    se_res = await self.self_edit_manager.apply_direct_change_and_self_approve(
                        title=l2_title[:160],
                        reason=str(output.l2_upgrade_proposal.get("reason") or output.diagnosis_summary)[:400],
                        expected_benefit=str(output.l2_upgrade_proposal.get("expected_benefit") or "")[:300],
                        rollback_plan=rollback_plan[:300],
                        proposed_change=raw_pc if isinstance(raw_pc, dict) else None,
                        desired_change=(l2_desired or l2_title)[:1000],
                        target_files=list(raw_files) if isinstance(raw_files, list) else None,
                        test_plan=str(output.l2_upgrade_proposal.get("test_plan") or "")[:400],
                        cycle_id=cycle_id,
                    )
                    l2_outcome = {"sent": False, "self_edit": True, **dict(se_res or {})}
                    if l2_outcome.get("ok") or l2_outcome.get("enqueued"):
                        new_last_l2_at = cur_now.isoformat()
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
                    outreach_kwargs: dict[str, Any] = {
                        "kind": "ask_human",
                        "topic_key": topic_key,
                        "title": l2_title[:160],
                        "message": l2_msg[:1200],
                        "reason": str(output.l2_upgrade_proposal.get("reason") or output.diagnosis_summary)[:300],
                        "expected_benefit": str(output.l2_upgrade_proposal.get("expected_benefit") or "")[:300],
                        "options": options,
                        "cycle_id": cycle_id,
                    }
                    proposed_change = output.l2_upgrade_proposal.get("proposed_change")
                    if self.l2_bridge_enabled and isinstance(proposed_change, dict):
                        try:
                            sig = inspect.signature(self.human_bridge.maybe_send_outreach)
                            if "extra_metadata" in sig.parameters or any(
                                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                            ):
                                outreach_kwargs["extra_metadata"] = {
                                    "bridge_stage": "stage0_candidate",
                                    "proposed_change": proposed_change,
                                    "rollback_plan": rollback_plan[:300],
                                    "hypothesis": str(output.l2_upgrade_proposal.get("reason") or output.diagnosis_summary)[:300],
                                }
                        except Exception:
                            pass
                    outreach_res = await self.human_bridge.maybe_send_outreach(**outreach_kwargs)
                    l2_outcome = dict(outreach_res or {})
                    if outreach_res.get("sent"):
                        new_last_l2_at = cur_now.isoformat()

        reflection_count = int(prev_state.get("reflection_count") or 0) + 1
        last_pb_domain = prev_state.get("last_applied_playbook_domain")
        if playbook_outcome.get("saved") and playbook_outcome.get("domain"):
            last_pb_domain = str(playbook_outcome["domain"])
        elif isinstance(attribution_impact, dict) and (attribution_impact.get("reverted_playbook") or {}).get("rolled_back"):
            last_pb_domain = None

        last_attr_for = prev_state.get("last_attribution_for")
        if isinstance(attribution_impact, dict) and attribution_impact.get("verdict") in {"improved", "neutral", "regressed"}:
            last_attr_for = prev_state.get("last_reflection_at")

        fail_rate_cur = float(current_metrics.get("goal_failure_rate") or 0.0)
        retry_cnt_cur = int(current_metrics.get("llm_schema_retry_count") or 0)
        timeout_cnt_cur = int(current_metrics.get("attempt_timeout_count") or 0)
        regressed_pen = 0.20 if (isinstance(attribution_impact, dict) and attribution_impact.get("verdict") == "regressed") else 0.0
        pain_score = round(
            clamp01(
                0.45 * fail_rate_cur
                + 0.25 * min(1.0, retry_cnt_cur / 15.0)
                + 0.20 * min(1.0, timeout_cnt_cur / 5.0)
                + regressed_pen
            ),
            4,
        )

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
            "baseline_metrics": current_metrics,
            "last_attribution": attribution_impact if attribution_impact is not None else prev_state.get("last_attribution"),
            "last_attribution_for": last_attr_for,
            "last_applied_playbook_domain": last_pb_domain,
            "unresolved_pain_score": pain_score,
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
            "attribution_impact": attribution_impact,
            "unresolved_pain_score": pain_score,
            "reflection_count": reflection_count,
        }
