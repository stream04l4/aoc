# AOC V4.0 开发任务书（自主学习 · 技能进化 · 外接层可控 RSI 版）

**基线版本：** `v3.2.0`（`main @ 7def093`，AOC V3.2 生产运行中）  
**开发分支：** `feature/v4.0`  
**目标版本：** AOC V4.0（Phase 3：自主技能进化、真正复用、外接器官自主增强与可控 RSI）  
**文档状态：** 已获 Joel 批准，按 M0 → M4 分阶段实施

---

## 一、核心宪法：内核归人类，内核外一切可自主进化

V4.0 严格遵守以下不可动摇的权限边界：

1. **人类独占不可变内核（Immutable Kernel — Joel 最高权限）**：
   - 严禁程序在运行期以任何方式修改 **AOC Core 源码**（`app/core/`, `app/drives/`, `app/affect/`, `app/governance/`, `app/storage/`）、**三大内驱力公式**、**Governance L0–L3 安全策略**、**Token/并发硬预算上限**，严禁触碰 **L3 权限**。
2. **程序可无人值守自主修改与增强的四大外接能力层（Autonomous Self-Evolution Zone）**：
   在「隔离沙箱验证 + 确定性指标验收 + 退化自动回滚」护栏内，AOC 享有充分自主进化权限：
   - **① 外接技能资产（Skill Packages）**：自主规范命名、语义去重合并、参数化执行、迭代升级（`v1 → v2`）、自动回归对比晋升/回滚与低价值淘汰。
   - **② 外接工具插件（MCP Tool Plugins）**：自动将 `VALIDATED` Skill 暴露为标准 MCP 可调用工具，并允许在隔离沙箱内自主编写、测试与升级外接只读/沙箱 MCP 工具插件。
   - **③ 外接感知器官（External Perception Sources）**：放开对 7 个固定域名的硬编码死锁；保留**防内网 SSRF + DNS 重绑定防护 + Prompt Injection 隔离 + 只读预算护栏**，允许好奇心自主发现、注册、调优与淘汰外部 RSS / Web / Git 知识源。
   - **④ 外置认知策略与经验手册（External Playbooks / Prompt Strategy）**：允许程序在不改内核代码的前提下，自主沉淀与版本化迭代外置经验策略（按任务/能力域的最佳实践与避坑规则），经真实 `Verified IG / 成功率` 对照验收，变强保留、退化自动回滚。

---

## 二、V3.2 生产 24h 真实基线与 V4 攻关目标

| 维度 | V3.2 实测基线（@ Cycle #661） | V4.0 核心攻关目标 |
|---|---|---|
| **Skill 资产质量** | 41 个结晶（22 `VALIDATED`），80%+ 为长句截断同质脚本，无真实调用统计 | 规范命名 `<domain>.<capability>.<operation>`；冗余同质率下降 **≥ 70%**；至少 **≥ 10** 个技能有真实跨任务复用证据，跨任务复用成功率 **≥ 85%** |
| **Skill 版本进化（RSI）** | 仅 v1.0.0 单次结晶，无迭代/对比/回滚/淘汰 | 完整 `v1 → vNext` 演进流水线（回归测试 + 新边界测试 + 指标对照），自动晋升/回滚与零复用淘汰 |
| **外置策略与工具自增强** | 仅 3 个内置只读 MCP，外接策略不可自改 | `VALIDATED` Skill 自动暴露为 MCP 工具；外置 Playbook 版本化自主迭代与退化回滚 |
| **好奇心外部探索** | `Curiosity = 0.35`，`GOAL_DEDUP_SKIPPED = 181/24h`（重复提案 66%），偏向内部自查 | 限制内部自查占比；外部探索占比 **≥ 50%**；`GOAL_DEDUP_SKIPPED` 下降 **≥ 50%**；自主发现并持续追踪多领域外部主题 |
| **绝对 Token 降本** | `37.97M / 24h`（`codex 20.43M` + `brain 17.54M`），`IG / 1M Tok = 3.06` | 通过 **Skill-first 直接执行（跳过 Codex 重写脚本）+ 分层上下文瘦身**，同等负载下 24h Token 下降 **≥ 25–30%**，`Verified IG / 1M Tokens ≥ 4.5` |
| **能力覆盖** | `coding(61)` 等 5 项已验证；`debugging/code_review/terminal=0`，`research=2` | 通过真实探索与技能进化自然积累 `research / debugging / code_review` 实证样本（不刷离线考题） |

### 2.1 本阶段明确不做（Non-Goals）
- ❌ 不伪造第二个 CLI Agent 凑数（宿主机仅装 `codex`；V4 以 **Skill Runtime 直接执行**作为轻量高速第二执行路径，保留 `AgentInterface` 供未来真实新 Agent 热插拔）。
- ❌ 不写空转的快慢多模型路由（AOC 专属端点当前仅单本地 `Qwen/Qwen3.8-27B-FP8`）。
- ❌ 不新建开放域知识图谱表、离线考试题库表、同义 `skill_versions` 表或 `exploration_topics` 表。
- ❌ 不因 168h 串行前置门禁阻塞迭代（采用离线全量测试 + 故障注入 + 灰度上线 24h 实时对比 + 一键回滚）。

---

## 三、最小数据库增量（1 次向后兼容迁移 `0010_v4_skill_rsi_evolution`）

在既有 38 张表基础上，**仅增列 1 张既有表 + 新增 1 张轻量审计表（共 +1 表）**：

