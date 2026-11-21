# AOC V2.0 生产快照增量迁移与历史测试数据归档报告

## 1. 概述与数据保护原则

本报告记录 Artificial Organism Core（AOC）从 V1.0 初始生产快照（`backups/aoc_pg_dump_20261120.sql`，PostgreSQL 16.16，Alembic Revision `0001_initial_schema`）连续升级至 V2.0 完整 Schema（Revision `0008_p1_9_affect (head)`）的实测迁移回归结果，以及历史测试残留数据（`AOC-BENCH-*`、`AOC-E2E-*`）的分类与隔离归档演练结果。

严格遵循《开发任务书 v2.1》决策 2 与决策 5：
1. **原始快照只读保留**：`backups/aoc_pg_dump_20261120.sql`（7,715,667 字节）全程只读，未作任何修改或覆盖。
2. **唯一事实源复用**：复用 V1.0 原有 18 张表中的 `agent_executions` 与 `memory_index` 作为唯一事实主表，不重复创建职责重叠的冗余表。
3. **非破坏性演练**：所有迁移回归与归档清理演练均在从原始快照恢复的独立临时 PostgreSQL 16 数据库（`aoc_p1_10_rehearsal`）上执行并验证，验证完成后立即销毁临时库；**正式生产数据库 `aoc` 未经 Joel 单独批准前保持原状**。

---

## 2. 数据库 Schema 演进总览（V1.0 18 张 → V2.0 36+1 张）

### 2.1 V1.0 原有表扩展（4 张表增量扩展，14 张表原样保留）

| 原有表名 | 增量 Migration | 新增列 / 约束 | 用途说明 |
|---|---|---|---|
| `organism_state` | `0002_p1_1a_security_and_budget` | `quota_reset_at TIMESTAMPTZ`, `throttled_reason TEXT` | 经济配额耗尽（`THROTTLED`）精确唤醒时间戳与原因；同时在 P1-9 正式启用预留列 `emotion_reserved JSONB` |
| `action_plans` | `0002_p1_1a_security_and_budget` | `plan_hash VARCHAR(64)` | 审批绑定与防篡改 SHA-256 计划摘要 |
| `agent_tasks` | `0003_p1_2_durable_tasks` | `state_version INT DEFAULT 1`, `finalized_at TIMESTAMPTZ`, `idempotency_key VARCHAR(128)` | 16 态持久化任务状态机乐观锁与幂等结项时间戳 |
| `agent_executions` | `0003` + `0004_p1_3_runner_recovery` | `attempt_number INT`, `idempotency_key VARCHAR(128)`, `runner_id VARCHAR(64)`, `process_identity JSONB`, `thread_id VARCHAR(128)`, `finalized_at TIMESTAMPTZ` | 复用为唯一执行事实表，承载宿主机 Runner 进程身份（`boot_id + starttime + cgroup`）与 `codex-cli 0.171.0` `thread_id` |
| `memory_index` | `0005_p1_4_semantic_memory` | `embedding_status VARCHAR(24)`, `reliability FLOAT`, `information_gain FLOAT`, `ix_memory_index_trgm` | 复用为唯一记忆主表，承载四因子信息增益与 `pg_trgm` 双语检索索引 |
| `agent_registry` | `0007_p1_7_self_model` | `stats JSONB DEFAULT '{}'` | 持久化各外部 Agent 实测成功率与耗时统计，供 `AgentRouter` 可解释路由使用 |

### 2.2 V2.0 新增表清单（核心必选 18 张 + 可选 Affect 1 张）

| Migration 版本 | 阶段 | 新增表名 | 数量 | 核心职责与唯一约束 |
|---|---|---|---|---|
| `0002_p1_1a_security_and_budget` | P1-1a / P1-8 | `api_credentials`, `approval_records`, `policy_evaluations` | 3 | API Bearer Token 哈希凭证、强绑定 L2 审批单（`nonce`+`plan_hash`+`expires_at`）、三层安全策略审计 |
| `0003_p1_2_durable_tasks` | P1-2 | `task_lifecycle_events`, `outbox_events`, `inbox_events` | 3 | 状态机转换审计（`uq_task_lifecycle_ver`）、PostgreSQL `SKIP LOCKED` 事务发件箱与幂等收件箱（`uq_inbox_consumer_key`） |
| `0004_p1_3_runner_recovery` | P1-3 | `runner_registry`, `execution_recovery_records` | 2 | 宿主机 `systemd` Runner 心跳注册表与 `kill -9` 崩溃重启对账记录 |
| `0005_p1_4_semantic_memory` | P1-4 | `embedding_model_registry`, `memory_embeddings` | 2 | 可切换 Embedding 模型版本/维度注册表与按模型隔离的向量存储（`uq_mem_emb_mem_model`） |
| `0006_p1_5_world_sensors` | P1-5 | `world_event_sources`, `sensor_checkpoints`, `world_events` | 3 | 外部 RSS/Git/Web 感知源配置、增量游标位点与内容哈希去重事件表（`uq_world_events_dedup`） |
| `0007_p1_7_self_model` | P1-7 | `self_model_versions`, `capability_profiles`, `capability_evidence`, `skill_registry`, `skill_validation_results` | 5 | 动态自我模型版本快照、Beta-Binomial 能力后验画像、不可变执行证据账本（`uq_cap_evidence_prof_exec`）、可验证技能注册表与隔离测试结果 |
| `0008_p1_9_affect` | P1-9（可选） | `affect_history` | 1 | 默认关闭的有界情绪调制历史轨迹表 |

