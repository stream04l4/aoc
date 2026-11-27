# AOC V5.3 开发思路与工程任务书（技能生态闭环治理与 L2 真实自演化落地）

- **文档状态**：V5.3 架构定稿草案（承接 V5.2 `feature/v5.2 @ c281986`，深化闭环原中长期改进策略的第 3 点与第 4 点）
- **基线版本**：`feature/v5.2`（`c281986`，`alembic=0011_v5_evolution_control_plane`，`252` 项单元测试通过，`v5_soak_check.py` `steady_state_ok: true`）
- **目标分支与发布**：开发分支 `feature/v5.3` → 目标发布标签 `v5.3.0`
- **数据库迁移约束**：**0 新增 Alembic 迁移**（全部复用 `world_states` JSONB `kind`、`skill_registry.evolution_meta`、`public.evolution_proposals`、`event_logs` 与 `decision_logs`）
- **专有模型端点与隔离约束**：严格延续全局规范，AOC 及其手脚专属使用 `http://80.158.109.51:9999/v1`（模型 `Qwen/Qwen3.8-27B-FP8`，API Key `joel`），严禁与 Antigravity / Telegram 或外部 API 混用。

---

## 一、现状盘点：V5.2 已解决什么 vs 问题 3 & 4 剩余的“最后一公里”

在 V5.2（M1–M4）中，我们重点拆除了第 1 个与第 2 个笼子（`brain_research` 原生调研、`interest_portfolio` 跨界配额与舒适区切换），同时在 M3 与 M4 中搭建了“定向重构”与“元反思”的**基础骨架**。  
但如果严格对照此前讨论中确立的 **第 3 点（让技能演化 vNext 与复用真正跑通、保持技能库精干高复用）** 与 **第 4 点（解冻 `reproduction` 质量驱动、结合 `FAILED`/`attempt_failed`/`llm.schema_retry` 做因果复盘、让 L2 升级提案经确认后自动进入测试分支与受控生效）**，V5.2 仍刻意保留了 6 段未封口的“最后一公里”：

