# AOC V2.0 — V1.0 技术债清零核对与 V3.0 演进路线图

## 1. V1.0 十项核心技术债（T1–T10）清零核对表

| 编号 | V1.0 技术债描述 | 解决阶段 | V2.0 根治方案与验证状态 |
|---|---|---|---|
| **T1** | 每日预算耗尽被当作 `Survival CRITICAL`，导致 `CRITICAL <-> BUDGET_DEFERRED` 每 30s 死循环空转 | P1-1 | Survival 与 Economic Quota 正交解耦；配额耗尽进入 `THROTTLED` 并休眠至 `quota_reset_at`，100 周期耗尽测试 0 空转（**已根治**） |
| **T2** | 跨周期 `IN_FLIGHT` 任务完成后直接 `continue`，跳过评估、世界状态更新、记忆沉淀与 Goal 结项 | P1-2 | 统一幂等 `TaskFinalizer.finalize_execution()` 纯 DB 重建上下文，跨 ≥3 周期长任务 100% 完整走完结项闭环（**已根治**） |
| **T3** | `agent_executions` 进程启动后才建行，Core/Runner `kill -9` 后孤儿进程与状态丢失 | P1-2 + P1-3 | 发射前预写 `STARTING` 行，宿主机 `systemd` Runner + cgroup v2 + `ProcessIdentity` (`boot_id + starttime`)，18/18 真 `kill -9` 零孤儿（**已根治**） |
| **T4** | 外部感知依赖人工 `POST /world` 注入，缺少真实环境传感器 | P1-5 | 实现真实 RSS / Git / Web / System 传感器流水线 + SSRF/XXE/双语 Prompt Injection 防护（**已根治**） |
| **T5** | `SELF_MODEL` 静态不更新、词袋哈希 Embedding、重启硬编码 `_last_info_gain=0.5` | P1-4 + P1-7 | 本机量化 ONNX `bge-small-zh-v1.5`（Recall@5 98%）+ `pgvector`/`pg_trgm` 混合检索 + 持久化信息增益 + 动态版本化 Self Model 经 Outbox 投影至 Letta（**已根治**） |
| **T6** | Letta 写入在主循环内同步等待或断网后历史 `pending` 无法可靠对账 | P1-2 + P1-4 | PostgreSQL `SKIP LOCKED` 事务 Outbox 异步投递 + 指数退避 + 幂等 Inbox，断网恢复 100% 自动补齐（**已根治**） |
| **T7** | `Hermes` CLI 不可用导致缺乏第二条受控执行路径 | P1-6 | `HermesAdapter` 保持 `DISABLED`；通过 `MCPClientManager` 建立 3 个真实受控 MCP 工具作为第二条工具执行路径（`ToolProvider`）（**已根治**） |
| **T8** | API 无鉴权默认绑定 `0.0.0.0`，L2 审批后直接调用私有 `_launch_attempt()` 跳过重检，无外部通知 | P1-1a + P1-8 | 默认绑定 `127.0.0.1` + 全端点 Bearer RBAC + 强绑定审批单 + `approve_and_dispatch()` 四重重检 + Telegram Bot 推送与 Webhook 闭环 + 中英双语语义风控（**已根治**） |
| **T9** | LLM 调用分散且缺乏统一 HTTP 级 Token 记账、并发控制与熔断器 | P1-1 | 全部 LLM 调用（含语义风控与外部 Codex 消耗）统一收敛至 `LLMGateway`（**已根治**） |
| **T10** | `Reproduction` 自动把外部 Agent 静态能力计入 AOC 已获能力，导致默认虚高饱和 | P1-7 | 彻底删除静态能力折叠逻辑（静态贡献 = 0），改由 `capability_evidence` 驱动的 Beta-Binomial `capability_profiles` 与经隔离测试 + SHA-256 校验的 `SkillRegistry` 计算（**已根治**） |

---

## 2. 启动 72 小时生产长稳前的 3 项运维前置确认事项

1. **生产 PostgreSQL 容器镜像切换（`postgres:16-alpine` → `pgvector/pgvector:0.8.7-pg16-bookworm`）**：
   - `docker-compose.yml` 已更新为官方 `pgvector:0.8.7-pg16-bookworm`。由于 Alpine (musl) 与 Debian (glibc) 索引排序规则不同，正式切换生产库时必须通过 `pg_dump` 导出并导入全新数据卷，切勿直接挂载旧 Alpine 数据卷。
2. **生产数据库 V1.0 测试数据归档审批与遗留 Hermes L2 任务裁决**：
   - 待 Joel 批准后，在生产库运行 `scripts/archive_v1_test_data.py --execute` 隔离 `AOC-BENCH-*` / `AOC-E2E-*` 历史测试残留（已在临时库完成 100% 演练核对），并通过审批 API 显式 `reject` 遗留的 Hermes 待审批任务 `c4c48d42-cdde-4fcf-becd-1210d8c074ab`。
3. **Telegram 机器人聊天白名单绑定（`@Joel_aoc_bot`）**：
   - `.env` 中已配置新申请的 Bot Token 与随机 Webhook Secret，`AOC_TELEGRAM__ALLOWED_CHAT_IDS` 当前保持为空（默认安全拒绝一切未授权 Webhook 指令）。向 `@Joel_aoc_bot` 发送 `/start` 获取你的 `chat_id` 并填入 `.env` 后即可激活 Telegram 远程审批。

---

## 3. V3.0 演进建议（Phase 2 展望）

1. **第二个真实通用 `AgentProvider` 接入**：在宿主机部署或接入第二个经过实测验证的独立 CLI/API 通用 Agent（如只读 Research Agent 或修复后的 Hermes 新版本），与 `CodexAdapter` 形成多 `AgentProvider` 协同与故障热备。
2. **Skill 自动合成与沙箱化复用调用**：让 `CognitionBrain.plan()` 在规划阶段自动检索 `skill_registry(status='VALIDATED')` 中匹配的已验证技能，并在校验 SHA-256 后优先复用已沉淀技能脚本以降低 Token 消耗。
3. **跨实例知识包导出与只读迁移（受控虚拟繁衍扩展）**：在严格不自我复制进程的前提下，支持将 `VALIDATED` 的 `skill_registry` 与高置信度语义记忆打包为签名的可验证知识包（Knowledge Bundle）。
