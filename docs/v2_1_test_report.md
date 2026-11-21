# AOC V2.1 测试与真实数据验收报告

> 测试分支：`feature/v2.1`
> 执行时间：2026-11-21
> 总结结论：**全部自动化测试与真实生产数据只读验收 100% 通过（0 失败，0 生产写入，0 密钥泄露）**

---

## 1. 完整测试矩阵

| 测试类别 | 测试命令 | 用例数 | 通过 | 失败 | 跳过 | 环境性质 |
|---|---|---:|---:|---:|---:|---|
| **Python 全量单元测试**（V2 140 + V2.1 新增 6） | `.venv/bin/pytest tests/unit/ -q` | **146** | **146** | **0** | **0** | SQLite / 真实 UDS / FastAPI ASGI |
| **V2 故障注入专项套件**（含真实 `kill -9`、DB/Letta/LLM/Embedding/超时/幂等） | `.venv/bin/python scripts/run_v2_fault_injection.py` | **10** | **10** | **0** | **0** | 真实子进程 `SIGKILL` + 真实 ONNX 模型 |
| **前端 Vitest 组件与安全测试**（AuthGate / RBAC / 配置脱敏 / Mind 未记录诚实展示） | `cd frontend && npm run test` | **2** | **2** | **0** | **0** | Vitest + React Testing Library (`jsdom`) |
| **前端 TypeScript 编译与生产构建** | `cd frontend && npm run build` | **1** | **1** | **0** | **0** | `tsc -b` + `vite build`（2,021 模块，0 错误） |
| **PostgreSQL `pgvector` 隔离全流程演练** | `scripts/migrate_pgvector.py` + `HybridRetrievalEngine` | **1** | **1** | **0** | **0** | 真实生产 `pg_dump` + `pgvector:0.8.7-pg16-bookworm` + 真实 ONNX |
| **容器化 Core -> 宿主机 `systemd` Runner UDS 实测** | `docker run --user 1000:1000 ...` -> `/run/aoc-runner/runner.sock` | **1** | **1** | **0** | **0** | 真实 Docker 容器 + 真实宿主机 `systemd` Runner |
| **Control Center 真实生产数据全端点只读验收** | `verify_live_control_center.py`（`AOC_ENABLE_SCHEDULER=false`） | **13 端点 + SPA** | **全部 200** | **0** | **0** | **真实生产 `aoc-postgres`（0 写入变更）** |

---

## 2. V2.1 新增自动化测试用例明细

### 后端新增（6 项）
1. `tests/unit/test_v2_1_reliability_fixes.py::test_v2_1_retry_superseded_execution_is_finalized` — 验证任务失败进入 `RETRY_WAIT` 时前序尝试自动打上 `finalized_at` 与 `superseded_by_retry=true`。
2. `tests/unit/test_v2_1_reliability_fixes.py::test_v2_1_reconcile_script_idempotent_and_protects_aoc_001` — 验证历史修复脚本幂等性，以及对 `AOC-001` 身份记录的严格保护。
3. `tests/unit/test_v2_1_reliability_fixes.py::test_v2_1_runner_uds_socket_mode_is_0660` — 启动真实 uvicorn Runner 实例验证 Unix Domain Socket 权限严格为 `0o660`（非 `0o666`）。
4. `tests/unit/test_v2_1_reliability_fixes.py::test_v2_1_embeddings_endpoint_returns_native_512d_no_zero_padding` — 验证 `/v1/embeddings` 返回原生 512 维向量且不存在零填充。
5. `tests/unit/test_v2_1_control_center_api.py::test_v2_1_control_center_api_auth_redaction_and_full_trace` — 覆盖全部 13 个接口的 401/403 RBAC 拦截、6 类真实密钥全量脱敏、11 步轨迹及跨实体跳转。
6. `tests/unit/test_v2_1_control_center_api.py::test_v2_1_frontend_static_fault_isolation` — 验证 `frontend/dist` 缺失时 `/health` 与核心 API 100% 正常响应。

### 前端新增（2 项）
1. `frontend/src/__tests__/ControlCenter.test.tsx::shows Bearer Token AuthGate when unauthenticated and logs in via /v2/auth/me` — 验证未认证拦截至 AuthGate、登录后加载总览数据、配置中心正确展示 `***REDACTED***` 与风险提示。
2. `frontend/src/__tests__/ControlCenter.test.tsx::enforces read-only UI for SCOPE_READ users and renders explicit 未记录 for null Mind steps` — 验证 `SCOPE_READ` 只读账号隐藏管理员控制按钮，且 Mind 页对 `null` 步骤明确展示“未记录”（8 个未记录 / 3 个已记录，不伪造任何推理文本）。

