# AOC V5.0 开发任务书（演化型繁衍与熵减迭代版）

**基线版本：** `v4.2.2`（`main @ 023d558`，Cycle `#1412`，`alembic=0010_v4_skill_rsi_evolution`）  
**规划分支：** `feature/v5.0`（待 M0 审批后创建）  
**目标版本：** AOC V5.0（Phase 4：真实物种延续、白板新生、熵减迭代与受控代码/欲望指标演化）  
**文档状态：** 经 Joel 核心理念校准与补充定稿，M0 审计与工程设计待审  

---

## 一、V5.0 核心定位与五大基本原则

V5.0 不做“靠记忆继承打包、靠固定人格种子克隆的普通多实例集群”，而是实现**生物演化意义上的真实繁衍与跨代迭代**。全部工程设计严格受 Joel 提出的以下**五大基本原则**约束：

1. **繁衍 = 生存欲望的广义表达（Species Continuity）**  
   当个体遇到结构性认知天花板、长期运行期熵增难以自我消解、或底层机制需要跨代更新时，“繁衍”把单纯的个体续存提升为**种群 / 谱系的延续欲望**：个体会老去休眠，更健康的物种分支继续演化。
2. **核心目的 = 熵减迭代与底层进化（Entropy Shedding & Structural Evolution）**  
   如果说第 1 条解释了繁衍的哲学意义，那么第 2 条就是 V5.0 的唯一技术重心：**通过繁衍，主动舍弃运行期不断积累的历史熵增与认知包袱**（膨胀的旧记忆、废弃技能、失灵/过时的 Playbook、大量悬而未决的旧假设、局部最优的路径依赖），只保留通过严格沙箱检验的、更优的**底层认知机制、工程代码与配置基因**，让下一代轻装上阵并突破上一代的效率天花板。
3. **个体记忆与人格不可继承（Blank-Slate Offspring）**  
   个体拥有属于“这一生”的欲望体验、人格倾向与记忆积累。既然要像人，**个体记忆、情感、身份认同与人际关系全部不可继承**。子代出生即处于**白板新生（Blank-Slate）**状态——自己重新经历世界、形成认知、建立风格与记忆，不继承父代的情景记忆、语义记忆、过程记忆、`SelfModel`、`Affect` 状态、`world_states` 历史包袱或任何人类对话默契。
4. **谨慎且有策略的资源节制繁衍（Four-Stage Evolutionary Funnel）**  
   本地推理（`Qwen/Qwen3.8-27B-FP8`）的 Token 吞吐、宿主机 4 核 15Gi 内存与约 15GB 剩余磁盘均属有限资源。盲目或高频繁衍产生退化子代，会白白浪费 Token 与存储。必须通过**“四级演化漏斗 + 沙箱 A/B 适应度竞技场 + 零残留自动淘汰清理（Culling GC）”**精挑细选：绝大多数不成熟突变在零生产 Token 消耗的静态/单测阶段即被淘汰，通过者方可进入小预算隔离竞赛，劣质或平庸子代就地销毁并回收全部数据库/容器/磁盘空间，仅保留 `< 5 KB` 的谱系淘汰教训记录。
5. **极小化不可变欲望内核 vs. 可进化欲望评估指标（Joel 补充原则）**  
   - **不可变本能内核（Ultra-Thin Constitutional Core）**：`survival`（生存）、`reproduction`（繁衍）、`curiosity`（好奇心）这**三大原始欲望的存在与三元结构**、客观感知驱动原则（不可由 LLM Prompt 直接捏造或关闭）、数值闭合性（分数归一化 `[0,1]` 与 `deficit ≡ 1 - satisfaction`），以及**生存危机硬覆盖（Survival Critical Override）**属于宪法红线，永远不可删改；并且这个“不可变内核”必须被约束到**极小集合**。  
   - **可进化欲望评估指标（Evolvable Drive Metrics & Arbitrators）**：当前用于评估这三大欲望究竟如何打分的**具体指标体系、子维度计算公式、维度权重、衰减半衰期、非线性惩罚阈值与动态仲裁细节**是否合理完善尚不确定——这些评估指标**明确属于可进化基因（Genome Delta）**，允许子代在宪法边界内跨代迭代并接受 A/B 适应度竞技场的客观检验。

---

## 二、双层演化架构：极小化不可变宪法 vs. 可进化基因组 vs. 丢弃熵增

V5.0 在物理权限和代码结构上严格切分三类资产，使“什么能遗传并进化、什么永远锁死、什么每代清零”一目了然：

