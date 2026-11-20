"""Prometheus metrics."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

CYCLES_TOTAL = Counter("aoc_cycles_total", "Autonomous cycles completed", ["outcome"], registry=REGISTRY)
CYCLE_DURATION = Histogram(
    "aoc_cycle_duration_seconds",
    "Cycle wall time",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
DRIVE_SATISFACTION = Gauge("aoc_drive_satisfaction", "Drive satisfaction", ["drive"], registry=REGISTRY)
DRIVE_PRIORITY = Gauge("aoc_drive_priority", "Last arbitration priority", ["drive"], registry=REGISTRY)
LIFECYCLE_STATE = Gauge("aoc_lifecycle_state", "1 for the current lifecycle state", ["state"], registry=REGISTRY)
AGENT_EXECUTIONS = Counter(
    "aoc_agent_executions_total", "Agent executions by final status", ["agent", "status"], registry=REGISTRY
)
LLM_CALLS = Counter("aoc_llm_calls_total", "LLM calls", ["operation", "result"], registry=REGISTRY)
LLM_TOKENS = Counter("aoc_llm_tokens_total", "LLM tokens used", registry=REGISTRY)
POLICY_VERDICTS = Counter("aoc_policy_verdicts_total", "Governance verdicts", ["verdict"], registry=REGISTRY)
SENSOR_FAILURES = Counter("aoc_sensor_failures_total", "Sensor failures", ["sensor"], registry=REGISTRY)
MEMORY_WRITES = Counter("aoc_memory_writes_total", "Long-term memory writes", ["type", "result"], registry=REGISTRY)


def set_lifecycle(state: str, all_states: list[str]) -> None:
    for s in all_states:
        LIFECYCLE_STATE.labels(state=s).set(1 if s == state else 0)