1. **复用并扩展 `skill_registry`**（已有 `(skill_key, version)` 联合唯一约束，天然支持多版本）：
   - `canonical_name` (`VARCHAR(128)`, indexed)：规范名 `<domain>.<capability>.<operation>`
   - `parent_version` (`VARCHAR(32)`, nullable)：演进父版本号
   - `superseded_by` (`UUID`, nullable)：被合并/升级替代的目标 `skill_id`
   - `input_schema` (`JSONB`) / `output_schema` (`JSONB`)：标准输入输出 Schema
   - `invocation_count` (`INT`, default 0) / `reuse_count` (`INT`, default 0) / `reuse_success_count` (`INT`, default 0) / `last_invoked_at` (`TIMESTAMPTZ`, nullable)
   - `evolution_meta` (`JSONB`, default `{}`)：变更摘要、指标对照与自动回滚记录
2. **新增 1 张表 `skill_invocations`**（记录每一次真实 Skill Runtime / MCP 调用，严格区分“被放进 Prompt”与“跨任务真实执行复用”）：
   - `id`, `skill_id` (FK), `skill_version`, `task_id`, `cycle_id`, `invocation_source` (`skill_first | mcp | agent`), `is_cross_task_reuse` (`BOOL`), `passed` (`BOOL`), `latency_ms` (`FLOAT`), `estimated_saved_tokens` (`INT`), `input_digest` (`VARCHAR(64)`), `output_excerpt` (`TEXT`), `error` (`TEXT`), `created_at`
3. **复用既有 `digital_world_state`**：
   - `kind="exploration_topic"`：承载好奇心外部探索主题生命周期（`discovered -> exploring -> verified -> closed`，含领域与冷却）
   - `kind="external_playbook"`：承载程序可自主迭代、带版本号与指标回滚的外置经验策略索引（正文落外接工作区文件 + SHA-256）

---

## 四、M0 → M4 分阶段实施路线与门禁

- **M0（基线冻结与 41 个旧 Skill 自动盘点清洗方案）**：
  - 从 `v3.2.0` 创建 `feature/v4.0`；导出只读 V3.2.0 24h 基线快照 `docs/v4_baseline_snapshot_v3_2.json` 与纯 Schema 备份 `backups/aoc_schema_v3_2_0.sql`。
  - 编写只读审计与规范化脚本 `scripts/v4_skill_audit.py`，自动分类线上全部 41 个旧技能（同质聚类、一次性脚本归档清单、规范化种子映射），输出 `docs/v4_skill_audit_v3_2.json`。
- **M1（外接 Skill Runtime + 自动暴露为 MCP 工具 + Skill-first 真实复用）**：
  - 迁移 `0010_v4_skill_rsi_evolution`；实现轻量外接技能包（`skill.py + test_skill.py + manifest.json`）与隔离 `SkillRuntime`（Schema 校验 + `python -I` 超时沙箱 + 权限预检）。
  - 将 `VALIDATED` Skill 自动注册为可调用 MCP 工具；在 `AutonomousLoop` 实现 **Skill-first Planning**（匹配已有技能直接调用 Skill Runtime，跳过 Codex 重复写代码），落库 `skill_invocations` 并精准统计跨任务复用率。
- **M2（可控 RSI 技能进化引擎：规范化 + 语义去重 + `v1 → vNext` 自动晋升/回滚与淘汰）**：
  - 落地 `<domain>.<capability>.<operation>` 规范命名器 + 向量/Schema 联合语义去重，彻底消灭长句同质技能膨胀。
  - 实现 `SkillEvolutionEngine`：针对失败/低效/高价值技能在隔离区生成 `vNext` → 跑原有回归测试 + 新增边界测试 + 延迟/通过率对照 → **优于旧版自动晋升并指向新版本，退化立即自动回滚**；长期零复用/低成功率自动 `DEPRECATED`（保留历史证据）。
- **M3（外接器官自主增强：外置 Playbook 自进化 + 好奇心外部探索扩张）**：
  - 实现外置 `PlaybookManager`：程序可自主沉淀、修订按能力域划分的外置经验策略（严禁触碰内核源码），每次修改带版本号、SHA-256 与后续周期成功率/IG 追踪，退化自动回滚至上一稳定版。
  - 升级外部感知与好奇心探索：放宽非内网公开知识源的动态注册（保留严格 SSRF / 私有网段 / 元数据阻断 + Prompt Injection 隔离 + 只读预算护栏）；引入“内部自查占比硬上限”与多领域外部探索主题生命周期，让 Curiosity 真正向外生长；允许受控生成/升级外接沙箱 MCP 工具插件。
- **M4（分层上下文瘦身 + Control Center 进化视图融合 + 生产上线）**：
  - 实现分层上下文裁剪（按相关性检索 Top-K Skill 签名与 Playbook 片段，减少冗余 Prompt Token）。
  - 在现有 Control Center 页面（`自我认知`、`环境感知`、`拟人总览`）融合展示：**技能真实复用与版本演进树、外置策略自进化历史、外部探索主题分布与 Token 节省面板**。
  - 通过全量 Python 单元测试、故障注入与前端构建后上线生产，并与 `docs/v4_baseline_snapshot_v3_2.json` 进行 24h 同口径对比验收。

---

## 五、已获 Joel 批准的关键拍板（2026-11-22）
1. **外接层自主进化授权**：严禁修改内核源码与 Governance/L3 策略；允许程序在沙箱验证 + 自动回滚护栏内无人值守自主修改和增强 **① Skill 代码与版本、② 外接沙箱/只读 MCP 工具插件、③ 外部只读感知源（防 SSRF + 注入隔离，放宽 7 域名死锁）、④ 外置认知策略/经验 Playbook**。
2. **务实执行路线**：不伪造第二 CLI Agent，不空转多模型路由；以 **Skill Runtime 直接执行 + 分层上下文瘦身**作为 V4 核心提效与自进化杠杆。
