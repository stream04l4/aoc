# AOC V3.0 开发任务书（精简内核 · 外接器官版）

**基线版本：** `v2.1.0`（`main @ 9bf548b`，AOC V2.1 生产运行中）  
**开发分支：** `feature/v3.0`  
**目标版本：** AOC V3.0（Phase 2：类人情绪内核、认知提效与标准外接器官闭环）  
**文档状态：** 已获 Joel 批准，进入分阶段执行（M0 → M4）

---

## 一、核心架构宪法：精简类人内核 + 四大外接器官

V3.0 及后续所有版本（含 V4 RSI 自进化）必须严格遵守以下**第一性原理**：

> **内核只做“生命之所以为生命”的最小不可替代部分；所有认知算力、感官、长时记忆与执行手脚，一律通过标准端口外接，绝不把外设能力硬编码进内核，防止系统熵增膨胀。**

```text
                    ┌──────────────────────────────────────────┐
                    │           AOC 精简生命内核 (Core)         │
                    │                                          │
  [外接感知器官] ──► │  1. 内驱力引擎 (Survival / Repro / Cur)  │ ──► [外接大脑器官]
  MCP / 插件 / 外设  │  2. 情绪调制层 (AffectModulator, 有界)    │     LLMGateway (任意模型)
                    │  3. 动机与认知守门 (去重 / 冷却 / IG / Preflight) │
  [外接记忆器官] ◄──►│  4. 自我认知指针 (Self Model & 证据索引)  │ ◄──►[外接行动器官]
  Letta (长期演化)   │  5. 治理底线 (L0-L2 无人值守 / L3 人审)   │     Host Runner + 外部 Agents / MCP
                    └──────────────────────────────────────────┘
```

### 1.1 内核 vs 外接器官边界清单

| 器官 | 定位 | V3.0 实现原则 | 明确禁止（防冗余红线） |
|---|---|---|---|
| **生命内核 (Kernel)** | 欲望 + 情绪 + 动机仲裁 + Preflight + 证据索引 + 安全底线 | 保持轻量、确定性、高内聚、零模型绑定 | 禁止在内核里自研 Agent 框架、自建开放域知识图谱、自建离线考试题库 |
| **外接大脑 (Brain)** | `LLMGateway` | 纯插拔：今天接本地 `Qwen3.8-27B-FP8`，明天可无缝切换任意更强模型 | 禁止把复杂业务逻辑寄托在特定模型提示词链上；27B 弱点用极简确定性校验兜底 |
| **外接感知 (Perception)** | 统一 `PerceptionPort`（传感器插件 + MCP 只读工具 + Webhook） | 外部插件只负责往内核推送标准化 `WorldEvent`；内核只做去重、信任分级、重要度与冷却 | 禁止在 PostgreSQL 新建 `world_entities / world_facts / world_relationships / world_hypotheses` 等重型图谱表 |
| **外接记忆 (Memory)** | `Letta`（长时语义记忆）+ PG（最小运行事实索引 + pgvector） | 让 Letta 承担长期知识组织与回忆，PG 只守状态权威与检索索引 | 禁止在内核里重复建设复杂的记忆推理/实体消歧引擎 |
| **外接行动 (Action)** | 宿主机 `Runner` + 外部 CLI Agents（Codex 等）+ 外部 MCP Servers | 行动全外接；内核仅负责**动作前硬校验（Preflight）+ 动作后确定性验收（Verifier）** | 禁止在内核里再造自研 Agent；不搞脱离宿主机的虚假“多 Agent 凑数” |
| **RSI 自进化基础** | 外部工作区产物 + 内核 `skill_registry` 指针闭环 | 外接 Agent 产出可复用脚本/技能并跑隔离单测；通过安全扫描与单测后自动入库复用 | V3 不允许修改 AOC Core 源码、不允许修改 Governance 策略、不碰 L3 权限 |

---

## 二、V3.0 精简范围（零同义新表，全部基于 V2.1 增量演进）

| 优先级 | 模块 | 针对的 V2.1 真实痛点 | V3.0 极简解法（复用现有资产） |
|---|---|---|---|
| **P0-1** | **内核执行守门：`Plan Preflight`** | L0 `read-only` 计划要求写文件导致 3 次无效重试白烧 20 万+ Tokens | 在现有 `Governance` / `Orchestrator` 前增加极简确定性 `ActionPreflight`（复用 `action_plans`）：校验 Schema、文件写意图 vs 沙箱权限、能力与并发槽位；冲突即刻拦截（`PERMISSION_MISMATCH`），**0 次启动 Agent、0 次无效重试** |
| **P0-2** | **内核认知提效：目标去重 + 确定性 IG v2** | `information_gain = 0.116`，围绕旧话题改写措辞重复空转 | 增强现有 `active_goals`（新增指纹/冷却字段）与 `app/memory/info_gain.py`：结构化指纹 + 语义相似度联合去重、失败/低增益冷却、基于“新记忆向量新颖度 + 话题关闭 + 真实能力证据增量”的确定性 IG 计算 |
| **P0-3** | **内核情绪层：升级并启用 `AffectModulator`** | 情绪系统虽有骨架但未激活（`affect_enabled = false`） | **直接升级现有 `app/affect/modulator.py` 与 `affect_history` 表**（保持 5 维：`valence / arousal / confidence / frustration / boredom`）：接入事件幂等、时间衰减、受控调制三大欲望优先级与探索/重试策略（严禁触碰 Governance 与原始欲望值） |
| **P1-1** | **外接感知与工具口：MCP 接入主循环 + 真实外部源扩展** | `WorldEvent` 稀疏；已有 `app/tools/mcp_manager.py` 未接入主循环 | 将现有 `MCPClientManager` 作为**标准外接感知/只读工具口**接入 `AutonomousLoop`；扩充外部 RSS/Git/Web 感知源（初期聚焦 AI/Agent 工程开源动态 + 宿主机与仓库状态），并预留后续好奇心自发发现新探索主题的扩展口 |
| **P1-2** | **面向 RSI 的最小技能闭环（复用 `skill_registry`）** | `Validated Skill = 0`，`coding/debugging` 为 0 样本，拖累 `Reproduction` | 复用现有 `app/self_model/skills.py`（`skill_registry` + SHA-256 校验）：① 允许 Reproduction 提出低风险 L1 `workspace-write` 真实编程任务，自然产生 `coding / debugging` 真实证据；② 成功后由外接 Agent 提炼候选 Skill 脚本与断言测试，经静态安全扫描 + 隔离单测 + SHA-256 校验通过后，**L0/L1 级自动晋升 `VALIDATED` 并进入复用（免人审）**；L2/L3 涉及外部副作用则保持 `DRAFT` 等待审批 |

