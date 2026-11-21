# Artificial Organism Core (AOC) V2.0

Artificial Organism Core（AOC）是一个基于 Python 3.12+ 模块化单体架构（Modular Monolith）构建的自主数字生命体核心系统。系统由生存（Survival）、繁衍（Reproduction，基于实证证据的能力与可验证技能积累）和好奇心（Curiosity，基于四因子信息增益的未知探索）三大独立驱动器支配，在严格的 L0–L3 安全治理与每日资源预算边界内持续运行 16 阶段自主认知循环。

---

## V2.0 核心架构演进（Phase 1 / P1-1a 至 P1-10）

- **P1-1a 基础安全门禁前置** (`app/api/auth.py`, `app/orchestration/orchestrator.py`, `app/core/loop.py`)：
  - 默认绑定 `127.0.0.1`；全非公开 API 强制 Bearer Token 鉴权与四级 RBAC（`read`/`approve`/`control`/`admin`）；
  - 公共 `approve_and_dispatch()` 审批后四重重检（计划哈希、L3 规则、预算、Agent 健康度）；`run_cycle()` 防重入锁。
- **P1-1 预算与生存解耦及统一 `LLMGateway`** (`app/drives/survival.py`, `app/cognition/gateway.py`)：
  - 将 Survival 拆分为运行健康度、物理资源、外部依赖可用性与经济配额（`QUOTA_OK` / `QUOTA_EMERGENCY_ONLY` / `QUOTA_EXHAUSTED`）四个正交维度；配额耗尽进入 `THROTTLED` 休眠至 `quota_reset_at`，彻底消除空转风暴；
  - 所有 LLM 调用、健康探测与外部 Agent 同端点 Token 消耗统一经 `LLMGateway` 记账与熔断保护。
- **P1-2 16 态持久化任务状态机、幂等 `TaskFinalizer` 与事务 Outbox/Inbox** (`app/orchestration/state_machine.py`, `app/orchestration/finalizer.py`, `app/storage/outbox.py`)：
  - 乐观锁状态机（`state_version` + `task_lifecycle_events`）；
  - 纯数据库上下文重建的 `TaskFinalizer.finalize_execution()`，统一收敛同周期、跨周期 `IN_FLIGHT`、审批后与崩溃恢复任务；
  - 基于 PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` 的事务 Outbox/Inbox，无需引入 Redis/Kafka。
- **P1-3 宿主机 `systemd` Agent Runner 与 `kill -9` 崩溃恢复** (`app/runner/`, `deploy/systemd/aoc-agent-runner.service`)：
  - 非特权 `User=ubuntu`、`Delegate=yes`、cgroup v2 子树隔离（`MemoryMax=2G`, `TasksMax=128`）、Unix Domain Socket (`/run/aoc-runner/runner.sock`)；
  - 完整 `ProcessIdentity` (`boot_id + /proc/<pid>/stat starttime + cgroup.procs`) 防 PID 复用，通过 6×3=18 次真实 `kill -9` 验证。
- **P1-4 语义记忆、可切换 ONNX Embedding 与混合检索** (`app/memory/embedding.py`, `app/memory/retrieval.py`, `app/memory/info_gain.py`)：
  - 默认采用本机量化 `BAAI/bge-small-zh-v1.5:onnx-qint8`（512 维，独立 RSS 153.8MB < 600MB，中英混合 Recall@5 = 98.0%），支持热切换；
  - `pgvector` + `pg_trgm` + 时间衰减 + 来源可靠度 + 重要度五维混合检索；四因子确定性信息增益评估；Outbox 异步同步 Letta。
- **P1-5 真实数字环境感知与 SSRF/Prompt Injection 防护** (`app/perception/world/`, `config/sensors.yaml`)：
  - 接入真实 RSS、Git、Web 变更与增强 `SystemSensor`；全链路域名白名单、DNS 解析后私网/元数据/IPv6 阻断、中英双语 Prompt Injection 检疫隔离与 `<untrusted_world_event>` XML 围栏。
- **P1-8 强绑定 L2 审批、Telegram 交互通知与中英双语语义风控** (`app/governance/approval.py`, `app/governance/semantic_policy.py`, `app/notifications/telegram.py`)：
  - 确定性双语规则 + `SemanticRiskClassifier`（超时 Fail-Closed 至 L2，绝不降级确定性 L2/L3）；
  - 审批单强绑定 `(task_id, plan_hash, params_digest, nonce, expires_at)`；Telegram Outbox 脱敏推送与 Webhook 签名/白名单校验。
- **P1-6 受控 MCP `ToolProvider`（3 个真实工具）、`codex-cli 0.171.0` 升级与可解释路由** (`app/tools/`, `app/agents/codex.py`, `app/agents/router.py`)：
  - **架构边界明确声明**：严格区分 `AgentProvider`（通用自主 Agent：当前启用 `codex`，`hermes` 保持 `DISABLED`）与 `ToolProvider`（通过 MCP stdio JSON-RPC 2.0 提供的 `web_fetch_readonly`、`workspace_filesystem`、`git_docs_search` 3 个受控原子工具，构成第二条受控工具执行路径，**不等于**第二个通用自主 Agent，也**不等于**多 Agent 协作）；
  - 解析 `codex-cli 0.171.0` JSONL `thread_id` 并落库；`AgentRouter` 5 因子乘性可解释路由记录 `decision_logs`。
- **P1-7 动态 Self Model、可验证 `SkillRegistry` 与 Reproduction 重构** (`app/self_model/`, `app/drives/reproduction.py`)：
  - 五态能力生命周期（`DECLARED -> AVAILABLE -> ACQUIRED -> VALIDATED -> DEPRECATED`）与精确 Beta-Binomial 后验；外部 Agent 静态能力对 Reproduction 覆盖率贡献严格为 `0`；
  - 技能隔离断言测试与 SHA-256 `artifact_checksum` 防篡改校验；动态投影至 Letta `self_model` Core Block。
- **P1-9 可选有界 Affect 调制层（默认关闭）** (`app/affect/`, `config/affect.yaml`)：
  - 默认 `enabled: false`（零影响）；开启时严格限制在 `±maximum_influence ≤ 0.15` 内，严禁触碰安全治理、预算、审批与 Survival CRITICAL，异常自动静默降级为 V1.0 策略。

---

## 快速启动与验证

```bash
# 1. 运行全量单元测试（140 项，SQLite 离线快速执行）
.venv/bin/pytest tests/unit/ -v

