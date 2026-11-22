# AOC V4.0 — HTTP API 完整参考

默认绑定 `http://127.0.0.1:9200`（`docker-compose.yml` 可按需映射宿主机端口；非回环绑定在 `app/api/auth.py` 强制要求开启 `api.auth_enabled=true` 且至少配置管理密钥或数据库有效凭据）。

---

## 1. 认证与 RBAC 权限域（`app/api/auth.py`）

- `GET /health`：公开健康检查接口（无需凭据）。
- 其余全部接口均需携带 `Authorization: Bearer <token>`，由 `AOC_API__ADMIN_KEY`（`admin`）、`AOC_API__INTERNAL_TOKEN`（`internal`）或数据库中仅存 SHA-256 哈希的 `api_credentials` 校验：
  - `read`：只读可观测接口、`GET /v2/control-center/*`、`GET /world` 等
  - `control`：`/control/*` 生命周期控制、世界状态变更、数据源开关/轮询、任务对账/取消、工具调用
  - `approve`：`/approvals/*` 与 `/v2/approvals/*` L2 人工审批决策
  - `internal`：`/metrics`、`/v1/embeddings`
  - `admin`：全权限超集

---

## 2. Control Center 只读可观测接口（V2.1 – V4.0，`app/api/control_center.py`）

