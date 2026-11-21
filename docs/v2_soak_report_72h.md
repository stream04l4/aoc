# AOC V2.0 真实全栈集成验收与 72 小时长稳运行报告

## 1. 当前长稳门禁状态声明（诚实报告原则）

按照《开发任务书 v2.1》第 7 章 P1-10 第 10、12 条纪律（“任何一项 P0 指标未达标均不允许宣告 V2.0 发布，必须如实记录”）：

- **全栈真实组件端到端集成验收（`tests/acceptance/test_p1_10_e2e_full.py`）**：**PASSED（25.40s，零 Mock/Stub）**。
- **10 类真实故障注入套件（`scripts/run_v2_fault_injection.py`）**：**10 / 10 PASSED**。
- **P1-3 真实 OS 级 `kill -9` 矩阵（`tests/integration/test_p1_3_kill9_recovery.py`）**：**18 / 18 PASSED**。
- **全量单元测试套件（`tests/unit/`）**：**140 / 140 PASSED**。
- **72 小时连续挂机物理时长状态**：**`READY_TO_LAUNCH / NOT_YET_ELAPSED_72H`**。
  - 说明：代码、数据库迁移、宿主机 `systemd` Runner、本机 ONNX Embedding、Letta 0.16.4、远端 Qwen vLLM、真实 `codex-cli 0.171.0`、3 个真实外部 Sensor、3 个真实受控 MCP 工具、Telegram Bot (`@Joel_aoc_bot`) 及 72h 自动化守门员监控脚本（`scripts/soak_monitor_72h.py`）已 100% 就绪并通过全链路联调；但 **72 小时（3 昼夜）真实物理挂机窗口尚待 Joel 批准生产数据库迁移与清理后正式启动计时**。

---

## 2. 真实全栈端到端验收实测结果（`test_p1_10_real_full_stack_e2e_acceptance`）

执行命令：
```bash
.venv/bin/pytest tests/acceptance/test_p1_10_e2e_full.py -v
```

本次全栈验收在**零 Mock Brain、零 Stub Agent** 条件下，一次性串联验证了以下全部真实组件：

1. **宿主机 `systemd` Agent Runner**：连接真实 `/run/aoc-runner/runner.sock`，验证 `status=online`、`cgroup_delegated=true`（cgroup v2 子树隔离生效）。
2. **远端 Qwen vLLM 推理网关**：通过 `LLMGateway` 连接 `http://80.158.109.51:9999/v1`（`Qwen/Qwen3.8-27B-FP8`），完成健康探测、任务结果评估（`CognitionBrain.evaluate()`）与 HTTP 级 Token 记账。
3. **真实数字环境感知（P1-5）**：触发 `WorldEventPipeline.poll_all()`，完成本地 Git 仓库、PostgreSQL 官方 RSS 与 Python 3.12 文档源的真实抓取、去重与入库。
4. **3 个真实受控 MCP 工具（P1-6 `ToolProvider`）**：
   - `web_fetch_readonly`：真实抓取 `https://docs.python.org/3/` 并通过 SSRF/IP 校验；
   - `workspace_filesystem`：在隔离任务工作区真实写入并校验文件；
   - `git_docs_search`：在 `/home/ubuntu/aoc` 真实执行只读 `git_log` 检索；
   - 全部调用均生成 `policy_evaluations(subject_type='mcp_tool')` 与 `resource_usage(category='mcp_tool')` 审计记录。
5. **真实 `codex-cli 0.171.0` 闭环执行**：
   - 经 `AgentRouter` 可解释路由选定 `codex`，通过宿主机 `systemd` Runner 在 `/home/ubuntu/aoc-workspaces/...` 隔离工作区启动真实 `codex exec --json`；
   - 成功解析 JSONL 事件流并持久化真实 `thread_id` 至 `agent_executions.thread_id`；
   - `TaskFinalizer` 原子完成确定性校验、真实 LLM 评估、四因子信息增益计算、记忆落库、能力证据 `capability_evidence` 记账与 `self_model_versions` 快照生成。
6. **本机 ONNX Embedding + 真实 Letta 0.16.4 异步投影（P1-4 / P1-7）**：
   - `OutboxDispatcher` 异步消费 `memory.embed`（调用本机 `BAAI/bge-small-zh-v1.5:onnx-qint8` 生成 512 维向量）、`memory.sync_letta` 与 `self_model.sync_letta`；
   - 混合检索引擎 `HybridRetrievalEngine` 真实召回新写入记忆；
   - 真实回读 `aoc-letta` (`127.0.0.1:8283`) 的 `self_model` Core Memory Block，确认动态自我模型已实时同步至 Letta，且测试结束后自动清理临时 Letta Agent。
7. **生命周期、Affect 默认关闭与资源红线**：
   - `GET /v2/lifecycle` 确认 `affect_enabled: false`；`GET /metrics` 输出完整 Prometheus 指标；进程 RSS 远低于红线。

---

## 3. 72 小时真实长稳启动与监控操作手册（Runbook）

待 Joel 批准对生产数据库执行 `pgvector` 镜像升级与 V1.0 测试数据归档后，按以下三步启动 72 小时长稳窗口：

### 步骤 1：生产库备份、迁移与历史测试数据归档
```bash
# 1. 导出升级前完整快照
pg_dump -h 127.0.0.1 -p 5433 -U aoc -d aoc > backups/aoc_pg_pre_v2_clean_$(date +%Y%m%d).sql

# 2. 执行 Alembic 增量迁移至 0008_p1_9_affect (head)
.venv/bin/alembic upgrade head

# 3. 归档并清理非 AOC-001 历史测试数据（保留 AOC-001 全部 29 周期与状态）
.venv/bin/python scripts/archive_v1_test_data.py \
  --execute \
  --archive-out backups/aoc_v1_test_artifacts_archive_$(date +%Y%m%d).json
```

### 步骤 2：确认宿主机 Runner 与核心服务运行
```bash
systemctl status aoc-agent-runner.service
docker compose up -d
```

### 步骤 3：启动 72 小时自动化长稳守门员监控
```bash
nohup .venv/bin/python scripts/soak_monitor_72h.py \
  --api-base http://127.0.0.1:9200 \
  --duration-hours 72 \
  --poll-seconds 300 \
  --output logs/soak_72h_snapshots.jsonl > logs/soak_72h_monitor.log 2>&1 &
```
`scripts/soak_monitor_72h.py` 每 5 分钟自动采样一次全部 12 项硬门禁指标（≥72h、≥100 周期、≥20 规划、≥10 Codex 执行、≥20 环境事件、≥10 Letta 同步、≥10 IDLE、≥3 配额节流、0 周期 ERROR、0 未结项终态任务、0 Outbox 死信、Core RSS ≤ 512MB），72 小时期满且全部检查项为 `true` 时输出 `release_gate_passed: true`（退出码 `0`）。
