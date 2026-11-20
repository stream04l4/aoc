# AOC V1.0 — 100-Cycle Continuous Run & Acceptance Test Report

- **Timestamp (UTC)**: `2026-11-20T16:59:50.628527+00:00`
- **Organism**: `AOC-BENCH-ba9571`
- **Total Cycles Completed**: `100`
- **Wall Time**: `10.61s`
- **Fatal Errors**: `0`
- **Agent Calls**: primary=`23`, backup=`2`
- **Budget Enforcement**: `25` tasks used (limit window `25`)

## Cycle Outcomes

| Outcome | Count |
|---|---|
| `ACTION_FAILED` | 1 |
| `BUDGET_DEFERRED` | 29 |
| `IDLE` | 48 |
| `SUCCEEDED` | 22 |

## Selected Drives Distribution

| Selected Drive | Count |
|---|---|
| `curiosity` | 15 |
| `none` | 48 |
| `reproduction` | 1 |
| `survival` | 36 |

## Acceptance Scenario Verification (Scenarios A–F)

| Scenario | Passed |
|---|---|
| `A_survival_emergency` | PASS |
| `B_curiosity_explored` | PASS |
| `C_reproduction_virtual` | PASS |
| `D_idle_no_forced_task` | PASS |
| `E_restart_recovery` | PASS |
| `F_agent_fallback_and_bounded_fail` | PASS |
| `budget_respected` | PASS |

## Real End-to-End Closed-Loop Verification (PostgreSQL + Qwen LLM + Codex CLI + Letta)

- **Test**: `tests/acceptance/test_p0_8_e2e_real.py::test_e2e_real_curiosity_closed_loop_with_postgres_llm_codex_and_letta`
- **Result**: **PASSED** (`cycle_id = 439a85f4-93a2-407a-9cc4-df8873b7e02c`)
- **Selected Drive**: `curiosity`
- **External Agent Executed**: `codex` (`codex-cli 0.147.0`, real `codex exec` in isolated sandbox, completed in 23s, deterministic verification passed)
- **Expected vs Measured (Task Book 9.1)**:
  - Brain `expected_improvement`: `0.98` (advisory only, never applied directly)
  - Re-perceived `measured_satisfaction_change`: `{"survival": +0.0197, "curiosity": 0.0, "reproduction": -0.0001}`
- **Memory Fault-Tolerance**: Letta archival write timeout was caught cleanly without crashing the cycle; episodic & semantic memories persisted in PostgreSQL `memory_index` and recalled cleanly on `"MVCC"`.

## Full Automated Test Suite Summary

- Unit tests (`tests/unit/`): **76 passed** (P0-1 skeleton: 5, P0-2 drive engine: 43, P0-3 perception: 6, P0-4 cognition: 6, P0-5 governance & orchestration: 7, P0-6 memory: 3, P0-7 closed-loop & API: 6)
- Live integration tests (`tests/integration/`):
  - `test_p0_4_live_llm.py` (`Qwen/Qwen3.8-27B-FP8`): **PASSED**
  - `test_p0_5_real_agent.py` (real `codex exec` L1 workspace-write): **PASSED**
  - `test_p0_6_letta_live.py` (live `letta/letta:0.16.4` container): **PASSED**
- Real E2E acceptance test (`tests/acceptance/test_p0_8_e2e_real.py`): **PASSED**
- 100-cycle continuous PostgreSQL run (`scripts/run_100_cycles.py`): **PASSED** (100/100 cycles, 0 fatal errors, Scenarios A–F + Budget Cap all PASS)

