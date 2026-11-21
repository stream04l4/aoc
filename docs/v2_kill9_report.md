# AOC V2.0 真实 `kill -9` 崩溃恢复与 10 类故障注入测试报告

## 1. 测试概述

为验证 AOC V2.0 在进程强杀（`SIGKILL / kill -9`）、外部基础设施断连、模型超时与消息重复投递下的工程韧性，本项目完成了两部分实测：

1. **P1-3 真实操作系统级 `kill -9` 矩阵测试**（`tests/integration/test_p1_3_kill9_recovery.py`）：**6 大场景 × 3 次重复 = 18 次真实 `kill -9`**。
2. **P1-10 全栈 10 类真实故障注入套件**（`scripts/run_v2_fault_injection.py`）：覆盖 Core、Runner、Worker、PostgreSQL、Letta、Embedding、LLMGateway、执行超时、Outbox 10 倍重放与每日配额耗尽恢复。

---

## 2. P1-3 真实 `kill -9` 矩阵实测结果（6 场景 × 3 轮 = 18/18 PASSED）

| 场景编号 | 故障注入时机与目标进程 | 重复次数 | 期望恢复行为 | 实测结果 | 残留孤儿进程 | 误判 SUCCEEDED |
|---|---|---:|---|---|---:|---:|
| **Scenario 1** | 任务 `RUNNING` 期间对 **AOC Core** 发送 `kill -9`（Runner 与子进程继续运行完） | 3 | 新 Core 启动后通过 Runner `/v1/executions/{id}` 回收真实退出码 `0` 与输出，由 `TaskFinalizer` 幂等结项为 `SUCCEEDED` | **3 / 3 PASSED** | 0 | 0 |
| **Scenario 2** | 任务 `RUNNING` 期间对 **Host Agent Runner** 发送 `kill -9`（shim 与子进程独立会话存活） | 3 | Runner 重启后通过 `recover_from_disk()` 校验 `ProcessIdentity` (`boot_id + starttime`) 重新纳管或读取 `.exitcode` 完成对账 | **3 / 3 PASSED** | 0 | 0 |
| **Scenario 3** | 任务 `RUNNING` 期间对 **Worker/Codex 子进程** 发送 `kill -9` | 3 | Shim 捕获 `137`/`-9` 退出码，Runner 与 Orchestrator 立即识别为 `FAILED`，写入 `execution_recovery_records` | **3 / 3 PASSED** | 0 | 0 |
| **Scenario 4** | 任务 `RUNNING` 期间同时对 **AOC Core + Runner** 发送 `kill -9` | 3 | 双方重启后通过持久化 `.json`/`.child.pid`/`.exitcode` 与 `ProcessIdentity` 完成双向对账，状态一致率 100% | **3 / 3 PASSED** | 0 | 0 |
| **Scenario 5** | 子进程超时未退出 + 强制杀死清理 | 3 | Runner 对整个进程组/cgroup 发送 `SIGTERM -> SIGKILL`，状态落入 `TIMEOUT`，子孙进程 100% 清理 | **3 / 3 PASSED** | 0 | 0 |
| **Scenario 6** | 进程崩溃且无 `.exitcode`（模拟断电或 PID 被其他进程复用） | 3 | `verify_process_identity()` 通过 `/proc/<pid>/stat` 第 22 字段 `starttime` 识破 PID 复用，拒绝误判成功，转入 `RECOVERY_REQUIRED / FAILED` | **3 / 3 PASSED** | 0 | 0 |

- **汇总指标**：**18 / 18 (100%) 通过**；可确认孤儿进程 = **0**；未知状态误判成功 = **0**；重复结项 = **0**。

---

## 3. P1-10 十类真实故障注入套件实测结果（10 / 10 PASSED）

复现命令：
```bash
.venv/bin/python scripts/run_v2_fault_injection.py
```

| 序号 | 故障标识 | 故障场景描述 | 关键断言与实测表现 | 状态 |
|---:|---|---|---|---|
| 1 | `1_core_kill9` | AOC Core 进程被 `SIGKILL` 强杀，子进程存活 | 新实例启动后通过 `verify_process_identity()` 定位存活子进程，按策略清理并将任务经 `RECOVERY_REQUIRED` 安全收敛至 `FAILED` | **PASSED** |
| 2 | `2_runner_kill9` | 独立 Agent Runner 守护进程在子进程执行中被 `SIGKILL` | 子进程在独立会话中继续输出 `SURVIVED_RUNNER_KILL9` 并退出；Runner 重启后从磁盘恢复执行记录并返回 `SUCCEEDED` | **PASSED** |
| 3 | `3_child_kill9` | Worker 子进程在运行中被外部 `SIGKILL` | Runner 捕获退出码 `137` (`-9`)，准确标记为 `FAILED`，绝不误判为成功 | **PASSED** |
| 4 | `4_postgres_outage` | PostgreSQL 短暂不可达 (`127.0.0.1:19999`) | `db.ping()` 捕获连接异常并返回 `False`，不抛出未捕获致命错误；数据库恢复后立即重连成功 | **PASSED** |
| 5 | `5_letta_outage` | Letta 0.16.4 服务不可达，随后恢复 | 记忆优先落库 `memory_index(sync_status='failed')` 并保留在 `outbox_events`；Letta 恢复后经 Outbox 重放 100% 补同步为 `synced` | **PASSED** |
| 6 | `6_embedding_outage` | Embedding 提供商抛出 `EmbeddingError` | 写入不丢数据（`embedding_status='pending'` 留待 Outbox 补嵌），检索自动降级为词法/Trigram 匹配并正常召回 | **PASSED** |
| 7 | `7_llm_timeout` | 远端 LLM 接口建立 TCP 连接后挂起超时 | `LLMGateway` 在超时阈值（0.4s）准确中断请求，连续失败达阈值（2 次）后自动打开熔断器（`circuit_breaker_opened=True`）快速失败 | **PASSED** |
| 8 | `8_agent_timeout` | 外部 Agent 子进程死循环超过 `timeout_seconds` | Runner 超时监控器自动终止进程组，状态设为 `TIMEOUT`，`verify_process_identity` 确认 `orphan_alive=False` | **PASSED** |
| 9 | `9_outbox_duplicate` | Outbox / `TaskFinalizer` 对同一 `execution_id` 连续重放 10 次 | 首次结项写入 1 条记忆与 1 条能力证据；后续 10 次重放全部被 `inbox_events` 与 `finalized_at` 幂等拦截（`replays_skipped=10`），零重复写入 | **PASSED** |
| 10 | `10_budget_exhaust_recover` | 每日 Token 配额耗尽（200/100），随后配额刷新 | 耗尽周期零 LLM 调用直接转入 `THROTTLED` 并记录精确 `quota_reset_at`；配额恢复后自动回到 `ACTIVE/IDLE` 并入队 `quota_recovered` 通知 | **PASSED** |
