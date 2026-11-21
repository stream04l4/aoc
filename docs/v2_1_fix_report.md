# AOC V2.1 问题修复报告

> 版本分支：`feature/v2.1`（基线 `main @ 3de9fae`）
> 生成日期：2026-11-21
> 适用范围：V2.0 残余可靠性问题修复（P0-1 / P0-2 / P0-3 / P1-4 / P1-5 / P1-6）

---

## 1. 修复总览

| 编号 | 级别 | 问题摘要 | 修复状态 | 生产切换状态 | 核心代码/脚本 |
|---|---|---|---|---|---|
| P0-1 | P0 | 重试被替代的 `agent_executions` 缺少 `finalized_at` 终结语义与监控口径混杂 | **已完成并验证** | 待 Joel 批准执行幂等回填 | `app/storage/repository.py`, `app/orchestration/orchestrator.py`, `app/orchestration/finalizer.py`, `scripts/soak_monitor_72h.py`, `scripts/reconcile_v2_1_data.py` |
| P0-2 | P0 | 生产 PostgreSQL 使用 `postgres:16-alpine` 缺少 `pgvector`，向量为 `jsonb` 全表加载 | **已完成隔离演练** | 待 Joel 批准维护窗口切换 | `scripts/migrate_pgvector.py`, `docker-compose.yml`, `docs/v2_1_pgvector_migration_plan.md` |
| P0-3 | P0 | Docker Compose 拓扑不一致、临时 `172.18.0.1:9200` 转发、512d→1024d 零填充 hack、Runner UDS 权限被 uvicorn 改为 `0666` | **已完成并实测容器→宿主机 UDS** | 待 Joel 批准重启 Runner 与迁移 Letta 512d | `app/runner/service.py`, `app/api/app.py`, `app/core/config.py`, `docker-compose.yml`, `scripts/migrate_letta_embedding_512d.py` |
| P1-4 | P1 | 历史 `Organism Identity: AOC-E2E-*` 测试记录残留在 `memory_index` | **已完成幂等归档脚本** | 待 Joel 批准执行（Dry-Run 已识别 3 条，保护 `AOC-001`） | `scripts/reconcile_v2_1_data.py` |
| P1-5 | P1 | 6 小时长稳消耗 1,096.6 万 Token 的根因未定位、缺乏分来源监控 | **已完成根因拆解并接入 Control Center** | 已在 Governance 页实时展示 | `app/api/control_center.py`, `frontend/src/pages/GovernancePage.tsx` |
| P1-6 | P1 | 运行配置（`auto_approve_max_level=L2`、`semantic_policy.enabled=false`、`9999` 限额）缺乏统一可见性与风险提示 | **已完成透明展示（未擅自回退 Joel 配置）** | 已在 Configuration & Governance 页展示 | `app/api/control_center.py`, `frontend/src/pages/ConfigPage.tsx` |

---

## 2. 逐项根因分析与修复验证

### 2.1 P0-1：重试执行记录终结语义与历史数据修复

- **根因**：在 V2.0 中，当某次 `agent_executions` 尝试失败或校验未通过并进入 `RETRY_WAIT` 时，系统仅在任务最终结束时为最后一次 `execution_id` 写入 `finalized_at`，导致前序失败重试记录的 `agent_executions.finalized_at` 永久保持 `NULL`。同时 `scripts/soak_monitor_72h.py` 仅统计 `unfinalized_terminal_execs`，容易将“已被后续重试替代的历史尝试”与“真正未终结的异常任务”混淆。
- **语义规范**：
  - **Task 级终结 (`agent_tasks.finalized_at`)**：仅在任务进入终态并完成 `TaskFinalizer`（结果评估 + 记忆沉淀 + Self Model 证据更新）后写入。
  - **Execution 级终结 (`agent_executions.finalized_at`)**：当某次尝试因重试被替代（进入 `RETRY_WAIT`）时，立即写入 `finalized_at` 并在 `verification` 中标记 `{"superseded_by_retry": true}`；同时在 `TaskFinalizer.finalize_execution()` 中原子补齐该任务下任何早于终态的未封口尝试。