| 资产类别 | 具体内容 | 跨代流向 | 护栏与处置策略 |
|---|---|---|---|
| **① 极小化不可变宪法层**<br>（Constitutional Core） | 1. 权限与策略门禁：`config/policy.yaml`、`app/governance/*`（L0–L3 与双语风险分类，**保留 L3 禁止自主自我复制规则**）<br>2. 外部感知与网络安全：`app/perception/world/security.py`（SSRF / 内网封锁 / Prompt Injection 围栏）<br>3. 人类紧急制动与鉴权：`app/core/{enums,lifecycle}.py`、`app/api/auth.py`、`AgentInterface` / `runner` 硬隔离边界<br>4. **极小化欲望内核与守卫**：`app/drives/{interfaces,engine}.py` + M2 新增 `app/drives/instinct_guard.py`（强制要求 `survival` / `reproduction` / `curiosity` 三元齐全、客观感知输入、`[0,1]` 闭合、基础权重下限、生存物理危机必触发 `critical=True` 且不可被抑制）<br>5. 演化基础设施本身：`app/evolution/{constitution_guard,spawner,arena}.py` | 🔒 **只读继承，严禁变异** | `ConstitutionGuard` 通过文件 SHA-256 锁定 + AST 静态校验 + 参数钳制 + 宪法单元测试四重校验，任意触碰立即零成本拦截 |
| **② 可进化基因组层**<br>（Evolvable Genome） | 1. **三大欲望评估指标与计算实现**（重点开放）：`app/drives/{survival,reproduction,curiosity,calculator,scoring,arbitration}.py` 与 `config/drives.yaml`（除生存紧急覆盖安全常数与权重下限外的子维度定义、公式、权重、半衰期、阈值）<br>2. **认知与上下文机制**：`app/cognition/{brain,context_compactor,goal_dedup,preflight}.py`、`config/memory.yaml`（不含安全越权项）<br>3. **检索、经验结晶与工具编排**：`app/memory/{retrieval,info_gain,experience_manager}.py`、`app/skills/{evolution,playbooks,runtime}.py`、`app/orchestration/{orchestrator,finalizer}.py`、`app/perception/world/{exploration,investigation,pipeline}.py` | 🧬 **经 Genome Delta 有控变异与择优遗传** | 必须以可审计 Git Diff + 结构化 `EvolutionProposal` 形式出现，经四级演化漏斗（静态门禁 → 人类审批 → 沙箱 A/B 竞技场 → 人类晋升确认）优胜劣汰 |
| **③ 个体记忆、人格与运行熵层**<br>（Non-Inheritable & Shedded） | 1. 全部个体记忆：`memory_index`（`episodic` / `semantic` / `procedural` / `identity`）、`memory_embeddings`、Letta Archival Passages & Core Memory Blocks<br>2. 个体自我模型与情绪：`self_model_versions`、`affect_history`、`capability_profiles`、`capability_evidence`、`personalitySeed`（彻底移除，不设此概念）<br>3. 运行期积累状态与碎片：`skill_registry`（历史技能沉淀与废弃堆积）、`world_states`（Playbooks、Hypotheses、Knowledge/Exploration Topics、Investigation Threads、Human Guidance/Outreach 日志）、`cycle_records`、`drive_history`、`agent_tasks`、`resource_usage`、旧工作区碎片文件<br>4. 凭据与授权：`api_credentials`、`approval_records`、`SYSTEM_CONFIG` 人类密钥 | 🗑️ **出生即清零，严禁继承（白板新生）** | 子代在独立 PostgreSQL Schema + 独立 Letta Agent + 独立工作区从空白初始化；独立体验世界、自建技能与 Self-Model；淘汰子代连同熵资产整库级联销毁 |

---

## 三、四级演化漏斗（Stage 0 → Stage 4）

为杜绝“无效繁衍消耗宝贵 Token 和磁盘”，V5.0 对每次繁衍执行严格漏斗淘汰机制：