| 归属 | V5.2 已落地的能力 | V5.3 必须攻克的“最后一公里”缺口 |
| :--- | :--- | :--- |
| **问题 3<br>（技能生态）** | 1. 禁止盲撞覆盖（`skill.crystallize_skipped_existing_validated`），不再狂刷无意义 `DEPRECATED`。<br>2. 指定 `skill_refactor_target` 时预置 `.aoc_base_skill/skill.py`、`test_skill.py`、`metadata.json` 并通过 Gate A/B 升级 `v1.1.0`。<br>3. `DRAFT > 48h` 超期自动淘汰；`plan()` 透出技能 Schema。 | **缺口 A（无失败现场注入）**：目前 `.aoc_base_skill/` 只放了旧源码与旧测试，没有把历史**真实失败调用入参、异常栈、错误原因**注入工作区——Codex 重构时依然要“盲猜为什么零复用或报错”。<br>**缺口 B（已验证低价值碎片只进不出）**：`20` 个已验证技能里若有 10+ 个连续 7–14 天调用量为 0 的一次性脚本，M3 只清了 `DRAFT`，未解决 `VALIDATED` 碎片合并或低价值淘汰退场机制。<br>**缺口 C（`skill_first` 缺强制预检与 Schema 自愈）**：`plan()` 仍靠大模型自由发挥决定是否用 `skill_first`，没有内核确定性相似度预匹配；一旦 `skill_first` 因少量参数键名/类型偏差失败，缺少自动参数修复，也不会自动反向排入重构队列。 |
| **问题 4<br>（自我迭代闭环）** | 1. 解冻 `reproduction`（接入 `skill_reuse_ratio_recent`、`execution_reliability_recent`、`playbook_freshness_score`、`pending_skill_refactor_count`）。<br>2. 新增规则门控 `MetaReflectionManager`（每 `12h` 或失败激增触发），输出 `playbook_revision`、`skill_refactor_item`、限频 `24h` 的 L2 Telegram 提案卡。<br>3. 提供只读 `/v5/observability/self-evolution`。 | **缺口 D（未聚合底层 Retry/JSON 抖动遥测）**：`MetaReflectionManager` 当前仅聚合 `EventLog(level in error/warning)` 与失败目标；线上高发的 `llm.schema_retry`（思考截断、`char 0`）与 `orchestrator.attempt_failed` 根因聚类（超时 vs 断言卡死 vs 校验报错）尚未结构化喂入诊断包。<br>**缺口 E（无因果归因与失效自动回退）**：M4 能“发出 Playbook/重构建议”，但下一次元反思时没有定量核验“上一轮策略改动后，失败率与重试率究竟是降了还是恶化了”，缺乏失效 Playbook 自动回滚闭环。<br>**缺口 F（L2 卡片停留在人类文字指令，未接通 V5.0 自动改分支+验证）**：Joel 点击 `A. 采纳并生成变更 (推荐)` 后，[human_bridge.py](file:///home/ubuntu/aoc/app/notifications/human_bridge.py#L245-L303) 目前仅向 `human_guidance` 写入一段文本提示，并未联动 V5.0 [proposal_manager.py](file:///home/ubuntu/aoc/app/evolution/proposal_manager.py) 创建 `evolution_proposals`、在隔离 `git worktree` 生成实操补丁并跑完 [constitution_guard.py](file:///home/ubuntu/aoc/app/evolution/constitution_guard.py) 四层零 Token 筛查后回传 `diff + 单测报告` 供二次确认生效。 |

> **V5.3 核心工程目标**：在不引入多大脑旁路、不破坏宪政文件锁、不新增 Alembic 迁移的前提下，把 **问题 3（失败现场注入 + `VALIDATED` 合并/淘汰 + `skill_first` 确定性命中与自愈）** 与 **问题 4（全量故障聚类诊断 + 因果效果归因 + L2 提案直通 V5.0 隔离分支验证与受控生效）** 全部锻造成自动闭合飞轮。

### 四条不可动摇的架构底线

1. **单一意识主权**：技能生命周期扫描、故障遥测聚合、反思效果归因均在 `AutonomousLoop` 内按规则或既有阶段运行，严禁旁路常驻 LLM Daemon。
2. **宪政文件铁锁与两段式人工签字**：`L2EvolutionBridge` 生成的任何代码/配置变更严格限制在 `EVOLVABLE_ALLOWLIST`（`config/drives.yaml`、`app/cognition/brain.py`、`config/sensors.yaml` 的新增允许源等有限可演化白名单，且禁止放宽 SSRF / `blocked_hosts`），**绝对禁止触碰 `CONSTITUTIONAL_FILES`（共 20 份宪法文件）**；且所有 L2 改动必须经过 **“① 确认生成方案 → ② 隔离分支跑通测试并查看 Diff 后批准生效”** 两段人工签字，绝不静默覆盖运行中代码。
3. **白板隔离守恒**：在 [blank_slate.py](file:///home/ubuntu/aoc/app/evolution/blank_slate.py) 将 V5.3 新增的 `world_states` 类型（`"skill_lifecycle_audit"`、`"l2_evolution_bridge_state"`）纳入 `_NON_INHERITABLE_WORLD_KINDS`，确保子代开局零遗传。
4. **独立开关与零损回退**：四大模块全部配备独立 `KernelV3Settings` 布尔开关（YAML 默认 `false`，`docker-compose.yml` 按需开启），关闭即无损退回 V5.2 行为。

---

## 二、四大核心突破支柱的精确工程设计（紧扣问题 3 与问题 4）

### 支柱一（M1 — 问题 3 深化）：失败现场注入重构与零复用 `VALIDATED` 技能生命周期治理

#### 1.1 把“历史失败样本”直接塞进 `.aoc_base_skill/`
当前 [`SkillRegistryManager.stage_base_skill_in_workspace()`](file:///home/ubuntu/aoc/app/self_model/skills.py#L595-L645) 仅拷贝 `skill.py`、`test_skill.py` 与 `metadata.json`。V5.3 M1 将真实运行失败反馈注入 Codex 重构现场：

1. **采集失败样本（[`app/self_model/skills.py`](file:///home/ubuntu/aoc/app/self_model/skills.py)）**：
   - 在 `resolve_validated_skill()` 后，查询该 `skill_id` 最近 **最多 5 条 `passed=False` 的 `skill_invocations` 记录**（含 `output_excerpt`、`error`、`invocation_source`、`created_at`），并合并 `skill_refactor_queue` 中的 `reason` / `target_generalization` 以及相关 `SkillValidationResultRow(passed=False)` 的截断摘要。
   - 对外部异常文案统一做长度裁剪（单条 `<= 400 chars`）与敏感字段脱敏。
2. **工作区落盘（`<.aoc_base_skill/failure_cases.json>`）**：
   - 新增写入 `<workspace>/.aoc_base_skill/failure_cases.json`，同时更新 [`orchestrator.py`](file:///home/ubuntu/aoc/app/orchestration/orchestrator.py) 注入给 Codex 的重构提示，要求其：
     1. 保持原有对外函数名 `execute(data: dict) -> dict` 与已有回归断言全部通过（Gate A）；
     2. 逐条针对 `failure_cases.json` 中的入参缺省键、类型抖动或边界异常进行泛化兼容，并在新版 `test_skill.py` 中补充对应回归用例（Gate B）。

#### 1.2 零复用 `VALIDATED` 技能合并提案与低价值碎片淘汰（`Unused VALIDATED Skill GC`）
30 小时跑出了 17/20 个零调用 `VALIDATED` 技能，必须在不误伤高价值核心工具的前提下做新陈代谢：

1. **生命周期审计方法（`SkillRegistryManager.audit_and_tidy_validated_skills()`）**：
   - 由 [`AutonomousLoop`](file:///home/ubuntu/aoc/app/core/loop.py) 每 `N` 小时（复用 `_last_draft_cleanup_at` 节奏，默认每 `6h` 一次，纯规则 0 LLM）运行，受开关 `kernel_v3.skill_lifecycle_gc_enabled` 控制。
   - **安全保护名单（绝不淘汰）**：
     - 被任何现存技能 `parent_version` 链条引用、或正处于 `skill_refactor_queue(status="open")` 中的技能；
     - 近 `14` 天内有 `reuse_count > 0` 或被 `mcp` / `skill_first` 成功调用过的技能；
     - 为某 `capability_key` 下**唯一仅存**的 `VALIDATED` 锚点技能（保底保留每个能力域至少 1 个可用代表，除非显式被新合并技能替代）。
   - **两条确定性治理路径**：
     1. **合并建议（Consolidate）**：若同一 `capability_key` 下存在 2 个及以上超 `skill_unused_consolidate_days`（默认 `7` 天）零复用且基于 [evolution.py](file:///home/ubuntu/aoc/app/skills/evolution.py#L226) 词法/Schema 相似度 `>= 0.40` 的 `VALIDATED` 技能，自动向 `skill_refactor_queue` 入队合并任务：
        `{"action": "consolidate", "primary_skill_key": ..., "merge_candidates": [...], "reason": "7d zero-reuse overlapping skills"}`；待主技能升级为合并版 `vNext VALIDATED` 后，在 [`finalizer.py`](file:///home/ubuntu/aoc/app/orchestration/finalizer.py) 自动将 `merge_candidates` 标记 `DEPRECATED`（`superseded_by=主技能 ID`）。
     2. **低价值碎片自动退场（Archive Unused Fragment）**：对超 `skill_unused_expiry_days`（默认 `14` 天）仍 `reuse_count == 0`、`invocation_count == 0`、且非该能力域唯一技能的低价值一次性脚本（如含 `once` / `fixture` / `probe` 或被元反思判定为窄域一次性验证），标记 `status="DEPRECATED"`，并在 `skill_registry.evolution_meta` 写入：
        `{"deprecated_reason": "unused_validated_gc", "archived_at": ..., "restorable_snapshot": {...}}`，同时在 `world_states(kind="skill_lifecycle_audit", key="latest")` 记录可审计台账（0 删除代码文件，可随时一键恢复）。

---

### 支柱二（M2 — 问题 3 深化）：`skill_first` 确定性预筛强路由与 Schema 失败自愈闭环

#### 2.1 内核级确定性技能匹配器（`app/skills/matcher.py`）
当前是否走 `skill_first` 全靠 `CognitionBrain.plan()` 在文本 Prompt 中自己回忆与对齐 `input_schema`，导致 94%+ 任务依然默认滑向 Codex。V5.3 新增轻量确定性预筛模块 [`app/skills/matcher.py`](file:///home/ubuntu/aoc/app/skills/matcher.py)：

1. **三信号混合确定性打分（0 LLM 调用）**：
   - 对每个已通过完整性校验的 `VALIDATED` 技能与当前 `GoalProposal`（`title + description + related_key + required_capability`）计算综合匹配分 `match_score in [0, 1]`：
     - **语义与词干重合（0.50）**：复用中英文分词/Jaccard + 已验证描述与 `applicable_scenarios` 关键词匹配；
     - **能力域与历史复利加分（0.30）**：`capability_key` 一致、历史 `reuse_success_count` 成功率加成、无近期连续失败惩罚；
     - **契约可绑定性（0.20）**：技能声明了清晰的 `input_schema`（非空且必填字段 `required <= 4`）或目标明确点名该技能/同类操作。
2. **认知注入与偏离审计（`CognitiveContext.top_skill_match`）**：
   - 在 `AutonomousLoop` 进入 `plan()` 前，将 `top_skill_match = {skill_key, canonical_name, score, input_schema, invocation_hint, matched_reason}` 注入 [`CognitiveContext`](file:///home/ubuntu/aoc/app/cognition/interfaces.py)（当 `score >= skill_match_hint_threshold`，默认 `0.55`）。
   - 在 `brain.py` `plan()` 约束规则中明确：**当 `top_skill_match` 分值达到建议线且任务属于该技能覆盖范围时，必须优先采用 `execution_mode="skill_first"`；若大模型仍决定改走 `codex`，必须在 `ActionPlan.rationale` 说明不适配原因**，内核将审计 `skill_matcher.override` 事件供元反思分析“为什么大模型拒绝复用”。

#### 2.2 `skill_first` 参数自愈（Auto-Heal）与双连败自动排入重构队列
1. **单次确定性 Schema 参数自愈（[`app/skills/runtime.py`](file:///home/ubuntu/aoc/app/skills/runtime.py) + [`app/core/loop.py`](file:///home/ubuntu/aoc/app/core/loop.py)）**：
   - 当 `kernel_v3.skill_first_auto_heal_enabled=True` 且首次 `SkillRuntime` 因入参校验/键名不匹配（`KeyError` / `ValidationError` / 缺可选默认键）失败时，先在本地做 **1 次确定性轻量对齐**：
     - 基于 `input_schema["properties"]` 的唯一候选键或常见别名（如 `url`/`target_url`、`items`/`data`、`text`/`content`、`query`/`q`）自动补全/映射单层入参并重跑 1 次；仍失败才像现在一样干净回退 Codex，并记录 `skill.auto_healed` 或 `skill.auto_heal_failed`。
2. **连续失败自动反向入队重构**：
   - 统计 `skill_invocations` 中同一 `skill_id` 在近 `24h` 内的连续 `passed=False` 次数：一旦达到 **`>= 2` 次**（说明已有需求匹配上了它，但它的入参或逻辑真不好用），**无需等待 12 小时元反思**，立刻以较高优先级（`priority=2`）向 `world_states(kind="skill_refactor_queue")` 写入该技能的重构工单，并附带这 `2+` 次真实失败样本，直通 V5.3 M1 的 `.aoc_base_skill/failure_cases.json` 闭环修复！

---

### 支柱三（M3 — 问题 4 深化）：全量故障遥测诊断（`llm.schema_retry` / `attempt_failed`）与元反思因果归因

#### 3.1 把沉默的模型抖动与重试卡点纳入元反思诊断包
目前 `llm.schema_retry` 只打了 structlog 日志，[orchestrator.py](file:///home/ubuntu/aoc/app/orchestration/orchestrator.py) 的多次尝试失败散落在子表里，元反思看不到全景。

1. **轻量故障结构化审计事件（不新开数据库表）**：
   - 在 [`app/cognition/llm_adapter.py`](file:///home/ubuntu/aoc/app/cognition/llm_adapter.py)（经 `LLMGateway` 回调或钩子）将高信息量结构化重试摘要（仅记录 `operation`、`attempt`、`error_category`: `empty_content | unterminated_json | validation_error`、`prompt_tokens`、`completion_tokens`）写入现有 `EventLog(event_type="llm.schema_retry", level="warning")`（每 `cycle` 去重限流防刷表）。
   - 在 [`app/orchestration/orchestrator.py`](file:///home/ubuntu/aoc/app/orchestration/orchestrator.py) 尝试失败时，确保 `EventLog("orchestrator.attempt_failed", level="warning")` 携带归类 `failure_bucket`（`timeout | verification_failed | sandbox_error | permission_denied`）及目标 `goal_title` 摘要。
2. **升级 [`MetaReflectionContext`](file:///home/ubuntu/aoc/app/cognition/interfaces.py#L379)（[`app/cognition/meta_reflection.py`](file:///home/ubuntu/aoc/app/cognition/meta_reflection.py)）**：
   - 在 `build_diagnostic_bundle()` 中新增三大精确统计切片：
     - `llm_schema_retry_stats`: 按 `operation` × `error_category` 聚合近 24h 重试次数与单次触发耗损；
     - `attempt_failure_clusters`: 按 `failure_bucket`（如 `timeout: 5`、`verification_failed: 4`）归因的重试热区；
     - `previous_reflection_impact`: 上一次元反思落地的 L1 Playbook 变更与技能重构，在随后的运行窗口内究竟带来了正向改善还是负面回归。

#### 3.2 因果效果归因器与退化 Playbook 自动回滚（`meta_reflection_attribution_enabled`）
真正的自我优化必须**检验自己改得对不对**：

1. **前置基线快照持久化**：
   - 每次 `MetaReflectionManager.run_if_due()` 执行并写入状态至 `world_states(kind="meta_reflection_state", key="primary")` 时，同时记录当轮健康基线快照 `baseline_metrics`：`{goal_failure_rate, llm_schema_retry_rate, skill_reuse_ratio, brain_research_ratio, recorded_at}`。
2. **下一轮触发前自动核验归因（`evaluate_previous_reflection_impact()`）**：
   - 经过至少 `6` 小时或 `>= 5` 个新终态周期后，下一次元反思先对照 `baseline_metrics` 计算新窗口指标差值，给出确定性三态评级：
     - `improved`：目标失败率明显下降或技能复用率抬升；
     - `neutral`：无显著变化；
     - `regressed`：目标失败率较基线升高超过 `+8%`（或 `llm_schema_retry_rate` 翻倍）。
   - **自动刹车回退**：若上一轮保存了 L1 `external_playbook` 新版本且当前评价为 **`regressed`**，内核立刻调用现有 [`PlaybookManager`](file:///home/ubuntu/aoc/app/skills/playbooks.py) 逻辑回退该 Playbook 至 `parent_version` 并记录事件 `meta_reflection.playbook_auto_reverted`；若痛点指标持续未解决（`pain_point_resolution_rate` 低），计算 `unresolved_pain_score` 回传给 [`ReproductionDriveEvaluator`](file:///home/ubuntu/aoc/app/drives/reproduction.py)，提高进化欲望紧迫度，倒逼系统切入更深层重构或升级 L2 提案。

---

### 支柱四（M4 — 问题 4 核心突破）：L2 提案直连 V5.0 自动化分支验证与受控生效闭环

这是打通“元认知提出改进建议 → 经人类确认 → 真正在代码/配置层面可验证进化”的决定性一环，全面复用 V5.0 已经写好但此前只对接静态提案的 [`ConstitutionGuard`](file:///home/ubuntu/aoc/app/evolution/constitution_guard.py)、[`ProposalManager`](file:///home/ubuntu/aoc/app/evolution/proposal_manager.py)、[`InstanceSpawner`](file:///home/ubuntu/aoc/app/evolution/spawner.py) 与 [`FitnessArena`](file:///home/ubuntu/aoc/app/evolution/arena.py)。

#### 4.1 标准化可机读 `proposed_change` 载荷（严格锁定安全边界）
在 [`MetaReflectionOutput.l2_upgrade_proposal`](file:///home/ubuntu/aoc/app/cognition/interfaces.py) 中扩展可选结构化字段 `proposed_change`（当开关 `l2_evolution_bridge_enabled=True` 且提案针对明确参数/提示词/策略文件时填写）：

```json
{
  "change_kind": "config_tune | evolvable_code_patch",
  "target_file": "config/drives.yaml | app/cognition/brain.py | ...",
  "hypothesis": "可证伪假说：将 plan() 某提示精简/某权重微调可使 schema_retry 下降 40%",
  "patch_summary": "一句话改动说明",
  "yaml_or_param_delta": {"...": "..."},
  "code_edit_instruction": "限定在单文件局部逻辑的有界修改指令"
}
```

- **硬编码前置安检**：内核先用 [`constitution_guard.py`](file:///home/ubuntu/aoc/app/evolution/constitution_guard.py#L27-L90) 校验 `target_file`——凡涉及 `CONSTITUTIONAL_FILES`、不在 `EVOLVABLE_ALLOWLIST`（及经受控白名单显式许可的 `config/settings.yaml` 指定安全阈值子集，**明确排除 `api.*` / `governance.*` / `sensors.blocked_hosts` / LLM 端点密钥**）的提案一律拒收并记录 `l2_bridge.target_forbidden`。

#### 4.2 两段式人类拍板 + 隔离 Worktree 零 Token 四层筛查流水线（`app/evolution/l2_bridge.py`）

```text
  [MetaReflection] 生成 L2 Telegram 卡片（含通俗痛点+收益+回滚+A/B/C）
           │
           ▼ Joel 点击「A. 采纳并生成变更 (推荐)」
  [HumanBridge.resolve_outreach_choice] ──(第一段签字)──▶ L2EvolutionBridge.on_proposal_adopted()
           │
           ├──▶ 1. 调用 ProposalManager.create_proposal() 入库 public.evolution_proposals (STAGE0_DRAFT)
           ├──▶ 2. 创建临时隔离 git worktree（绝不污染当前主干与运行容器）
           │        • 若为参数微调：应用安全白名单参数变更
           │        • 若为 EVOLVABLE_ALLOWLIST 代码（如 brain.py 提示词）：调用受限 Codex 在隔离 worktree 执行最小补丁
           ├──▶ 3. 调用 ConstitutionGuard 四层 Stage 1 校验（0 生产 Token）：
           │        Layer1 Constitutional 20文件SHA不变 + Layer2 InstinctGuard上下界 + Layer3 AST安全扫描 + Layer4 隔离 pytest 回归测试
           │
           ▼ Stage 1 通过？
      ┌── 否 ──▶ 标为 STAGE1_FAILED 并自动清理 worktree；Telegram 简洁告知失败原因与日志链接
      └── 是 ──▶ 推进至 STAGE1_PASSED，生成 `git diff` 摘要 + 测试报告 + 回滚方案，
                 向 Joel 推送【第二段确认卡】（「批准生效(推荐)」「仅留分支审阅」「作废关闭」）
                       │
                       ▼ Joel 点击「批准生效」（第二段签字）
                 • 推进 ProposalStage -> STAGE2_APPROVED
                 • 对热加载配置项（如驱动参数 / 传感器新公开源）：安全落盘备份并刷新生效
                 • 对核心代码进化分支：可选进入 V5.0 FitnessArena 双盲/影子验证 或 就绪待合入分支并生成一键合入/重启指令
```

#### 4.3 全链路可观测性与稳态巡检扩展
1. **[control_center.py](file:///home/ubuntu/aoc/app/api/control_center.py)**：
   - 严格保持 **12 个 Config Center 配置域**数量不变（新开关挂载在既有 `drives`、`brain_llm` 与 `evolution` 域内）。
   - 扩展 `GET /v5/observability/self-evolution` 只读接口返回：
     - `skills.unused_7d_count`、`skills.consolidated_or_archived_count`、`skills.auto_healed_count`、`skills.matcher_hit_rate`；
     - `meta_reflection.llm_schema_retry_stats`、`meta_reflection.attempt_failure_clusters`、`meta_reflection.last_attribution_verdict`；
     - `l2_evolution_bridge`: 活跃提案阶段统计（`STAGE0_DRAFT` / `STAGE1_PASSED` / `STAGE2_APPROVED` / `PROMOTED`）与隔离 `worktree` 清理状态。
2. **[v5_soak_check.py](file:///home/ubuntu/aoc/scripts/v5_soak_check.py)**：
   - 新增 `v5_3_observability` 检查：核验 **0 遗留孤立 L2 临时 `worktree` 目录**、连续 14 天未使用 `VALIDATED` 积压受控（`<= 10`）、`llm.schema_retry` 24h 峰值预警阈值，确保长期挂机稳健不膨胀。

---

## 三、分阶段实施路线（M1–M4）、验收标准与特性开关清单

### 3.1 里程碑拆解

| 里程碑 | 核心目标与交付文件 | 硬核验收标准（Unit + Live Probe） |
| :--- | :--- | :--- |
| **M1：失败现场注入与技能生命周理** | `app/self_model/skills.py`, `app/skills/evolution.py`, `app/orchestration/orchestrator.py`, `app/orchestration/finalizer.py`, `app/core/loop.py`, `app/evolution/blank_slate.py` | 1. 单测：`.aoc_base_skill/failure_cases.json` 含真实失败入参并随重构完成升版 `v1.1.0`；7 天重叠加注 `consolidate` 并自动将旧候选标 `DEPRECATED`；14 天低价值零调用技能触发 `unused_validated_gc`（保底锚点与最近复用技能豁免）。 |
| **M2：`skill_first` 确定性预筛与自愈闭环** | 新增 `app/skills/matcher.py`, 改造 `app/skills/runtime.py`, `app/cognition/interfaces.py`, `app/cognition/brain.py`, `app/core/loop.py` | 1. 单测：`SkillMatchPreSelector` 对匹配目标稳定返回 `score >= 0.55`；入参微偏差触发 1 次确定性别名自愈成功；连续 2 次 `skill_first` 失败自动进 `skill_refactor_queue(priority=2)`。<br>2. 实连 `Qwen/Qwen3.8-27B-FP8` Smoke：携带 `top_skill_match` 时 `plan()` 命中 `skill_first`。 |
| **M3：全量故障聚类遥测与因果归因回滚** | `app/cognition/llm_adapter.py`, `app/orchestration/orchestrator.py`, `app/cognition/meta_reflection.py`, `app/cognition/brain.py`, `app/drives/reproduction.py` | 1. 单测：`llm.schema_retry`（`empty_content` / `unterminated_json`）与 `orchestrator.attempt_failed` 按桶聚合进 `MetaReflectionContext`；基线对比发现目标失败率升高（`regressed`）时自动将 Playbook 回滚至 `parent_version`。<br>2. 实连 `Qwen/Qwen3.8-27B-FP8` Smoke：针对 `llm.schema_retry` 聚类给出准确诊断与简洁化修订建议。 |
| **M4：L2 提案直连 V5.0 隔离分支验证闭环** | 新增 `app/evolution/l2_bridge.py`, 改造 `app/notifications/human_bridge.py`, `app/evolution/constitution_guard.py`, `app/api/control_center.py`, `scripts/v5_soak_check.py`, `config/settings.yaml`, `docker-compose.yml` | 1. 单测：触碰 `CONSTITUTIONAL_FILES` 或 SSRF 边界的提案直接拒绝；合法 `EVOLVABLE_ALLOWLIST` 提案在 Joel 点击 Option A 后走通 `ProposalManager` → 临时 `worktree` 打补丁 → `ConstitutionGuard` 四层 Stage 1 校验通过 → 推送带 `diff` 的第二段确认卡；`BlankSlateVerifier` 拦截新增 `world_states`；Config Center 保持 **12 域**；全量 `tests/unit` 与 `v5_soak_check.py` 全绿。 |

### 3.2 特性开关与无损回退矩阵

| 配置项（`KernelV3Settings` / `settings.yaml`） | YAML 保守默认 | 生产期望默认 | 关闭效果（零数据库回滚） |
| :--- | :---: | :---: | :--- |
| `kernel_v3.skill_lifecycle_gc_enabled` | `false` | `true` | 停止 `VALIDATED` 合并入队与 14 天未使用技能淘汰（保留 `.aoc_base_skill/failure_cases.json` 只读注入） |
| `kernel_v3.skill_first_auto_heal_enabled` | `false` | `true` | 停止参数别名自愈与连续失败自动入队重构 |
| `kernel_v3.meta_reflection_attribution_enabled` | `false` | `true` | 元反思不做前置基线对比与退化 Playbook 自动回滚 |
| `kernel_v3.l2_evolution_bridge_enabled` | `false` | `true` | 点击 L2 卡片 Option A 退回仅写入 `human_guidance` 文本指引 |

---

## 四、V5.3 北极星验收指标（对比 V5.2 基线 → V5.3 目标）

| 观测维度 | V5.2 上线预期基线 | V5.3 验收目标（稳定运行 48 小时） |
| :--- | :--- | :--- |
| **活跃技能池健康度** | 能停止新增盲撞 `DEPRECATED`，但既有 `17+` 零复用 `VALIDATED` 技能缺乏自然消纳 | 超 7 天重叠碎片完成合并、超 14 天零价值脚本有序归档，**活跃技能库 7d 复用覆盖率 `>= 45%`** |
| **`skill_first` 命中与自愈率** | 依赖大脑自由回忆 Schema，`skill_first` 易因字段偏差回退 Codex | 确定性预匹配引导 + 1 次参数自愈，**同类复用任务 `skill_first` 一次命中率 `>= 75%`** |
| **模型输出抖动（`schema_retry`）** | `v5.1.2` 约 `1.5 次/小时` 截断或空响应，元反思盲区 | 故障遥测可视化并进入因果诊断，**单位周期 `llm.schema_retry` 发生率下降 `>= 40%`** |
| **策略进化安全性与有效性** | L1 Playbook 仅靠执行成功率被动回滚，元反思无前/后对比 | 100% 元反思记录基线快照并归因评级，**退化型 L1 Playbook 在 1 轮复盘内 100% 自动回退** |
| **L2 真实自我迭代落地** | L2 卡片经人类确认后仅形成聊天指引，无自动代码/配置流水线 | **100% 合法 L2 采纳提案自动生成隔离测试补丁**、跑完 `ConstitutionGuard` 四层验证并附 `git diff` 呈交二段确认生效 |