# 2. 运行 10 类真实故障注入测试套件
.venv/bin/python scripts/run_v2_fault_injection.py

# 3. 运行 P1-3 真实 OS 级 kill -9 矩阵（6 场景 x 3 轮 = 18 次）
.venv/bin/pytest tests/integration/test_p1_3_kill9_recovery.py -v

# 4. 运行 P1-10 真实全栈端到端验收测试（需宿主机 Runner、Letta 0.16.4 与远端 vLLM 在线）
.venv/bin/pytest tests/acceptance/test_p1_10_e2e_full.py -v
```

---

## 文档与 V2.0 验收报告导航

- [系统架构设计 (`docs/architecture.md`)](docs/architecture.md)
- [驱动器、混合检索、信息增益与自模型算法 (`docs/algorithms.md`)](docs/algorithms.md)
- [外部 Agent 适配器与 MCP ToolProvider 指南 (`docs/agent_adapters.md`)](docs/agent_adapters.md)
- [HTTP API 完整参考 (`docs/api.md`, V1 + `/v2/*`)](docs/api.md)
- [V2.0 生产快照迁移与历史测试数据归档报告 (`docs/v2_migration_report.md`)](docs/v2_migration_report.md)
- [V2.0 真实 `kill -9` 与 10 类故障注入报告 (`docs/v2_kill9_report.md`)](docs/v2_kill9_report.md)
- [V2.0 资源消耗、Embedding 选型与 Prometheus 监控报告 (`docs/v2_resource_report.md`)](docs/v2_resource_report.md)
- [V2.0 安全评估与对抗测试报告 (`docs/v2_security_report.md`)](docs/v2_security_report.md)
- [V2.0 真实全栈集成验收与 72 小时长稳运行报告 (`docs/v2_soak_report_72h.md`)](docs/v2_soak_report_72h.md)
- [已知问题与 V3.0 演进路线图 (`docs/known_issues_and_roadmap.md`)](docs/known_issues_and_roadmap.md)