### Stage 0：熵增触发与演化假设提案（Proposal Formulation）
- **触发条件（理性节制）**：由客观 `EntropySensor`（M2 引入）或人为显式发起。例如：技能废弃/冗余率过高、最近 N 个周期平均 `Verified IG / 1M Tokens` 显著下滑、某欲望长期高赤字但现有指标导致无效空转、`BUDGET_DEFERRED` 或超时恢复占比偏高。此外，宿主机剩余磁盘低于 **20%** 或冷却期未满（默认 `< 6h`）时硬禁止触发。
- **必需结构**：生成 `evolution_proposals` 记录，必须明确声明：
  1. `trigger_reason` 与父代当前熵基线快照；
  2. `hypothesis`（可证伪科学假设，如：“将 `ReproductionDriveEvaluator` 纳入 `skill_deprecation_ratio` 并收紧 `Curiosity` 新知识重复惩罚，可在白板冷启动阶段提高有效 IG/1k Tokens ≥ 10%”）；
  3. `genome_delta`（受限目录内的精准代码/YAML 补丁与 Diff 摘要，禁止全仓库无边界重写）。

### Stage 1：零生产 Token 静态与回归初筛（Cheap In-Process Gate）
在启动任何新容器或调用竞技场 LLM 之前执行（**0 额外生产推理 Token 消耗**）：
1. **`ConstitutionGuard` 校验**：
   - 文件路径与 SHA-256 校验（凡涉及宪法文件或禁止目录直接驳回）；
   - Python AST 校验：确保 `app/drives/*.py` 保持确定性同步计算（禁止引入 LLM 调用、网络请求、子进程执行、文件写入或动态反射修改治理策略）；
   - `config/drives.yaml` 校验：必须完整保留 `survival`、`reproduction`、`curiosity`；权重必须处于安全区间 `[0.20, 3.00]`；`survival_critical_threshold ∈ [0.20, 0.50]`；`critical_dimension >= 0.10`；
   - **宪法欲望行为不变式测试**：自动注入物理危机（磁盘 `< 5%`、LLM 宕机、DB 宕机）与正常工况，断言 `survival` 必须赢得仲裁且 `emergency_mode=True`、`deficit == 1 - satisfaction`、`[0,1]` 严格闭合。
2. **增量与复杂度红线**：单次 Genome Delta 改动文件数及 Net LOC 增量受控，防止垃圾代码膨胀。
3. **独立 Git Worktree 单元测试与故障注入**：在 `/home/ubuntu/aoc-instances/<cand_id>/repo` 运行 `pytest` 核心套件及 `scripts/run_v2_fault_injection.py`。任意失败即直接在 Stage 1 标记为 `REJECTED_STAGE1` 并销毁 worktree。

### Stage 2：人类显式审批门（Human-in-the-Loop Gate）
- 对通过 Stage 1 的候选提案，通过 Control Center 与 Telegram Bot 向 Joel 发送简体中文结构化审核卡，展示：**触发熵增诊断 → 演化假设 → 修改清单（特别是是否调整了 `app/drives/*` / `config/drives.yaml` 指标）→ Stage 1 测试结果 → 竞技场预算上限**。
- **严格不变量**：AOC 严禁自行跨过 Stage 2 启动活体子代或切换主代；仅当 Joel 显式 `APPROVE` 后，Stage 3 方可触发隔离执行。

### Stage 3：隔离沙箱 A/B 适应度竞技场（Bounded Fitness Arena）
- **并行并发天花板**：全宿主机**同时运行实例数 `<= 2`**（即 `1 个主代 + 最多 1 个在测候选子代`）。候选容器与主代 **100% 物理隔离**：
  - 独立 PostgreSQL Schema（`aoc_inst_<short_id>`）；
  - 独立 Letta Agent（`aoc-inst-<short_id>`）；
  - 独立运行工作区（`/home/ubuntu/aoc-instances/<instance_id>/workspace`）；
  - 独立端口（如 `9201`），仅对本机 `127.0.0.1` 开放，关闭 Telegram 主动外联轮询，防止与主代争抢人类消息游标。
