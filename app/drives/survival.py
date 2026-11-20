"""Survival Drive Evaluator (task book 4.1 + V2.0 P1-1 decoupling).

Evaluates genuine operational & physical survival threats using real collected metrics:
  - Resource Sufficiency: disk / memory / sustained load per core (never raw CPU %)
  - Service Availability: LLM health & latency, external agent availability
  - Operational Health: database/service availability & recent execution reliability

Economic Quota (`budget`) is recorded as an informational dimension for observability
and explicitly DECOUPLED from Survival CRITICAL so quota exhaustion enters THROTTLED
instead of causing a Survival CRITICAL <-> BUDGET_DEFERRED spin loop.
"""

from __future__ import annotations

from typing import Any

from app.core.enums import DriveType, ExecutionStatus
from app.drives.interfaces import DriveContext, DriveEvaluation, DriveEvaluator, clamp01
from app.drives.scoring import extract_number, linear_band, weighted_dimensions
from app.perception.interfaces import is_unavailable


class SurvivalDriveEvaluator(DriveEvaluator):
    drive_type = DriveType.SURVIVAL.value

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    def evaluate(self, ctx: DriveContext) -> DriveEvaluation:
        th = self.cfg.get("thresholds", {})
        obs = ctx.observation
        sys_state = obs.system_state or {}
        avail_res = obs.available_resources or {}
        notes: list[str] = []

        # 1. Resources (nonlinear: worst bottleneck pulls score down sharply)
        resources_score, res_notes = self._score_resources(sys_state, avail_res, th)
        notes.extend(res_notes)

        # 2. LLM service health
        llm_score, llm_notes = self._score_llm(sys_state, th)
        notes.extend(llm_notes)

        # 3. External agent availability
        agent_score, agent_notes = self._score_agents(avail_res)
        notes.extend(agent_notes)

        # 4. Budget & cost balance (Economic Quota — informational only, decoupled from Survival CRITICAL)
        budget_score, budget_notes = self._score_budget(ctx.budget, avail_res)
        notes.extend(budget_notes)

        # 5. Operational health & reliability (DB + monitored services + recent task failures)
        rel_score, rel_notes = self._score_reliability(ctx.recent_execution_statuses, sys_state, th)
        notes.extend(rel_notes)

        # Optional explicit database health probe
        db_health_score: float | None = None
        db_info = sys_state.get("database")
        if isinstance(db_info, dict) and "ok" in db_info:
            db_health_score = 1.0 if db_info.get("ok") else 0.0
            if db_health_score == 0.0:
                notes.append("core database unavailable")

        dimensions: dict[str, float | None] = {
            "resources": resources_score,
            "llm_health": llm_score,
            "agent_availability": agent_score,
            "budget": budget_score,
            "reliability": rel_score,
        }
        if db_health_score is not None:
            dimensions["database"] = db_health_score

        # Exclude `budget` from survival health weights (P1-1: Economic Quota != Survival Crisis)
        raw_weights = dict(
            self.cfg.get(
                "dimension_weights",
                {
                    "resources": 0.35,
                    "llm_health": 0.25,
                    "agent_availability": 0.15,
                    "reliability": 0.25,
                },
            )
        )
        raw_weights.pop("budget", None)
        if db_health_score is not None and "database" not in raw_weights:
            raw_weights["database"] = 0.25

        satisfaction, data_conf, w_notes = weighted_dimensions(
            dimensions, raw_weights, default_when_all_missing=0.35
        )
        notes.extend(w_notes)

        # Blend with observation confidence
        confidence = clamp01(data_conf * max(0.1, obs.confidence)) if data_conf > 0 else 0.0

        # Criticality check (P1-1): ONLY true physical/core-service survival threats trigger critical!
        # Budget exhaustion is NEVER in vital_dims.
        crit_dim_threshold = float(th.get("critical_dimension", 0.15))
        vital_dims = [
            d
            for k in ("resources", "llm_health", "database")
            if (d := dimensions.get(k)) is not None
        ]
        min_vital = min(vital_dims) if vital_dims else None
        critical = min_vital is not None and min_vital <= crit_dim_threshold

        # If a vital dimension is critically low, cap overall satisfaction so survival surfaces immediately
        if critical and min_vital is not None:
            satisfaction = min(satisfaction, clamp01(min_vital + 0.10))
            notes.append(f"critical survival dimension detected (min_vital={min_vital:.2f})")

        deficit = 1.0 - satisfaction
        base_u = float(self.cfg.get("base_urgency", 0.3))
        u_gain = float(self.cfg.get("urgency_deficit_gain", 0.7))
        urgency = 1.0 if critical else clamp01(base_u + u_gain * deficit)

        return DriveEvaluation(
            drive_type=self.drive_type,
            satisfaction=satisfaction,
            urgency=urgency,
            confidence=confidence,
            estimated_cost=clamp01(float(self.cfg.get("estimated_cost", 0.2))),
            estimated_risk=clamp01(float(self.cfg.get("estimated_risk", 0.35))),
            dimensions=dimensions,
            notes=notes,
            critical=critical,
        )

    def _score_resources(
        self, sys_state: dict[str, Any], avail_res: dict[str, Any], th: dict[str, Any]
    ) -> tuple[float | None, list[str]]:
        notes: list[str] = []
        sub_scores: list[float] = []

        # Disk free ratio (take worst mounted path)
        disk_info = sys_state.get("disk")
        if not is_unavailable(disk_info) and isinstance(disk_info, dict):
            ratios: list[float] = []
            if "free_ratio" in disk_info and not is_unavailable(disk_info["free_ratio"]):
                ratios.append(float(disk_info["free_ratio"]))
            elif "paths" in disk_info and isinstance(disk_info["paths"], dict):
                for p_data in disk_info["paths"].values():
                    r = extract_number(p_data, "free_ratio")
                    if r is not None:
                        ratios.append(r)
            if ratios:
                worst_disk = min(ratios)
                d_crit = float(th.get("disk_free_critical", 0.05))
                d_ok = float(th.get("disk_free_healthy", 0.20))
                # Nonlinear curve: squaring slightly below healthy penalizes critically low disk sharply
                lin = linear_band(worst_disk, d_crit, d_ok)
                disk_score = clamp01(lin**1.5)
                sub_scores.append(disk_score)
                if worst_disk < d_ok:
                    notes.append(f"disk free ratio low: {worst_disk:.1%}")

        # Memory available ratio
        mem_info = sys_state.get("memory")
        if not is_unavailable(mem_info) and isinstance(mem_info, dict):
            mem_avail = extract_number(mem_info, "available_ratio")
            if mem_avail is not None:
                m_crit = float(th.get("mem_avail_critical", 0.05))
                m_ok = float(th.get("mem_avail_healthy", 0.25))
                mem_score = clamp01(linear_band(mem_avail, m_crit, m_ok) ** 1.3)
                sub_scores.append(mem_score)
                if mem_avail < m_ok:
                    notes.append(f"memory available ratio low: {mem_avail:.1%}")

        # CPU sustained load per core (explicitly NOT instantaneous CPU utilization %)
        cpu_info = sys_state.get("cpu")
        if not is_unavailable(cpu_info) and isinstance(cpu_info, dict):
            load_per_core = extract_number(cpu_info, "load_per_core")
            if load_per_core is not None:
                l_ok = float(th.get("load_per_core_ok", 1.0))
                l_bad = float(th.get("load_per_core_bad", 3.0))
                load_score = linear_band(load_per_core, l_bad, l_ok)
                sub_scores.append(load_score)
                if load_per_core > l_ok:
                    notes.append(f"sustained load/core elevated: {load_per_core:.2f}")

        if not sub_scores:
            return None, ["system resource metrics unavailable"]

        # If any physical resource is in the critical zone, it cannot be masked by other healthy resources
        avg_score = sum(sub_scores) / len(sub_scores)
        min_score = min(sub_scores)
        crit_dim = float(th.get("critical_dimension", 0.15))
        if min_score <= crit_dim:
            return clamp01(min_score), notes
        return clamp01(0.4 * avg_score + 0.6 * min_score), notes

    def _score_llm(self, sys_state: dict[str, Any], th: dict[str, Any]) -> tuple[float | None, list[str]]:
        llm = sys_state.get("llm")
        if is_unavailable(llm) or not isinstance(llm, dict):
            return None, ["llm health metric unavailable"]
        avail = llm.get("available")
        if avail is None:
            return None, ["llm availability unknown"]
        if not avail:
            return 0.0, [f"llm unavailable: {llm.get('error', 'probe failed')}"]
        latency = extract_number(llm, "latency_ms")
        if latency is None:
            return 0.9, []
        ok_ms = float(th.get("llm_latency_ok_ms", 5000))
        bad_ms = float(th.get("llm_latency_bad_ms", 60000))
        score = 0.25 + 0.75 * linear_band(latency, bad_ms, ok_ms)
        notes = [f"llm latency high: {latency:.0f}ms"] if latency > ok_ms else []
        return clamp01(score), notes

    def _score_agents(self, avail_res: dict[str, Any]) -> tuple[float | None, list[str]]:
        agents = avail_res.get("agents")
        if is_unavailable(agents) or not isinstance(agents, dict):
            return None, ["agent availability unavailable"]
        total = extract_number(agents, "enabled_count")
        healthy = extract_number(agents, "healthy_count")
        if total is None or healthy is None:
            return None, ["agent counts incomplete"]
        if total <= 0:
            return 0.0, ["no enabled external agents available"]
        ratio = clamp01(healthy / total)
        notes = [f"only {int(healthy)}/{int(total)} agents healthy"] if ratio < 1.0 else []
        return ratio, notes

    def _score_budget(
        self, budget: dict[str, Any], avail_res: dict[str, Any]
    ) -> tuple[float | None, list[str]]:
        src = budget or avail_res.get("budget")
        if is_unavailable(src) or not isinstance(src, dict) or not src:
            return None, ["budget metrics unavailable"]

        ratios: list[float] = []
        notes: list[str] = []

        t_used = extract_number(src, "tasks_used")
        t_limit = extract_number(src, "tasks_limit")
        if t_used is not None and t_limit is not None and t_limit > 0:
            rem = clamp01(1.0 - t_used / t_limit)
            ratios.append(rem)
            if rem < 0.2:
                notes.append(f"daily task budget low ({int(t_used)}/{int(t_limit)})")

        tok_used = extract_number(src, "tokens_used")
        tok_limit = extract_number(src, "tokens_limit")
        if tok_used is not None and tok_limit is not None and tok_limit > 0:
            rem = clamp01(1.0 - tok_used / tok_limit)
            ratios.append(rem)
            if rem < 0.2:
                notes.append(f"daily token budget low ({int(tok_used)}/{int(tok_limit)})")

        if not ratios:
            return None, ["budget limits not specified"]
        return min(ratios), notes

    def _score_reliability(
        self, recent_statuses: list[str], sys_state: dict[str, Any], th: dict[str, Any]
    ) -> tuple[float | None, list[str]]:
        notes: list[str] = []
        parts: list[float] = []

        window = int(th.get("failure_window", 10))
        if recent_statuses:
            slice_st = recent_statuses[:window]
            failures = sum(
                1
                for s in slice_st
                if s in {ExecutionStatus.FAILED.value, ExecutionStatus.TIMEOUT.value}
            )
            task_rel = clamp01(1.0 - failures / len(slice_st))
            parts.append(task_rel)
            if failures:
                notes.append(f"{failures}/{len(slice_st)} recent executions failed/timed out")

        services = sys_state.get("services")
        if not is_unavailable(services) and isinstance(services, dict) and services:
            valid = [v for v in services.values() if isinstance(v, dict) and "ok" in v]
            if valid:
                ok_count = sum(1 for v in valid if v.get("ok"))
                svc_rel = clamp01(ok_count / len(valid))
                parts.append(svc_rel)
                if ok_count < len(valid):
                    notes.append(f"{len(valid) - ok_count}/{len(valid)} monitored services unhealthy")

        if not parts:
            # No executions yet and no monitored services: cannot claim 100% tested, treat as healthy-neutral 0.85
            # only if system_state itself is present; otherwise unavailable.
            if sys_state:
                return 0.85, []
            return None, ["reliability metrics unavailable"]
        return sum(parts) / len(parts), notes