---

## 3. 真实生产数据全栈验收证据（只读，0 变更）

在**不启动调度器、不执行任何写入**的前提下，直接对真实生产 PostgreSQL（`aoc`，含 `AOC-001` 全部真实运行数据）发起全端点与静态 SPA 实测：

### 3.1 全端点响应与耗时

| 接口 / 资源 | HTTP 状态 | 耗时 (ms) | 载荷大小 (bytes) |
|---|---:|---:|---:|
| `GET /`（Control Center SPA `index.html`） | **200** | — | 508 |
| `GET /v2/control-center/overview`（无 Token） | **401** | — | — |
| `GET /v2/auth/me` | **200** | 0.8 | 60 |
| `GET /v2/control-center/overview?hours=0` | **200** | 96.0 | 37,551 |
| `GET /v2/control-center/cycles?hours=0&limit=10` | **200** | 66.0 | 5,024 |
| `GET /v2/control-center/drives?hours=0` | **200** | 40.7 | 224,222 |
| `GET /v2/control-center/tasks?hours=0&limit=10` | **200** | 17.6 | 11,854 |
| `GET /v2/control-center/memory?limit=10` | **200** | 26.3 | 32,056 |
| `GET /v2/control-center/world?hours=0&limit=10` | **200** | 14.1 | 68,081 |
| `GET /v2/control-center/self-model` | **200** | 70.7 | 57,869 |
| `GET /v2/control-center/governance?hours=0` | **200** | 31.7 | 22,812 |
| `GET /v2/control-center/config` | **200** | 8.5 | 21,990 |
| `GET /v2/control-center/logs?hours=0&limit=20` | **200** | 73.7 | 13,641 |

### 3.2 真实 Cycle 端到端链路追踪（Cycle #141）

- **Cycle ID**：`53ed304d-5a9b-487f-b54c-a4bc58d4bf3f`（`Cycle #141`，状态 `SUCCEEDED`）
- **11 步决策轨迹完整落盘**：`1_perception` → `2_drive_assessment` → `3_arbitration` → `4_brain_assessment` → `5_goal_generation` → `6_planning` → `7_governance` → `8_agent_dispatch` → `9_result_verification` → `10_outcome_evaluation` → `11_post_perception`（**11/11 步真实记录**）
- **关联任务与执行**：Task `af636bfe-7b57-4eda-afdb-ded893550868`（`SUCCEEDED`，执行器 `codex`，宿主机 Runner `host-runner-1`，1 次执行即通过校验）

### 3.3 真实库统计快照与只读零变更校验

- **真实生产库体量**：`cycle_records = 131`、`agent_tasks = 82`、`agent_executions = 96`、`memory_index = 183`（`synced = 183`，`embedded = 179`，`pending = 4`）、`event_logs = 731`、`capability_profiles = 9`、`world_event_sources = 3`、全历史 Token = `11,357,025`。
- **只读零变更证明**：验收前后 `cycle_records` / `agent_tasks` / `agent_executions` / `memory_index` / `event_logs` 五张核心表行数 **100% 一致（mutations = 0）**。
- **真实密钥脱敏检查**：扫描生产 `.env` 中全部 5 项真实密钥/密码，在 `/v2/control-center/config` 12 个配置域输出中 **泄露数量 = 0**。
- **高频轮询内存稳定性**：连续 20 轮并发拉取 `overview` + `tasks`，平均每轮耗时 **47.1 ms**（峰值 107.4 ms），进程 RSS 增量 **0 KB**（`450,184 KB -> 450,184 KB`）。

### 3.4 浏览器可视化截图说明（诚实披露）
当前宿主机为无图形界面的纯命令行 Linux 服务器，未安装 Chromium / Playwright / X11 浏览器二进制文件，因此无法在宿主机本地截取浏览器 PNG 像素图。前端构建产物（`frontend/dist`）、SPA 静态服务（HTTP 200）、全部 13 个真实生产数据接口以及 React DOM 渲染测试均已 100% 通过验证；Joel 可直接在本机执行 `ssh -N -L 9200:127.0.0.1:9200 ubuntu@<HOST>` 后在桌面浏览器访问 `http://127.0.0.1:9200/` 查看全部 10 个页面实时渲染效果。