### 2.1 本阶段明确不做（Non-Goals）
- ❌ 不在内核自建开放域知识图谱（`world_entities / world_facts / world_relationships / world_hypotheses`）。
- ❌ 不在内核自建 80 道离线固定基准考题库。
- ❌ 不在内核自研新 Agent 框架，也不凑数伪造第二个 CLI Agent。
- ❌ 不新增同义数据库表（不新建 `affect_states`、`skills`、`mcp_tools`、`self_model_capabilities`）。
- ❌ 不把 Control Center 扩成 16 页（仅在现有 10 页内增量融合）。
- ❌ 不允许自主修改 AOC Core 源码、Governance 策略、L3 权限或预算上限。

---

## 三、极简数据库增量（仅 1 次向后兼容 Alembic 迁移 `0009_v3_kernel_enhancement`）

| 现有表 | 增量字段 | 用途 |
|---|---|---|
| `action_plans` | `preflight_status` (`VARCHAR(24)`), `preflight_reason_codes` (`JSONB`), `preflight_checked_at` (`TIMESTAMPTZ`) | 记录 `ActionPreflight` 确定性拦截/放行审计 |
| `active_goals` | `goal_fingerprint` (`VARCHAR(64)`, indexed), `duplicate_of` (`UUID`), `cooldown_until` (`TIMESTAMPTZ`), `predicted_ig` (`FLOAT`), `verified_ig` (`FLOAT`) | 目标去重、冷却与预期/实测信息增益追踪 |
| `affect_history` | `trigger_event_id` (`VARCHAR(128)`, unique nullable), `decay_applied` (`FLOAT`) | 保证同一事件不重复触发情绪更新、支持衰减重放 |
| `resource_usage` | *(复用现有 `details JSONB`)* | 记录 `goal_id / purpose / retry_reason / verified_ig` 实现 Token 效率归因 |

> **新增表数量：`0` 张**；纯增列 Expand-only，100% 兼容 `AOC-001` 全部既有数据，支持随时无损回滚。

---

## 四、Milestone 路线与验收门禁（M0 → M4）

- **M0（基线固化与分支初始化）**：打 Tag `v2.1.0`，创建 `feature/v3.0`，固化统一口径只读度量脚本 `scripts/v3_baseline_metrics.py` 与 V2.1 真实基线快照、导出纯 Schema 备份、通过全部既有测试门禁。
- **M1（内核防翻车与认知提效：P0-1 + P0-2）**：`ActionPreflight` + `GoalDedupEngine` + 确定性 `IG v2` + Token 归因；L0 写文件拦截率 100%（Agent 启动次数 0），≥200 组确定性异常/越权 Fuzz 单测，模拟重复目标拦截率 ≥95%。
- **M2（情绪内核升级与启用：P0-3）**：升级 `AffectModulator`（幂等 + 衰减 + 有界调制），证明永不越权 Governance，融合进 Control Center `Drives` 页；6h 实跑无发散/死锁。
- **M3（外接感知与 MCP 工具口打通：P1-1）**：将 `MCPClientManager` 接入 `AutonomousLoop`（至少 3 个只读 MCP 工具跑通审计），扩充 AI/Agent 开源与宿主机/仓库感知源，预留好奇心自发扩展外部主题的接口。
- **M4（面向 RSI 的最小技能闭环与 24h 全栈验收：P1-2）**：L1 真实编程任务自然积累 `coding / debugging` 证据，候选 Skill 经静态扫描 + 隔离单测 + SHA-256 后 L0/L1 自动晋升 `VALIDATED` 并复用；24h 无人值守全栈门禁通过（Core RSS ≤ 320 MB、Letta 同步 ≥ 99.5%、L0 写文件无效重试 = 0、≥ 3 个 Validated Skill、`Verified IG / 1M Tokens` 较 V2.1 基线提升 ≥ 40%、重复目标下降 ≥ 70%）。

---

## 五、已获 Joel 批准的关键策略拍板（2026-11-21）

1. **Skill 免审批自动晋升边界**：**已批准**——L0/L1 纯本地沙箱内通过静态安全扫描 + 隔离断言单测 + SHA-256 校验的 Skill 自动晋升 `VALIDATED` 并进入复用；仅 L2/L3 外部副作用需人工审批。
2. **外部感知初始主题与演进方向**：**已批准**——M3 初期优先接入 AI/Agent 工程开源动态 + 自身宿主机/仓库状态，同时在架构上保持 `WorldEventSource` 动态可扩展性，为后续版本“好奇心驱动自发探索更多外部主题”预留标准接口。
