# AOC V4.0 — 历史里程碑结项核对与后续演进路线图

## 1. 历代核心问题与阶段里程碑结项核对

### 1.1 V1.0 十项技术债（T1–T10，已在 V2.0 全部根治）

| 编号 | V1.0 遗留缺陷 | 解决阶段 | V2.0+ 根治机制 |
|---|---|---|---|
| **T1** | 日预算耗尽被误判为 `Survival CRITICAL`，每 30s 死循环 | P1-1 | Survival 与 Economic Quota 正交解耦；配额耗尽进入 `THROTTLED` 休眠等待重置（**已结项**） |
| **T2** | 跨周期 `IN_FLIGHT` 任务完成后跳过评估与目标结项 | P1-2 | 纯 DB 重构上下文的统一幂等 `TaskFinalizer.finalize_execution()`（**已结项**） |
| **T3** | `kill -9` 导致孤儿进程与执行记录丢失 | P1-2 + P1-3 | 宿主机 `systemd` Runner + cgroup v2 + `ProcessIdentity` (`boot_id + starttime`)（**已结项**） |
| **T4** | 缺乏真实数字世界外部感知管道 | P1-5 | RSS / Git / Web / System 传感器 + SSRF/XXE/双语 Prompt Injection 防护（**已结项**） |
| **T5** | 静态 SelfModel 与词袋 Embedding | P1-4 + P1-7 | 本地量化 ONNX `bge-small-zh-v1.5` + `pgvector`/`pg_trgm` 混合检索 + 动态 SelfModel 同步 Letta（**已结项**） |
| **T6** | Letta 同步阻塞主循环或断网难补齐 | P1-2 + P1-4 | PostgreSQL `SKIP LOCKED` 事务 Outbox + 指数退避 + 幂等 Inbox（**已结项**） |
| **T7** | 单一行动路径、缺乏原子工具层 | P1-6 | 保持未安装的 `HermesAdapter` 禁用；建立 MCP 3 个受控原子工具（**已结项**） |
| **T8** | API 无鉴权、L2 审批缺少重检与通知 | P1-1a + P1-8 | 默认回环 + Bearer RBAC + 强绑定 L2 审批 + `approve_and_dispatch()` + Telegram Webhook（**已结项**） |
| **T9** | LLM 调用分散且缺乏 HTTP 级记账 | P1-1 | `LLMGateway` 统一配额、并发、外部 Codex Token 记账与熔断（**已结项**） |
| **T10** | 外部 Agent 静态能力虚高饱和 `Reproduction` | P1-7 | 彻底剔除静态贡献（`= 0`），重构为 `capability_evidence` 后验 + `SkillRegistry`（**已结项**） |

### 1.2 V2.1 → V4.0 已交付核心能力演进

| 版本 | Tag | 核心交付与生产验证状态 |
|---|---|---|
| **V2.1** | `v2.1.0` | React/Vite 只读 Control Center 全套 11 页面与 `/v2/control-center/*` 观测接口；PostgreSQL 迁移至 `pgvector:0.8.7` |
| **V3.0** | `v3.0.0` | 薄内核 `ActionPreflight`、`GoalDedupEngine`、循环内 L0 MCP 预取、工作区 `skill.py` 自动结晶与 Alembic `0009` |
| **V3.1** | `v3.1.0` | Affect-to-Brain 只读闭环、SelfModel 信心锚点、Scheduler/Orchestrator 情绪微调、多候选目标效用排序、IG 预测误差校准、轻量世界假设 |
| **V3.2** | `v3.2.0` | Control Center 拟人数字生命体解剖图 SVG（`OrganismAnatomy.tsx`）与四大外接器官实时遥测视图 |
| **V4.0** | `v4.0.0` | **当前生产版本**：`0010_v4_skill_rsi_evolution` 迁移；隔离 `SkillRuntime` + MCP 桥接 + Skill-First 零 Codex 执行；`SkillEvolutionEngine` 规范命名/语义去重/`v1->vNext` 回归晋升回滚及线上 68→2 旧技能清理；内核外 `PlaybookManager` 与退化自动回滚；公开 HTTPS 感知扩展 + 强 SSRF 防护与 `ExplorationManager` 防内部自检死循环；`context_compactor` 分层压缩与 Control Center V4 RSI UI 融合 |

---

## 2. V4.0 生产上线观察与 24h 同口径对比清单

V4.0 已冻结上线前基线快照 [docs/v4_baseline_snapshot_v3_2.json](v4_baseline_snapshot_v3_2.json)（V3.2 Cycle #671，24h 消耗 `38.93M` Token、`Verified IG / 1M Tokens = 3.20`、`GOAL_DEDUP_SKIPPED = 181`、`Curiosity = 0.35`）。建议在生产运行 24 小时后重点核验以下同口径指标：

1. **技能真实跨任务复用与省 Token 效果**：检查 `skill_invocations` 中 `is_cross_task_reuse = true` 次数、跨任务复用成功率及累计 `estimated_saved_tokens`。
2. **单位认知效率与总体 Token 降幅**：对比 `Verified IG / 1M Tokens` 是否稳定提升（目标 ≥ `4.5`）及日消耗 Token 降幅。
3. **自进化与回滚稳定性**：观测 `skill_version_trees` 中 `v1 -> vNext` 演进与 `external_playbooks` 自动积累及异常回滚事件日志。
4. **好奇心外部探索扩张**：核对 `Control Center -> 环境感知 -> 好奇心外部探索主题追踪` 中的领域覆盖分布与 `GOAL_DEDUP_SKIPPED` 下降幅度，确认内部自检占比受 `max_internal_curiosity_ratio (0.45)` 有效约束。

---

## 3. 后续演进方向（V4.1+ / V5.0 展望）

1. **受控自主生成与升级沙箱/只读 MCP 工具插件**：在现有 `VALIDATED` Skill 自动暴露为 `skill:<canonical_name>` MCP 工具的基础上，探索针对高频外部只读检索任务自动合成并验证标准 MCP 工具插件包（严守 L0/L1 与 SSRF/沙箱护栏）。
2. **第二个真实独立 `AgentProvider` 接入**：当宿主机部署第二个独立、经实测可靠的 CLI 或 API 自主 Agent 后，通过现有 `AgentInterface` + `AgentRouter` 实现真正的多通用 Agent 协同、差异化专长分工与故障热备。
3. **签名跨实例技能与知识包迁移**：在不允许自我复制进程的前提下，支持对 `VALIDATED` 技能包、高实证表现的外置 Playbook 与高信息增益语义记忆导出为带 SHA-256 清单签名的只读 Knowledge Bundle，便于审计、备份或受控迁移。
