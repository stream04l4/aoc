# AOC V5.2 开发思路与工程任务书（认知解绑、跨界探索与真实自我迭代闭环）

- **文档状态**：V5.2 架构定稿草案（待确认后开启 `feature/v5.2` M1 开发）
- **基线版本**：`v5.1.2`（`main @ 0c1dcfd`，`alembic=0011_v5_evolution_control_plane`，`234` 项单元测试通过，`v5_soak_check.py` `steady_state_ok: true`）
- **目标分支与发布**：开发分支 `feature/v5.2` → 目标发布标签 `v5.2.0`
- **数据库迁移约束**：**0 新增 Alembic 迁移**（全部复用 `world_states` JSONB `kind` 及现有 `active_goals` / `agent_tasks` / `agent_executions` / `skill_registry` / `external_playbook` 等表结构）
- **专有模型端点与隔离约束**：严格延续全局规范，AOC 及其手脚专属使用 `http://80.158.109.51:9999/v1`（模型 `Qwen/Qwen3.8-27B-FP8`，API Key `joel`），严禁与其它系统混用或引入外部 API。

---

## 一、V5.2 核心定位：为什么问题不在“模型不够聪明”，而在“五大架构笼子”

在 `v5.1.2` 上线前后连续 **30 小时（`#1652`–`#1990`，339 个周期）** 的生产观测中，AOC 底层系统稳健运行（0 系统报错、0 Traceback、100% 记忆嵌入与 Letta 同步），V5.1 自主社交与情绪联动也稳定跑通。但同时暴露出一组硬数据反差：

- **几乎所有任务被迫走重型编码流水线**：`156` 次执行中 `155` 次被迫调度外部 Codex（平均单次 **`181.6s`**），连纯粹的信息调研、文献对比、给人类写的简报也要在 L1 隔离沙箱硬写 `skill.py + test_skill.py + fixture.json + report.sha256`。
- **任务失败率明显抬升**：近 30h 出现 **`16 FAILED + 1 ABANDONED`（失败率 `11.8%`）** 与 **`33` 次 `orchestrator.attempt_failed`**，多集中于“Codex 试图抓取实时网页或凑断言”的三连失败重试。
- **技能库“多生少用”且演化全败**：`20` 个 `VALIDATED` 技能里 **`17` 个调用次数为 `0`**；全库历史上全部 `5` 次技能升级（`version != 1.0.0`）**100% 回滚为 `DEPRECATED`**（核心原因：`skill.py` 碰撞同名后被盲测旧回归用例）。
- **进化驱动“吃饱躺平”且无法跳出安全圈**：`reproduction` 满意度因静态阈值（`skills: 10`）长期锁定在 **`0.984`**，30h 内生成 **`0` 个**进化目标；即使 Joel 多次提示扩展 AI 热点、心理学、运动等领域，30h 内非安全领域实质产出仍为 **`0`**。

> **核心判断**：`Qwen/Qwen3.8-27B-FP8` 的阅读综合、逻辑推理与自我反思能力已远超当前承载的任务形态；真正锁死 AOC 自主成长的，是架构强加的 **“全 Codex 编码漏斗 + 静态感官配额 + 盲撞式技能演化 + 静态饱腹驱动 + 无真实自我改进出口”** 五个笼子。  
> V5.2 的唯一目标，就是逐一拆掉这五个笼子，并严格守住以下四条架构底线：
> 1. **单一意识主权**：所有研究综合、主题配额与周期性元认知复盘均运行在 `AutonomousLoop` 内，严禁旁路大模型 Daemon。
> 2. **安全边界零退让**：保持 L0–L3 Governance 权限矩阵、`web_search` / `web_fetch_readonly` 的 SSRF/DNS-Rebinding 防护以及 `wrap_untrusted_world_event` 注入围栏。
> 3. **白板隔离守恒**：个人/对话衍生主题组合（`interest_portfolio`）与社交记忆严格禁止跨代遗传，确保 V5 子代白板验收守恒。
> 4. **开关可控、瞬时回退**：四大支柱均配备独立配置开关（`config/settings.yaml` 保守回退默认值，生产 `docker-compose.yml` 显式激活）。

---

## 二、四大核心突破支柱的精确工程设计

### 支柱一（M1）：大脑原生研究与综合路径（Brain-Native Research & Synthesis）