- **代码改动**：
  - `app/storage/repository.py`：新增 `finalize_superseded_executions(task_id, finalized_at, exclude_execution_id=None, session=None)`。
  - `app/orchestration/orchestrator.py`：`_collect_and_decide()` 在写入 `RETRY_WAIT` 时同步终结被替代尝试。
  - `app/orchestration/finalizer.py`：`finalize_execution()` 终结任务时补全前序尝试的 `finalized_at`。
  - `scripts/soak_monitor_72h.py`：保留 `unfinalized_terminal_execs`，同时拆分为 `unfinalized_terminal_tasks`、`unfinalized_execs_parent_open`、`unfinalized_execs_parent_finalized`，不掩盖任何真实异常。
  - `scripts/reconcile_v2_1_data.py`：提供 `--dry-run`（默认）与 `--execute` 幂等回填工具。
- **实测证据**：
  - 单元测试 `test_v2_1_retry_superseded_execution_is_finalized` 与 `test_v2_1_reconcile_script_idempotent_and_protects_aoc_001` 均通过。
  - 对真实生产库 `aoc` 执行 `--dry-run`：精确识别 **15 条** 已被后续重试替代的 `agent_executions`（**0 条** 无法解释的异常未终结记录）。在隔离副本上执行 `--execute` 验证 100% 回填成功且二次执行幂等为 0 变更。

### 2.2 P0-2：PostgreSQL `pgvector` 迁移准备与隔离演练

- **现状确认**：生产容器 `aoc-postgres` 当前运行 `postgres:16-alpine`，`pg_extension` 无 `vector`，`memory_embeddings.embedding` 列类型为 `jsonb`（179 行 512d 向量）。
- **迁移工具与方案**：新增 `scripts/migrate_pgvector.py`（`--sanitize-dump` / `--convert` / `--verify`），并在 `docker-compose.yml` 中预置 `pgvector/pgvector:0.8.7-pg16-bookworm` 标准配置。
- **隔离演练实测结果**（独立容器 `pgvector/pgvector:0.8.7-pg16-bookworm`，端口 `5499`，演练结束自动清理，零触碰生产库）：
  - 逻辑备份 `pg_dump`：15 MB，7,973 行（自动剔除 PG 16 客户端 `\restrict` / `\unrestrict` 元命令 2 行）。
  - 恢复校验：PostgreSQL `16.16 (Debian)`，`en_US.utf8`，扩展 `vector 0.8.7` + `pg_trgm 1.6`，`alembic_version = 0008_p1_9_affect`，**38 张表行数 100% 一致**，`memory_embeddings = 179`，`dim_mismatch_rows = 0`。
  - 类型转换与索引：`jsonb -> vector(512)` 成功，创建 HNSW 余弦索引 `ix_mem_emb_hnsw_cosine` 与 `pg_trgm` 索引 `ix_memory_index_trgm`。
  - 检索实测：`self_recall_at_1 = 1.0`、`self_recall_at_5 = 1.0`、`avg_latency_ms = 1.888 ms`、`p95_latency_ms = 1.77 ms`；`HybridRetrievalEngine` 真实 ONNX + 原生 pgvector 检索命中 5 条（最高综合分 `0.7198`，向量分 `0.5866`，词法分 `0.6905`）。
  - 详细切换与回滚步骤见 `docs/v2_1_pgvector_migration_plan.md`。

### 2.3 P0-3：Core + Host Runner 拓扑统一、`0660` UDS 权限与 512d Embedding 统一

- **uvicorn UDS `0666` 漏洞根因与修复**：
  - 实测发现 `uvicorn.Config.bind_socket()` 在 FastAPI `lifespan` 启动后强制执行 `os.chmod(self.uds, 0o666)`，导致原有 lifespan 内 chmod 失效。
  - 修复：在 `app/runner/service.py` 新增 `bind_uds_socket_0660()`，预先以 `umask(0o117)` 创建并 `chmod(0o660)` Unix Domain Socket，再以 `server.serve(sockets=[bound_sock])` 传入 uvicorn，彻底绕过 uvicorn 的 `0666` 覆盖。单元测试 `test_v2_1_runner_uds_socket_mode_is_0660` 实测权限严格为 `0o660`。
- **容器化 Core -> 宿主机 `systemd` Runner 实测**：
  - 使用独立 `python:3.12-slim` 容器（`--user 1000:1000`）挂载 `/run/aoc-runner:/run/aoc-runner` 与 `/home/ubuntu/aoc-workspaces:/home/ubuntu/aoc-workspaces`，携带 `X-AOC-Runner-Token` 直连宿主机 `systemd` Runner UDS：
    - `GET /v1/health` -> HTTP 200（`cgroup_delegated=true`）
    - `POST /v1/executions` -> HTTP 200（`CONTAINER_TO_HOST_RUNNER_UDS_OK=true`）