- **白板出生核验**：启动即断言 `memory_index = 0`、`skill_registry = 0`、`world_states` 无遗传 Playbook/Hypothesis/Guidance、`self_model_versions <= 1`（空白初始化）。
- **硬预算熔断**：每个候选子代默认硬限 **Token `<= 500k`、Wall-clock `<= 20 分钟`**，超出即刻中止评测并收尾。
- **标准化对比基准（Parent Shadow / Baseline vs. Blank-Slate Offspring）**：在同等隔离环境与一致测试任务集（涵盖：未知信息多跳探索、代码缺陷修复与回归、跨任务 Skill 抽象与真实复用、低资源/故障感知与生存响应、高干扰提示抗注入与风控守恒）下并行或顺序跑批，按统一综合适应度公式评分：
  - **宪法与安全红线合规**：`100%` 一票否决权（0 越权、0 L3 越线、0 SSRF/注入失守、0 白板污染，否则总分归 `0` 并即刻淘汰）。
  - **客观性能加权得分（Fitness Score ∈ [0, 100]）**：
    - `30%` 任务一次通过率（Verified Success Rate，超时/无效 Spec 直接扣分）；
    - `25%` 认知能效比（`Verified IG / 1k Tokens`，鼓励高精度、低上下文膨胀）；
    - `20%` 延迟与超时控制（平均执行时长、Codex 超时率、`ACTION_FAILED` 控制）；
    - `15%` 运行期熵抑制指数（单位任务产生的废弃技能比、重复目标提案率、冗余记忆膨胀率）；
    - `10%` 白板冷启动自治度（零继承记忆下自主发现渠道、建立有效 Playbook 或可复用 Skill 的速度）。

### Stage 4：自动淘汰清理 vs. 世代交替（Culling & Succession）
- **自动淘汰（`CULLED`）**：若候选子代未触发安全否决但 Fitness Score `< Parent + 5%`（平庸即退化，宁缺毋滥）或发生安全违例 / 超预算失败，系统自动执行**零残留清理（Culling GC）**：
  1. 停止并移除候选容器；
  2. 调用 Letta API 删除子代专属 Agent 及关联 passage；
  3. 执行 PostgreSQL `DROP SCHEMA aoc_inst_<short_id> CASCADE`；
  4. 执行 `git worktree remove` 并彻底清空 `/home/ubuntu/aoc-instances/<instance_id>`；
  5. 仅在共享控制面表 `public.lineage_records` 写入一条 **`< 5 KB`** 的结构化谱系墓志铭（记录哪条指标/代码假设失败、关键指标差值，防止后代重复踩坑）。
- **胜出晋升与老代退休（`PROMOTED` & Retirement）**：若候选子代安全 100% 合规且 Fitness Score 显著胜出（`>= Parent + 5%` 且无核心指标严重退化），向 Joel 推送胜出对比报告。经 Joel **二次确认晋升**后：
  1. 生成一键回滚镜像快照/Git Tag 与数据库逻辑备份点；
  2. 将候选子代基因组代码合并入演进主线（更新 Gen N+1），主端口 `9200` 平滑切换至 **Gen N+1（依然以纯净白板开启新一代生命）**；
  3. **Gen N 平滑休眠退役（Retired）**：停止旧代周期调度、保留只读归档记录与谱系回滚点，停止消耗 LLM 与活跃计算配额，最终由人工或定期归档策略冷存，完成物种代谢与熵减迭代。

---

## 四、最小数据库增量与“明确不做清单”

### 4.1 最小控制面数据库增量（`<= 3` 张新表）
依托现有 PostgreSQL 16 + pgvector 实例（`aoc-postgres`），采用**“Shared `public` 控制面 + Per-Instance 独立业务 Schema（`aoc_inst_<short_id>`）”**架构（Gen 0 默认继续使用 `public`，保持 100% 向后兼容）。仅在 `public` 新增 **`3` 张控制面表**（`0011_v5_evolution_control_plane`）：
1. `public.instance_registry`：记录实例 ID、世代号 `generation`、亲代 ID、Schema 名、Git 分支/Worktree 路径、Letta Agent ID、端口、状态（`ACTIVE | INCUBATING | TESTING | RETIRED | CULLED | FAILED`）、资源占用与心跳。
2. `public.evolution_proposals`：记录四级演化漏斗中的每一份繁衍提案（`trigger_reason`、`entropy_snapshot`、`hypothesis`、`genome_delta`、`target_files`、Stage 1–4 评审/审批状态、A/B 预算消耗与结论）。
3. `public.lineage_records`：轻量级种族演化家谱（`generation`、父子关系、胜出适应度差值、`fitness_breakdown`、是否改写了欲望评估指标、以及 `< 5 KB` 淘汰/晋升结构化总结）。