> 所有 `/v2/control-center/*` 端点严格只读（`SCOPE_READ`），敏感密钥字段统一返回 `***REDACTED***`，未落库字段显式返回 `null`（绝不伪造默认值）。

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/v2/auth/me` | 返回当前 Bearer Token 主体名称、权限 `scopes` 与来源 |
| `GET` | `/v2/control-center/overview?hours=24` | 拟人生命体全息总览：内核状态、三内驱力及趋势、五维 `affect` 状态，以及 `organs` 四大器官遥测（含 V4 `organs.evolution` 跨任务复用次数/成功率/估算节省 Token、活跃 Playbook 数、`exploration_topics`、单位认知效率 `verified_ig_per_1m_tokens`） |
| `GET` | `/v2/control-center/cycles` | 分页自主认知周期列表（支持状态与时间窗口筛选） |
| `GET` | `/v2/control-center/cycles/{cycle_id}/trace` | 单周期完整 11 步认知链路追踪与输入输出证据 |
| `GET` | `/v2/control-center/drives` | 驱动器实时状态、情绪调制与趋势历史 |
| `GET` | `/v2/control-center/tasks` | 分页任务执行列表 |
| `GET` | `/v2/control-center/tasks/{task_id}/detail` | 任务 16 态状态机事件流、多次尝试记录、验证结果、策略评估、绑定审批单与 Runner 对账信息 |
| `GET` | `/v2/control-center/memory` | 记忆索引、Letta/Outbox 同步状态与 Embedding 模型覆盖率 |
| `GET` | `/v2/control-center/world` | 数字世界传感器状态、`open_public_perception_enabled` 模式开关、`allowed_domains`、内置+桥接 MCP 工具清单、轻量世界假设 `hypotheses`、Prompt Injection 隔离事件，以及 **V4 `exploration` 外部探索多领域追踪摘要与主题列表** |
| `GET` | `/v2/control-center/self-model` | 不可变身份、动态 SelfModel 快照历史、Beta-Binomial 能力画像、`capability_evidence` 账本，以及 **V4 `skill_reuse_summary`（跨任务复用统计+`skill_invocations` 流水）、`skill_version_trees`（`canonical_name` 演进树）与 `external_playbooks`（含回滚统计）** |
| `GET` | `/v2/control-center/governance` | L0–L3 治理审计流水、绑定审批与 Token/资源消耗分析 |
| `GET` | `/v2/control-center/config` | 全系统只读配置视图（自动脱敏密钥并标注覆盖来源与风险提示） |
| `GET` | `/v2/control-center/logs` | 决策日志（`decision_logs`）与系统结构化事件日志（`event_logs`）分页检索 |

---

## 3. 内核状态与运行观测（V1 – V2，`app/api/app.py`）

| Method | Path | Scope | 说明 |
|---|---|---|---|
| `GET` | `/health` | Public | 系统、PostgreSQL、Letta、生命周期与调度器健康状态 |
| `GET` | `/state` | `read` | 生命体身份、生命周期状态、三大驱动器与每日预算状态 |
| `GET` | `/drives` | `read` | 最新持久化 `DriveState` |
| `GET` | `/cycles?limit=20` | `read` | 近期自主认知周期审计记录 |
| `GET` | `/goals?status=&limit=50` | `read` | 活跃与历史目标列表 |
| `GET` | `/tasks?status=&limit=50` | `read` | 任务与验证结果列表 |
| `GET` | `/v2/tasks/{task_id}/lifecycle` | `read` | 任务不可变 `task_lifecycle_events` 状态迁移流水 |
| `GET` | `/v2/tasks/{task_id}/attempts` | `read` | 任务所有 `agent_executions` 执行尝试记录 |
| `POST` | `/v2/tasks/{task_id}/cancel` | `control` | 取消活跃/排队任务并触发终态收敛 |
| `POST` | `/v2/tasks/{task_id}/reconcile` | `control` | 与宿主机 Agent Runner 对账核实执行状态 |
| `GET` | `/v2/runners` | `read` | 宿主机 `systemd` Agent Runner 注册与心跳状态 |
| `GET` | `/v2/runners/{runner_id}/executions` | `read` | 指定 Runner 追踪的执行列表 |
| `GET` | `/agents` | `read` | 注册外部 Agent 及实时探针健康度 |
| `GET` | `/memories?memory_type=&query=&limit=20` | `read` | 记忆索引检索 |
| `POST` | `/v2/memories/search` | `read` | 五维混合（向量+Trigram+衰减+可靠度+重要度）记忆检索 |
| `GET` | `/v2/memories/embeddings/status` | `read` | 当前活跃 Embedding 模型与覆盖统计 |
| `GET` | `/v2/memories/sync/status` | `read` | PostgreSQL 与 Letta Outbox 同步统计 |
| `POST` | `/v2/memories/sync/replay` | `control` | 重放失败或积压的 Embedding / Letta Outbox 事件 |
| `GET` | `/events?limit=50&event_type=` | `read` | 系统结构化事件日志 |
| `GET` | `/decisions?limit=50` | `read` | 仲裁、策略、降级、路由与 Skill-First 决策日志 |
| `GET` | `/v2/lifecycle` | `read` | 生命周期状态、配额限流标记与 `affect_enabled` 状态 |
| `GET` | `/metrics` | `internal` | Prometheus 指标导出 |

---

## 4. 数字环境、工具与自我认知（V2 – V4）

| Method | Path | Scope | 说明 |
|---|---|---|---|
| `GET` | `/world?kind=&status=` | `read` | 查询 `digital_world_state`（含 `knowledge_topic`, `capability`, `hypothesis`, `exploration_topic`, `external_playbook` 等） |
| `POST` | `/world` | `control` | 注入或更新世界状态条目（可选唤醒休眠生命体） |
| `DELETE` | `/world/{kind}/{key}` | `control` | 删除指定世界状态条目 |
| `GET` | `/v2/world/events` | `read` | 去重、评分与溯源后的外部世界事件列表 |
| `GET` | `/v2/world/sources` | `read` | 世界传感器数据源配置、检查点与健康状态 |
| `POST` | `/v2/world/sources` | `control` | 注册或更新外部传感器源（V4 开启公开 HTTPS 感知时允许动态公开 HTTPS 源，但强制校验防 SSRF/私网 IP/元数据） |
| `POST` | `/v2/world/sources/{source_id}/enable` | `control` | 启用指定数据源 |
| `POST` | `/v2/world/sources/{source_id}/disable` | `control` | 禁用/熔断指定数据源 |
| `POST` | `/v2/world/poll` | `control` | 触发故障隔离的外部传感器轮询 |
| `GET` | `/v2/tools` | `read` | 列出所有受控 MCP `ToolProvider` 工具（包含 3 个内置工具及 V4 自动桥接的 `skill:<canonical_name>` 工具） |
| `POST` | `/v2/tools/{tool_name}/invoke` | `control` | 经 `MCPClientManager` 触发 JSON Schema + Governance + Budget 校验执行 MCP 工具 |
| `GET` | `/v2/self-model` | `read` | 最新版动态 SelfModel 快照（`self_model_versions`） |
| `GET` | `/v2/self-model/history` | `read` | SelfModel 历史版本快照 |
| `GET` | `/v2/capabilities` | `read` | Beta-Binomial 实证能力画像与 90% 置信区间 |
| `GET` | `/v2/skills` | `read` | 技能注册表（含 `canonical_name`、复用统计及 SHA-256 完整性校验结果） |
| `GET` | `/v2/skills/{skill_id}` | `read` | 单技能元数据、SHA-256 完整性与隔离断言验证历史 |
| `POST` | `/v1/embeddings` | `internal` | 内部 OpenAI 兼容本地 ONNX Embedding 接口（供 Letta 调用） |

---

## 5. 生命周期控制与治理审批

| Method | Path | Scope | 说明 |
|---|---|---|---|
| `POST` | `/control/pause` | `control` | 暂停自主调度循环（`PAUSED`） |
| `POST` | `/control/resume` | `control` | 恢复自主调度循环（`ACTIVE`） |
| `POST` | `/control/wake` | `control` | 立即唤醒休眠中的生命体 |
| `POST` | `/control/cycle` | `control` | 同步执行 1 个自主周期（已有周期运行时返回 `409 Conflict`） |
| `POST` | `/approvals/{task_id}/approve` | `approve` | 通过 `approve_and_dispatch()` 审批 L2 任务（原子认领并四重重检策略、预算、`plan_hash`、Agent 健康与工作区） |
| `POST` | `/approvals/{task_id}/reject` | `approve` | 拒绝 L2 任务并同步更新 Plan/Goal/Approval 状态 |
| `GET` | `/v2/governance/policies` | `read` | 只读策略规则与语义风控开关状态 |
| `GET` | `/v2/governance/audit` | `read` | 查询 `policy_evaluations` 审计流水（覆盖 `plan` / `approval_recheck` / `mcp_tool` / `skill_execution`） |
| `GET` | `/v2/approvals` | `read` | 查询强绑定 L2 `approval_records` |
| `POST` | `/v2/approvals/{approval_id}/approve` | `approve` | 按 `approval_id` 批准一次性绑定审批单 |
| `POST` | `/v2/approvals/{approval_id}/reject` | `approve` | 按 `approval_id` 拒绝绑定审批单 |
| `POST` | `/v2/webhooks/telegram` | Webhook | Telegram 机器人交互审批回调（Secret Header + `allowed_chat_ids` + 一次性 nonce TTL） |

---

## 6. Web Control Center 单页路由（`app/api/app.py`）

- `GET /` 与 `GET /ui/{full_path:path}`：托管 `frontend/dist/` 编译后的 Control Center 前端静态资源。