- **移除临时转发与 512d→1024d 零填充 hack**：
  - `docker-compose.yml` 统一 Core (`aoc-core`)、PostgreSQL、Letta 同网段编排，Letta 直接访问 `http://aoc-core:9200/v1`，不再依赖 `172.18.0.1:9200` 宿主机桥接转发。
  - `app/api/app.py` `/v1/embeddings` 与 `app/core/config.py` 统一为原生 **512 维**（`aoc-local-embed-512`），彻底移除 512→1024 零填充逻辑，并通过 `test_v2_1_embeddings_endpoint_returns_native_512d_no_zero_padding` 验证。
  - 新增 `scripts/migrate_letta_embedding_512d.py`（已对生产 Letta `agent-2f2d9c34-ad28-4a2b-8799-565e9fa06f2c` 完成 `--dry-run` 验证），待 Joel 批准后执行切换。

### 2.4 P1-4：历史测试身份记录归档（保护 `AOC-001`）

- `scripts/reconcile_v2_1_data.py` 对 `memory_index` 中标题匹配 `Organism Identity: AOC-E2E-*` 的历史测试残留执行**非破坏性归档**（追加 `archived_test_artifact` 标签与降权，绝不物理删除，绝不触碰 `AOC-001`）。
- 生产库 `--dry-run` 准确检出 **3 条** `AOC-E2E-*` 记录，并确认 `AOC-001` 身份记录与历史周期 100% 完好。

### 2.5 P1-5：Token 消耗根因拆解（6h 长稳实测 10.97M Tokens）

对生产 PostgreSQL `resource_usage`（`>= 2026-11-21 01:55Z` 长稳窗口）进行精确归因统计：

| 来源 / 阶段 | 调用次数 | Token 消耗 | 占比 | 单次均值 | 单次峰值 |
|---|---:|---:|---:|---:|---:|
| **Codex CLI 执行器** | 91 | **6,568,615** | **59.9%** | 72,183 | 1,487,092 |
| **Brain Gateway 合计** | 356 | **4,396,947** | **40.1%** | 12,351 | — |
| └ `assess`（局势评估） | — | 1,430,000+ | 13.0% | — | — |
| └ `evaluate`（结果评估） | — | 1,330,000+ | 12.1% | — | — |
| └ `plan`（行动计划） | — | 830,000+ | 7.6% | — | — |
| └ `generate_goal`（目标生成） | — | 810,000+ | 7.4% | — | — |
| **窗口总计** | **447** | **10,965,562** | **100.0%** | — | — |

*(注：截至停机时全历史累计 `resource_usage` 为 `11,357,025` Tokens。)*

- **主要浪费根因**：
  1. **重复探索开放话题**：对尚未关闭的 `p1-8` / `p1-9` curiosity 话题，连续多个周期反复提出近似措辞的新目标，导致 Codex 重复扫描同一代码库。
  2. **L0 `read-only` 沙箱与落盘要求冲突**：部分计划以 L0 `read-only` 模式派发，却要求写入 `result.md`，导致 3 次重试全部失败（单个失败任务白白消耗 230K–284K Tokens）。
- **V2.1 落地能力**：在 Governance 页面实时展示按来源（Codex vs Brain）、按阶段、按 Top 高耗任务拆解的 Token 统计与趋势图，便于 Joel 一眼发现异常重试与高耗任务。

### 2.6 P1-6：安全与预算运行配置的透明展示

- **严格遵守不静默回退原则**：V2.1 **未修改** Joel 明确批准的 `auto_approve_max_level=L2`、`semantic_policy.enabled=false` 及本地 Qwen `9999` 调用/预算限额。
- **透明化呈现**：
  - Configuration Center（`/v2/control-center/config`）按 12 个配置域展示每一项的**当前生效值、配置来源（env / yaml / default）、是否覆盖默认值、是否需要重启、是否敏感脱敏**。
  - Governance & Configuration 页面显式展示风险提示（Risk Advisories），当前生产配置共检出 **3 条** 风险提示（L2 自动放行、语义策略门控关闭、9999 高限额），所有原始密钥 100% 替换为 `***REDACTED***`。