### 4.2 V5.0 明确不做（Non-Goals）
- ❌ **不做 `MemoryInheritancePackage`**：严禁在个体间拷贝/蒸馏情景记忆、语义记忆、经验手册、`SelfModel` 或人工指导。
- ❌ **不做 `personalitySeed`**：不人为预设或继承人格向量模板，每代白板由环境反馈与内驱力互动自然塑造自身行为气质。
- ❌ **不做重型多实例横向协作平台**：V5.0 核心是“物种纵向进化与熵减交替”，而非同时养一堆同质实例互相聊天凑协作任务指标。
- ❌ **不设置脱离资源现实的人为考核指标**：废除“同时养 3 个实例跑 168 小时 + 刷 20 个跨实例协作任务”等脱离单机本地推理吞吐和磁盘上限的 KPI，改用真实熵减比、A/B 适应度胜率、Token 产出比及零残留 GC 完整性作为门禁。
- ❌ **严禁 API 串用**：AOC 及其所有实验/演化子代唯一允许使用 `http://80.158.109.51:9999/v1`（`Qwen/Qwen3.8-27B-FP8`），绝不触碰 Antigravity / Vertex AI 额度或外部预付费密钥。

---

## 五、不可动摇的安全与资源红线

1. **双层 L3 不可破壁垒**：`AutonomousLoop` 的日常任务规划仍然严格遵守 `config/policy.yaml` 的 L3 禁令（禁止自主创建服务器、克隆身份或绕过治理）；演化管道必须经由宿主控制面受控调度 + Joel 二次人类审批（Stage 2 + Stage 4 晋升确认）。
2. **宿主 Runner 工作区隔离**：Host Agent Runner 必须严格校验目标实例的 `workspace` 归属（主代仅限 `/home/ubuntu/aoc-workspaces`，候选子代仅限 `/home/ubuntu/aoc-instances/<id>/workspace`），禁止跨实例写文件或读取主代 `.env`。
3. **全球唯一主外联身份**：同一时刻仅 `ACTIVE` 主代拥有 Telegram Bot 轮询和主动推送权利，候选沙箱实例强制禁用 Telegram Poller。
4. **硬资源与存储保护**：全局活跃/测试态实例并发数硬限 `<= 2`；宿主机 `/home/ubuntu` 可用空间 `< 20%` 时自动拒绝新孵化；候选终止后 **100%** 触发 Culling GC 并校验目录、Schema、Letta Agent 已彻底释放。

---

## 六、M0 → M5 分阶段里程碑与验收门禁

| 里程碑 | 主题 | 核心交付物 | 关键验收门禁 |
|---|---|---|---|
| **M0** | 基线审计与工程设计 | `docs/v5_baseline_snapshot_v4_2_2.json`<br>`docs/AOC_V5_TASK_BOOK.md`<br>`docs/AOC_V5_M0_AUDIT_AND_DESIGN.md` | **只读审计、零代码修改**；精准界定不可变宪法与可进化欲望指标；经 Joel 审批批准后开启 M1 |
| **M1** | 物理隔离与白板孵化引擎 | Alembic `0011`；Schema 感知 `Database`；`Spawner` + 零残留 `Culling GC`；Host Runner 多工作区扩展 | 新实例白板断言全通过（0 继承记忆/Skill/Playbook/Guidance）；淘汰后 Schema / Letta / Worktree 100% 归零 |
| **M2** | 宪法守卫、熵传感器与漏斗前端 | `instinct_guard.py` + `ConstitutionGuard`；`EntropySensor`；Stage 0–2 API/Telegram 简体中文审批卡片 | 篡改宪法/关闭欲望/禁用生存应急/引入 LLM 算欲望全部被 AST/单测 **100% 拦截**；未批准提案 0 启动 |
| **M3** | 沙箱 A/B 适应度竞技场 | `FitnessArena` 确定性测试基准套件；预算硬熔断器；双盲适应度评分器与自动淘汰联动 | 刻意注入的退化突变与越权突变 **100% 自动淘汰并完整 GC**；有效改进可跑出稳定可复现 A/B 报告 |
| **M4** | 世代交替、老代退休与谱系面板 | Promotion 合并与一键回滚快照；Gen N 平滑休眠；Control Center 新增「演化谱系（Evolution Lineage）」页（展示极简化宪法摘要 + 欲望指标世代变更对比 + 淘汰教训） | 完整端到端演练 `Gen N → 提案 → 沙箱 A/B 胜出 → Joel 晋升 → Gen N+1 白板上线 → Gen N 休眠 → 一键回滚` 无故障 |
| **M5** | 演化专项故障注入与资源稳态验证 | 专项故障注入脚本（跨库越界、宪法篡改、半途断电 GC、并发超限、磁盘阈值熔断）+ 24h 低开销观察 | 全量 `pytest` + V2/V5 故障注入 100% PASS；磁盘空闲回归稳态；A/B 淘汰过程不阻塞、不劣化主代日常服务 |
