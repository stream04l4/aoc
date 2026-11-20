# AOC V1.0 — Core Drive & Arbitration Algorithms

All weights, thresholds, and decay constants live in `config/drives.yaml` and are never hard-coded.

## 1. Unified DriveState Model

Each drive produces a `DriveState` with normalized fields in `[0, 1]` (except `trend ∈ [-1, 1]`):

- `satisfaction`: weighted average of available normalized sub-dimensions.
- `deficit = 1 - satisfaction`: strictly derived invariant.
- `urgency`: `clamp(base_urgency + urgency_deficit_gain * deficit + urgency_trend_gain * max(0, -trend))`; forced to `1.0` on critical survival triggers.
- `confidence`: fraction of configured dimension weights actually available × observation confidence. Missing/unavailable metrics are excluded from the denominator and lower `confidence` (never treated as "normal").
- `persistence`: accumulates by `persistence_growth * deficit` each cycle while `deficit > unmet_threshold`, decays multiplicatively by `persistence_decay` once satisfied.
- `trend`: exponential moving average of `satisfaction(t) - satisfaction(t-1)`.

## 2. Independent Evaluators

1. **Survival (`SurvivalDriveEvaluator`)**:
   - `resources`: bottleneck-weighted blend (`0.4 * mean + 0.6 * min`) of nonlinear disk free ratio, memory available ratio, and sustained load per core (raw CPU % is explicitly not used).
   - `llm_health`: probe reachability and latency band (via `LLMGateway.health_check`).
   - `agent_availability`: `healthy_count / enabled_count`.
   - `reliability`: success ratio over recent execution window + monitored service health.
   - `budget`: recorded as an informational Economic Quota dimension only — explicitly **decoupled from Survival health weights and `critical` triggers (P1-1)** so daily quota exhaustion transitions cleanly into `THROTTLED` until `quota_reset_at` instead of causing a `CRITICAL <-> BUDGET_DEFERRED` spin loop.
   - Only genuine vital dimensions (`resources`, `llm_health`, `database`) ≤ `critical_dimension` trigger `critical=True`, cap satisfaction, and set `urgency=1.0`.
2. **Reproduction (`ReproductionDriveEvaluator`)** *(virtual only in V1.0)*:
   - `capability_coverage`, `reusable_skills`, `capability_goals`, `resource_reserve`, `expansion_readiness`.
3. **Curiosity (`CuriosityDriveEvaluator`)**:
   - `knowledge_gaps`, `unknown_information`, `exploration_completion`, `new_knowledge`, time-decayed `information_gain`, and time-decayed `learning_progress`.

## 3. Hybrid Arbitration

For each drive $d$:

$$\text{base}_d = \text{deficit}_d \cdot \text{urgency}_d \cdot \text{confidence}_d \cdot w_d$$

$$\text{inhibition}_d(t) = 1 - s_{\text{inhib}} \cdot \exp\left(-\frac{t - t_{\text{sat},d}}{\tau_{\text{inhib}}}\right)$$

$$\text{priority}_d = \max\Big(0,\; \text{base}_d \cdot (1 + \alpha \cdot \text{persistence}_d) \cdot \text{inhibition}_d(t) - \beta \cdot \text{cost}_d - \gamma \cdot \text{risk}_d\Big)$$

Decision rules:
1. **Survival emergency protection**: if `survival.satisfaction < survival_critical_threshold` and `survival.confidence >= min_confidence`, immediately select `survival` (bypassing inhibition) and attach `emergency_mode=True`.
2. **Confidence gate**: drives with `confidence < min_confidence` are ineligible.
3. **IDLE**: if no eligible drive reaches `idle_threshold`, select `none` (`IDLE`) — never force a task.
4. **Auditability**: `DriveDecision.reason` logs why the winning drive was chosen and why each alternative was rejected, alongside the full `scores` breakdown.