---

## 3. 原始生产快照连续迁移与归档演练实测结果

在临时 PostgreSQL 16 数据库 `aoc_p1_10_rehearsal` 中完整导入 `backups/aoc_pg_dump_20261120.sql` 后，连续执行两次 `alembic upgrade head` 并运行 `scripts/archive_v1_test_data.py --execute`：

- **Alembic 最终版本**：`0008_p1_9_affect (head)`（重复执行 `alembic upgrade head` 100% 幂等，零报错）。
- **历史 `pending` 记忆零误发**：迁移后 `outbox_events` 初始行数为 **`0`**，52 条历史 `AOC-BENCH-*` 的 `pending` 记忆未被盲目推入生产 Letta。
- **归档导出文件**：生成独立 JSON 归档文件（21,468 字节），完整保存全部被隔离的 `AOC-BENCH-*` / `AOC-E2E-*` 实例、54 条未同步/失败测试记忆、20 条 `bench-topic-*` 及 2 条 `bench-*` 测试 Agent。

### 3.1 归档前后精确行数核对表（实测）

| 表名 | 归档前总行数 | 归档后保留行数 | 隔离/归档行数 | 主实例 `AOC-001` 完整性核验 |
|---|---:|---:|---:|---|
| `organism_identity` | 8 | **1** | 7 (`4 BENCH + 3 E2E`) | `AOC-001` (`63346945-3899-4380-a009-0f7918175ce0`) 100% 完好 |
| `organism_state` | 8 | **1** | 7 | `AOC-001` (`PAUSED`, `cycle_count=29`) 100% 完好 |
| `cycle_records` | 432 | **29** | 403 (`400 BENCH + 3 E2E`) | `AOC-001` 全部 29 个历史周期零丢失 |
| `active_goals` | 80 | **1** | 79 | `AOC-001` 唯一 `ACTIVE` 目标保留 |
| `agent_tasks` | 82 | **1** | 81 (`76 BENCH + 1 E2E + 4 孤儿`) | `AOC-001` 待审批任务 `c4c48d42-...` 完整保留 |
| `agent_executions` | 81 | **0** | 81 (`80 bench-* + 1 E2E`) | `AOC-001` 原有 0 条执行，无误删 |
| `memory_index` | 58 | **4** (`0 pending, 0 failed`) | 54 (`52 pending + 2 failed`) | `AOC-001` 唯一 `synced` 身份记忆完好保留 |
| `world_states` | 25 | **5** | 20 (`bench-topic-*`) | 20 条基准测试话题全部清理 |
| `agent_registry` | 5 | **3** | 2 (`bench-primary/backup`) | 保留 `codex`, `hermes`, `generic-dsh` |
| `drive_history` | 2,586 | **168** | 2,418 | `AOC-001` 全部 168 条驱动历史 100% 完好 |
| `resource_usage` | 121 | **30** | 91 | `AOC-001` 全部 30 条资源记录 100% 完好 |

---

## 4. 正式生产库切换与遗留 L2 任务处置规程（待 Joel 审批后执行）

1. **生产 PostgreSQL 镜像升级（`pgvector`）**：
   - 现有 `aoc-postgres` 容器运行 `postgres:16-alpine`（不含 `pgvector` C 扩展），而 `docker-compose.yml` 已升级为官方 `pgvector/pgvector:0.8.7-pg16-bookworm`。
   - **重要红线**：严禁将 Alpine 数据卷直接挂载给 Debian/Bookworm 镜像（glibc 与 musl 排序规则不兼容）。正式切换时必须先从旧容器 `pg_dump` 导出最新快照，重建全新的 `pgvector:0.8.7-pg16-bookworm` 数据卷并导入，随后执行 `alembic upgrade head`。
2. **执行归档脚本**：
   ```bash
   .venv/bin/python scripts/archive_v1_test_data.py \
     --execute \
     --archive-out backups/aoc_v1_test_artifacts_archive_$(date +%Y%m%d).json
   ```
3. **裁决遗留 L2 审批任务 `c4c48d42-cdde-4fcf-becd-1210d8c074ab`**：
   - 该任务创建于 `2026-11-20 16:56:28Z`，目标执行器为 `hermes`（当前宿主机未安装且已禁用）。建议在启动 V2.0 72h 长稳前通过 `POST /approvals/c4c48d42-cdde-4fcf-becd-1210d8c074ab/reject` 显式拒绝并关闭关联 Goal。