#### 1.1 目标与路由边界
把“纯思考/纯调研/写给人类看的综合分析”从 Codex L1 脚本工厂彻底解绑出来，让 `CognitionBrain` 直接利用已受治理的 L0 只读 MCP 工具与长期向量记忆在 **10–25 秒内** 完成高质量思考输出。

- **何时走 `brain_research`**：概念调研、多篇论文/新闻/网页对比综合、跨界领域探索（AI 动态、心理学、运动、认知科学等）、为 Joel 总结阅读简报、信息缺口收敛总结。
- **何时继续走 `codex`**：明确需要确定性脚本计算、数据解析、沙箱测试验证、可复用算法工具构建、以及 M3 显式定向技能重构（`skill_refactor_target`）。
- **何时走 `skill_first`**：已有匹配 `VALIDATED` 技能可直接零 Codex Token 运行。

#### 1.2 接口与代码改造点
1. **[interfaces.py](file:///home/ubuntu/aoc/app/cognition/interfaces.py)**：
   - 在 `ActionPlan` 新增字段：`execution_mode: str = "codex"`（经校验归一化为 `"codex" | "skill_first" | "brain_research"`）。
   - 新增模型 `ResearchSynthesisContext`（包含目标、`mcp_prefetch_results`、相关向量记忆、活跃调查线索、受众说明）与 `BrainResearchOutput`：
     - `key_findings: list[str]`
     - `synthesis_markdown: str`
     - `open_questions: list[str]`
     - `recommended_actions: list[str]`
     - `cited_sources: list[str]`
     - `confidence: float`
     - `human_brief: str | None = None`
   - 在 `BrainInterface` 新增可继承默认方法 `async def synthesize_research(self, context: ResearchSynthesisContext) -> BrainResearchOutput`。
2. **[brain.py](file:///home/ubuntu/aoc/app/cognition/brain.py)**：
   - 更新 `generate_goal()` 与 `plan()` 系统提示词：明确区分“工程编码任务（`codex`）”“现有技能复用（`skill_first`）”“大脑原生研究综合（`brain_research`）”，并提示 `plan()` 当选择 `brain_research` 时必须附上 1–4 个有效的 L0 `mcp_prefetch_calls`（`web_search` / `web_fetch_readonly` / `git_docs_search`）。
   - 实现 `CognitionBrain.synthesize_research()`：通过 `self._gateway.complete_structured(..., schema_cls=BrainResearchOutput)` 生成结构化研究结论，所有外部抓取上下文统一套用 `<untrusted_world_event>` 围栏与 `_UNTRUSTED_DATA_RULE`。
3. **[loop.py](file:///home/ubuntu/aoc/app/core/loop.py)**：
   - 在 `_run_cycle_locked` 的 dispatch 分支（[`_try_skill_first_execution`](file:///home/ubuntu/aoc/app/core/loop.py#L1492) 并列位置，约 `1192-1231` 行）接入：
     当 `kv3.brain_research_enabled` 为 `True` 且 `(plan.execution_mode == "brain_research" or plan.assigned_agent == "brain_research")` 时，优先执行新增方法 `_try_brain_native_research(...)`。
   - `_try_brain_native_research(...)` 复用 [`_run_mcp_prefetch`](file:///home/ubuntu/aoc/app/core/loop.py#L1623) 获取至多 `max_brain_research_mcp_calls`（默认 `4`）条受治理只读外部证据，结合 `HybridRetrievalEngine` 召回记忆调用 `brain.synthesize_research()`。
   - 复用 `governance.prepare_workspace(str(task_uuid))` 落盘 `research_brief.md` 与 `brief.json`，写入标准化 `agent_tasks`（`assigned_agent="brain_research"`）与 `agent_executions`，再无缝汇入唯一终态收口路径 `TaskFinalizer.finalize_execution()`（保证 `EvaluationResult`、记忆沉淀、信息增益测量与 M4 统计完全统一）。
   - 若 L0 MCP 全部失败且既无可用证据也无上下文记忆，或 `synthesize_research()` 抛出异常，记录可审计降级日志并安全回落，防止产生空壳幻觉成功记录。
4. **[config.py](file:///home/ubuntu/aoc/app/core/config.py)** & **[settings.yaml](file:///home/ubuntu/aoc/config/settings.yaml)**：
   - 在 `KernelV3Settings` 新增 `brain_research_enabled: bool = False`（YAML 默认 `false`，`docker-compose.yml` 默认开启 `AOC_KERNEL_V3__BRAIN_RESEARCH_ENABLED=true`）及 `max_brain_research_mcp_calls: int = 4`。

---

### 支柱二（M2）：动态主题组合（Interest Portfolio）、跨界探索配额与主题疲劳降权

#### 2.1 解决“舒适区死循环”与“社交提示一次性失忆”
1. **持久化跨周期兴趣池 `world_states(kind="interest_portfolio")`**（0 新表）：
   - 当 Joel 在 Telegram 对话中提出想看的研究方向（`adopt_as_goal=true`）或决策卡片下发长期指引时，除了现有的单次 `social_goal_hints`，同时在 `interest_portfolio` 中持久维护主题条目：
     `{topic_key, domain, prompt, origin: "human_seeded" | "self_seeded", priority, status: "active" | "paused" | "explored", touched_count, last_explored_at, created_at}`。
   - **白板隔离守恒**：在 [`blank_slate.py`](file:///home/ubuntu/aoc/app/evolution/blank_slate.py) 将 `"interest_portfolio"` 加入 `_NON_INHERITABLE_WORLD_KINDS`，确保子代不继承父代私人对话衍生兴趣。
2. **领域谱系扩展与疲劳/舒适区检测（[`exploration.py`](file:///home/ubuntu/aoc/app/perception/world/exploration.py)）**：
   - 扩展 `infer_topic_domain()` 与分类器，覆盖：`security_audit`、`ai_industry`、`psychology_cognition`、`sports_health`、`science_general`、`humanities_society`、`engineering_tools`、`databases`、`agent_memory`、`general_tech`。
   - 升级 `ExplorationManager.build_guidance(recent_goals, ...)`：
     - **舒适区连续度**：统计近期连续多少个 `curiosity` 目标落入 `security_audit`（`comfort_zone_streak`）。
     - **同题连续度**：结合 [`topic_diversity.py`](file:///home/ubuntu/aoc/app/observability/topic_diversity.py) 的 `extract_topic_bucket()` 统计当前 `same_topic_streak`。
     - **人类提示饥饿度**：检查是否存在 `status="active"` 的 `human_seeded` 条目连续超过 `portfolio_starvation_cycles`（默认 `6`）个目标未被触达。
     - 当满足 `comfort_zone_streak >= max_same_domain_streak`（默认 `3`）或 `same_topic_streak >= 3` 或 `human_seeded` 严重饥饿时，在 `exploration_guidance` 中输出 `must_pivot_domain: true`、`cooldown_domains`、`human_portfolio_candidates`。
3. **目标生成阶段的确定性执行强约束（[`loop.py`](file:///home/ubuntu/aoc/app/core/loop.py) + [`brain.py`](file:///home/ubuntu/aoc/app/cognition/brain.py)）**：
   - 将 `interest_portfolio` 精简注入 `generate_goal()`（与 `social_goal_hints`、`exploration_guidance` 并列）。
   - 在 `AutonomousLoop` 对 `GoalProposal`（含 `alternative_goals`）进行内核候选评分与确定性仲裁：当 `must_pivot_domain=True` 时，对落入 `cooldown_domains` 的主候选降权，优先选用来自 `alternative_goals` 中未受冷却限制的跨界/人类兴趣候选；目标达成后同步更新 `interest_portfolio` 的 `touched_count` 与 `last_explored_at`。
4. **解绑开放检索域限制（保持 SSRF / Prompt-Injection 防线）**：
   - 明确区分 [`sensors.yaml`](file:///home/ubuntu/aoc/config/sensors.yaml) 的 **被动定期轮询 RSS/Web 源** 与 `brain_research` 使用的 **主动 SearXNG 只读检索（`web_search` / `web_fetch_readonly`）**，避免跨界探索被 7 个固定技术域名白名单卡死；同时保持内网/回环/元数据 IP 拦截、DNS 重绑定校验与 `<untrusted_world_event>` 围栏不变。可在 `config/sensors.yaml` 或配置说明中预留跨界 RSS 扩展位（如通用科技/认知科学信源）。

---

### 支柱三（M3）：显式定向技能重构（Explicit Skill Refactoring）与 `skill_first` 强路由

#### 3.1 修复“5/5 演化全败”的根本设计缺陷
查看 [`app/self_model/skills.py`](file:///home/ubuntu/aoc/app/self_model/skills.py#L618-L649) 与 [`app/skills/evolution.py`](file:///home/ubuntu/aoc/app/skills/evolution.py#L399-L430) 可确认：此前一旦新任务产出的 `canonical_name` 撞车旧技能，系统在 Codex 完全不知情的情况下直接调用 `evolve_skill()`，拿旧技能的 `base_test_code` 强行测试针对新任务写的脚本，必然因签名或断言不兼容而触发 `base_regression_failed` 并写死一条 `DEPRECATED` 记录。

#### 3.2 改造方案
1. **将“盲撞覆写”拆分为两条明确分支（[`app/self_model/skills.py`](file:///home/ubuntu/aoc/app/self_model/skills.py) + [`app/cognition/interfaces.py`](file:///home/ubuntu/aoc/app/cognition/interfaces.py)）**：
   - 在 `ActionPlan` 新增字段 `skill_refactor_target: dict[str, Any] | None = None`（`{skill_key, canonical_name?, reason, target_generalization}`）。
   - **正常探索任务碰撞已有 `VALIDATED` 技能**：不再盲目触发 `evolve_skill()`。记录信息日志 `skill.crystallize_skipped_existing_validated`（若任务工作区确有独立新语义，则在允许前提下保留独立新键；若属高度重复一次性脚本则跳过重复入库），从根源上消除“每次写完相似脚本都多出一条 `DEPRECATED` 垃圾版本”。
   - **显式定向技能重构任务**：当规划器（或 M4 元认知重构队列）明确设定 `plan.skill_refactor_target` 并进入 [`orchestrator.py`](file:///home/ubuntu/aoc/app/orchestration/orchestrator.py) / `governance.prepare_workspace()` 时，内核先把目标基线技能的 `.aoc_base_skill/skill.py`、`.aoc_base_skill/test_skill.py` 及 `input_schema`/`output_schema` 预置进隔离工作区，并向 Codex 明确下达约束：**必须保持既有接口契约并先跑通全部旧回归测试，再泛化参数、输入适应性与新增单测**。此时 `finalizer` 才触发 `evolution_engine.evolve_skill()` 完成 Gate A（旧回归）+ Gate B（新单测）+ 延迟退化门禁晋升。
2. **提升 `skill_first` 强路由覆盖面（[`brain.py`](file:///home/ubuntu/aoc/app/cognition/brain.py) + [`loop.py`](file:///home/ubuntu/aoc/app/core/loop.py)）**：
   - 在 `plan()` 的 `validated_skills` 紧凑上下文中显式透出 `input_schema`、`output_schema`、`invocation_hint` 与 `reuse_count`，提示优先通过 `ActionPlan(execution_mode="skill_first", skill_invocation=...)` 调用已有技能，避免重复造轮子。
3. **低价值 `DRAFT` 自动清理**：
   - 增加轻量卫生清理逻辑：超过 `draft_skill_expiry_hours`（默认 `48h`）仍未通过校验的遗留 `DRAFT` 技能自动标记为 `DEPRECATED`（备注 `stale_draft_expired`），保持技能库精干。

---

### 支柱四（M4）：解冻 `reproduction` 质量驱动 + 周期性元认知复盘（Meta-Reflection Critic）与 L2 升级提案闭环

#### 4.1 让 `reproduction` 从“静态凑数”升级为“质量与迭代内驱力”
查看 [`app/drives/reproduction.py`](file:///home/ubuntu/aoc/app/drives/reproduction.py) 与 [`config/drives.yaml`](file:///home/ubuntu/aoc/config/drives.yaml#L61-L69) 可知，目前只要 `validated_skill_count >= 10` 就把权重 `0.45` 的 `reusable_skills` 拉满至 `1.0`。V5.2 将在严格兼容现有单测默认权重的前提下引入高质量进化维度（在 `kernel_v3.reproduction_quality_scoring_enabled` 下生效）：

- 在 [`app/drives/interfaces.py`](file:///home/ubuntu/aoc/app/drives/interfaces.py) 的 `DriveContext` 增加可选遥测字段（默认 `None`，向后兼容既有评估逻辑）：
  - `skill_reuse_ratio_recent: float | None = None`（近 24h 跨任务技能复用占比 / 有调用记录的已验证技能占比）
  - `execution_reliability_recent: float | None = None`（近 24h 目标达成率与一次性通过率）
  - `playbook_freshness_score: float | None = None`（`external_playbooks` 近期更新与元认知复盘鲜度）
- 当近 24h 失败率偏高（如 `11.8%`）、大量技能长期 `0` 复用、或久未迭代自身策略时，`reproduction` 的赤字与紧迫度自然抬升，从而自主赢下仲裁去执行“定向技能重构”或“系统进化闭环”。

#### 4.2 周期性“元认知复盘（Meta-Reflection Critic）”阶段
在 `AutonomousLoop` 内新增规则门控的周期性元认知复盘模块（新建 [`app/cognition/meta_reflection.py`](file:///home/ubuntu/aoc/app/cognition/meta_reflection.py)，状态持久化于 `world_states(kind="meta_reflection_state")`，零新增 DB 表）：

- **触发门槛（纯规则 0 LLM 空跑保护）**：距上次复盘达到 `meta_reflection_interval_hours`（默认 `12h`）**或**近 12 小时累计失败目标 `>= 5` 个。
- **输入诊断包（只读事实聚合）**：过去 12–24h 的 `FAILED` / `ABANDONED` 目标样本与失败阶段、`orchestrator.attempt_failed` 分布、`llm.schema_retry` 统计、`skill_registry` 零复用及未通过校验技能清单、以及当前 `external_playbooks` 与 `topic_diversity` 摘要。
- **输出三大可执行进化出口（[`BrainInterface.reflect_on_system()`](file:///home/ubuntu/aoc/app/cognition/brain.py)）**：
  1. **L1 Playbook 热进化（自动落地，带自动回滚）**：通过现有 [`PlaybookManager.save_revision()`](file:///home/ubuntu/aoc/app/skills/playbooks.py#L85) 自动修订 `external_playbooks`（如固化“禁止硬抓需登录/反爬实时页面”“纯调研必须走 `brain_research`”“避免脆弱全量文件断言”等规则），受 `scan_playbook_safety`、SHA-256 与性能退化自动回滚保护。
  2. **L1 技能重构队列**：每次复盘可入队至多 1 条具体技能优化目标至 `world_states(kind="skill_refactor_queue")`，由下次 `reproduction` 周期消费并通过 M3 显式定向重构落地升级。
  3. **L2 系统级升级提案卡片（经 Joel 审批）**：当问题根源超出 L1 Playbook 权限（例如建议调整 `sensors.yaml`、`drives.yaml` 权重、`stale_active_goal_hours` 或核心路由提示词），生成结构化 **Telegram L2 进化提案选项卡**（经 `HumanBridge` Channel A 发送，严格限频至多 `1` 条 / `24h`）：用通俗中文写清 **发现的真实痛点数据 + 具体改动建议 + 预期收益 + 回退方式**，并提供 `A. 采纳并生成变更 / B. 延后观察 / C. 驳回并记录原因`（及系统自动附加的 `其它答案`）。

#### 4.3 自我进化可观测性与稳态体检扩展
- **[control_center.py](file:///home/ubuntu/aoc/app/api/control_center.py)**：新增只读接口 `GET /v5/observability/self-evolution`（保持 `SCOPE_READ` 鉴权与 `12` 配置域结构），输出执行模式占比（`codex` / `brain_research` / `skill_first`）、跨界领域分布与 `interest_portfolio` 状态、技能演化成功率与复用率、最近一次 `meta_reflection` 摘要与待审批进化提案。
- **[v5_soak_check.py](file:///home/ubuntu/aoc/scripts/v5_soak_check.py)**：扩展 `v5_2_observability` 巡检项（核验 `brain_research` 失败隔离、长期积压 `DRAFT` 技能数、`meta_reflection_state` 节律、非 `stale` 僵尸目标与 `/v5/observability/self-evolution` 健康响应）。

---

## 三、分阶段实施路线、验收标准与开关清单

### 3.1 里程碑拆解（M1–M4）

| 里程碑 | 范围与核心交付文件 | 硬核验收标准（Unit + Live Probe） |
| :--- | :--- | :--- |
| **M1：大脑原生研究路径** | `interfaces.py`, `brain.py`, `loop.py`, `config.py`, `settings.yaml` | 1. 新增单元回归测试：验证 `brain_research` 复用 L0 `mcp_prefetch`、不启动 Codex、产出 `research_brief.md` + `brief.json` 且走通 `TaskFinalizer`。<br>2. 实连 `Qwen/Qwen3.8-27B-FP8` Smoke：1 个调研类目标在 `<= 35s` 内由大脑完成综合且生成可读中文简报。 |
| **M2：动态兴趣组合与跨界配额** | `exploration.py`, `loop.py`, `brain.py`, `blank_slate.py`, `sensors.yaml` | 1. 单元测试：验证连续 3 个安全领域触发 `must_pivot_domain`，人类对话写入的 `interest_portfolio` 跨周期持久生效，且 `BlankSlateVerifier` 严格拦截 `interest_portfolio` 跨代泄漏。<br>2. 实连 `Qwen/Qwen3.8-27B-FP8`：模拟安全疲劳状态能切换产出跨界主题目标。 |
| **M3：显式定向技能重构与强复用** | `skills.py`, `evolution.py`, `orchestrator.py`, `brain.py`, `loop.py` | 1. 单元测试：碰撞撞车不再产生盲目 `evolve_skill` 回滚；显式 `skill_refactor_target` 预置 `.aoc_base_skill` 并成功升级到 `v1.1.0` 且 `status=VALIDATED`；遗留超期 `DRAFT` 自动清理；`skill_first` 优先匹配已验证技能。 |
| **M4：进化驱动、元认知复盘与面板** | `reproduction.py`, `drives.yaml`, `meta_reflection.py`, `control_center.py`, `v5_soak_check.py` | 1. 单元测试：`InstinctGuard` 全绿；低复用/高失败下 `reproduction` 胜出仲裁；`MetaReflection` 产出受安全扫描保护的 Playbook 修订 + 技能重构队列 + 限频 L2 升级卡。<br>2. 全量 `tests/unit` 零失败 + `v5_soak_check.py` 通过；仅合并部署并观测真实跨界目标与技能升级落地。 |

### 3.2 特性开关与即时无损回退矩阵

| 配置项（`AppSettings` / `settings.yaml`） | YAML 保守默认 | 生产期望默认 | 关闭效果（零数据库回滚） |
| :--- | :---: | :---: | :--- |
| `kernel_v3.brain_research_enabled` | `false` | `true` | 瞬时退回全部走 `codex` / `skill_first` |
| `kernel_v3.interest_portfolio_enabled` | `false` | `true` | 退回 V4.2 `ExplorationManager` 基础统计 |
| `kernel_v3.explicit_skill_refactor_enabled` | `false` | `true` | 停止新式预置定向重构流程 |
| `kernel_v3.reproduction_quality_scoring_enabled` | `false` | `true` | 退回纯静态 `skills/capabilities` 计分 |
| `kernel_v3.meta_reflection_enabled` | `false` | `true` | 暂停周期性元认知复盘与 L2 提案发送 |

---

## 四、V5.2 北极星对比指标（30h 实测基线 → V5.2 目标）

| 观测维度 | V5.1.2 实测基线（30 小时） | V5.2 验收目标（稳定运行 24–48h） |
| :--- | :--- | :--- |
| **纯调研/综合执行耗时** | 全部进 Codex 写脚本，均值 **`181.6s`** | `brain_research` 占比 **`>= 35%`**，研究类目标单次 **`<= 35s`** |
| **目标失败率** | **`11.8%`**（`16 FAILED + 1 ABANDONED` / `144`） | **`<= 5%`**（消灭“抓实时页面写脚本三连败”） |
| **技能演化存活率** | **`0%`**（`5/5` 全部回滚成 `DEPRECATED`） | 定向重构升级成功率 **`>= 60%`**，不再产生盲撞 `DEPRECATED` |
| **已验证技能跨任务复用** | `17/20` 零调用，复用占比仅 **`~6%`** | `skill_first` 复用占比 **`>= 15%`**，活跃技能零调用积压显著下降 |
| **跨界领域多样性** | 非安全领域实质目标 **`0`** | 非安全/人类指引兴趣主题占比 **`>= 25%`**，舒适区连续锁死消失 |
| **真实自我迭代产出** | `reproduction` 锁定 `0.984`（`0` 目标 / `0` L2 提案） | `external_playbooks` 自动迭代可观、出现实质高质量技能升级与 L2 审批提案 |
