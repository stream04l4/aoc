# Artificial Organism Core (AOC) V4.0

**Artificial Organism Core（AOC）** 是一个以「不可变类人生命内核 + 可插拔外接器官 + 外接层可控 RSI 自进化」为核心设计的自主数字生命体核心系统（Python 3.12+ 模块化单体架构 + React/Vite Control Center）。

系统围绕类人内驱力（**Survival 生存** / **Reproduction 繁衍** / **Curiosity 好奇心**）与有界五维情绪构成不可变内核，外部插接四大器官——**外接大脑（LLM）**、**外接感知（Sensors / MCP）**、**外接记忆（Letta + pgvector）**、**外接行动（Host Runner / Codex / SkillRuntime）**——在严格的 L0–L3 安全治理与每日 Token/任务预算边界内，持续运行 16 阶段自主认知循环，并在内核之外实现技能、工具、只读感知源与策略手册的可控自我进化。

---

## 一、V4.0 核心宪法：内核归人类，内核外自主进化

| 边界层次 | 辖区与路径 | 权限与进化策略 |
|---|---|---|
| **人类独占不可变内核** | `app/core/`、`app/drives/`、`app/affect/`、`app/governance/`、`app/storage/`、三大内驱力公式、L0–L3 审批与硬预算上限 | **绝对不可变**：严禁程序在运行期以任何方式修改内核源码、内驱力公式、安全策略或触碰 **L3** |
| **① 外接技能资产（Skill Packages）** | `<workspace_root>/skills/`、`app/skills/runtime.py`、`app/skills/evolution.py` | **可控 RSI 进化**：规范命名 `<domain>.<capability>.<operation>`、语义去重、隔离 `python -I` 沙箱回归对比、`v1 → vNext` 自动晋升或退化自动回滚 |
| **② 外接工具插件（MCP Tool Bridge）** | `app/tools/mcp_manager.py` | **自动工具化**：经 SHA-256 与沙箱测试认证的 `VALIDATED` Skill 自动同步注册为标准 `skill:<canonical_name>` MCP 工具 |
| **③ 外接只读感知（External Perception）** | `app/perception/world/pipeline.py`、`app/perception/world/exploration.py` | **可控对外扩张**：支持 `open_public_perception_enabled` 放宽公开 HTTPS 源动态发现；严格强制防内网 SSRF / DNS 重绑定 / 元数据 IP / Prompt Injection 隔离与内部自查占比上限 |
| **④ 外置认知策略手册（External Playbooks）** | `<workspace_root>/playbooks/<domain>/v<N>.md`、`app/skills/playbooks.py` | **经验自迭代**：按领域沉淀可验证经验策略（SHA-256 校验，禁止内核改写与注入指令），跟踪实证成功率与 IG，退化自动回滚至 `parent_version` |

---

## 二、版本演进总览（V1.0 → V4.0）

- **V4.0（当前生产基线 `v4.0.0` · 自主学习、技能进化与外接层可控 RSI）**：
  - **M0 基线与资产盘点**：冻结 V3.2.0 24h 基线（[docs/v4_baseline_snapshot_v3_2.json](docs/v4_baseline_snapshot_v3_2.json)）与纯 Schema 备份，完成线上历史技能只读聚类审计（[scripts/v4_skill_audit.py](scripts/v4_skill_audit.py)）。
  - **M1 隔离 `SkillRuntime` + MCP 桥接 + Skill-First 执行**：Alembic 迁移 `0010_v4_skill_rsi_evolution`（扩展 `skill_registry` 并新增真实调用审计表 `skill_invocations`）；`VALIDATED` 技能通过 `python -I` + Schema 校验 + SHA-256 防篡改直接在 `SkillRuntime` 执行，跳过 Codex 重写脚本，并严格区分“放进 Prompt”与“真实跨任务复用”。
  - **M2 有界 RSI 技能演进引擎**：`SkillEvolutionEngine` 实现 `<domain>.<capability>.<operation>` 规范命名、语义+Schema 联合去重、Gate A 旧回归测试 + Gate B 新边界测试 + 延迟守卫，线上完成 68 条历史技能清理归并（保留 2 个 `VALIDATED` 规范种子，合并 39 条同质脚本与 27 条旧 `DRAFT`，保留全部历史证据）。
  - **M3 外置 Playbook 与好奇心外部探索**：`PlaybookManager` 实现内核外版本化策略迭代与退化自动回滚；`WorldEventPipeline` 支持强 SSRF 防护下的公开 HTTPS 源扩展；`ExplorationManager` + `select_best_goal_candidate` 打破内部自检死循环。
  - **M4 分层上下文压缩与 Control Center 融合**：`context_compactor.py` 裁剪冗余 Prompt（Top-K 相关性/复用率 Skill 签名、按领域截断 Playbook 并完整保留 `<untrusted_world_event>` 安全围栏）；Control Center「拟人总览」「自我认知」「环境感知」融合技能复用统计、版本演进树、Playbook 回滚历史与外部探索主题追踪。
- **V3.2（`v3.2.0` · 数字生命体拟人架构界面）**：Control Center 总览引入可交互 SVG 拟人数字生命体解剖图（[OrganismAnatomy.tsx](frontend/src/components/OrganismAnatomy.tsx)），实时映射三内驱力、五维情绪与四大外接器官遥测，侧边栏分组为生命内核/外接器官/观测审计。
- **V3.1（`v3.1.0` · 内核闭环增强）**：只读 Affect 注入 Brain 上下文、SelfModel 实证置信度进入情绪评估、Scheduler 反思周期与 Orchestrator 退避乘数受控调节、多候选目标效用排序、IG 预测误差校准及轻量世界假设（`digital_world_state(kind='hypothesis')`）。
- **V3.0（`v3.0.0` · 薄内核确定性护栏）**：`ActionPreflight` 发射前编译期预检、`GoalDedupEngine` 语义/词法去重与冷却退避、循环内 L0 只读 MCP 预取、任务工作区 `skill.py` 自动结晶、Alembic `0009_v3_kernel_enhancement`。
- **V2.1（`v2.1.0` · 只读 Control Center 与 PG pgvector 升级）**：新增 React + TypeScript + Vite Control Center 与 `/v2/control-center/*` 全套观测端点；数据库容器升级至 `pgvector/pgvector:0.8.7-pg16-bookworm`。
- **V2.0（`P1-1a` – `P1-10` · 生产级可靠性架构升级）**：Bearer RBAC、Survival 与经济配额解耦（`THROTTLED`）、统一 `LLMGateway`、16 态任务状态机与纯库重建 `TaskFinalizer`、`SKIP LOCKED` 事务 Outbox/Inbox、宿主机 `systemd` Agent Runner（cgroup v2 + `ProcessIdentity`）、本地量化 ONNX `BAAI/bge-small-zh-v1.5`（512 维）+ 五维混合检索 + 四因子信息增益、真实 RSS/Git/Web 传感器与双语 Prompt Injection 防护、强绑定 L2 审批 + Telegram Webhook、动态 Beta-Binomial `SelfModelManager`。

---

## 三、技术栈与仓库结构

- **后端内核与运行环境**：Python 3.12+、FastAPI、SQLAlchemy 2.x (AsyncPG / aiosqlite)、Alembic (`0001` – `0010`)、Pydantic v2、Prometheus、structlog
- **数据库与外接器官**：PostgreSQL 16 + `pgvector` + `pg_trgm`、Letta (`0.16.4`)、本地量化 ONNX Embedding (`bge-small-zh-v1.5`)、Host `systemd` Agent Runner (`codex-cli 0.171.0`)、MCP stdio JSON-RPC 2.0
- **前端 Control Center**：React 19 + TypeScript + Vite + Tailwind CSS + Recharts + Lucide + Vitest

```text
aoc/
├── app/
│   ├── api/            # FastAPI 路由、Bearer RBAC (auth.py) 与 Control Center 只读接口 (control_center.py)
│   ├── core/           # 不可变内核生命周期、Scheduler、16 阶段 AutonomousLoop、Runtime 装配与配置
│   ├── drives/         # Survival(配额解耦) / Reproduction(实证能力) / Curiosity(四因子 IG) 与混合仲裁
│   ├── affect/         # 有界五维情绪调制器 (仅调参数，禁改策略与安全边界)
│   ├── governance/     # L0-L3 分级审批、双语语义风控、预算账本、强绑定 ApprovalService
│   ├── cognition/      # LLMGateway、CognitionBrain、ActionPreflight、GoalDedupEngine、Tiered ContextCompactor
│   ├── orchestration/  # 16 态 TaskStateMachine、纯 DB 幂等 TaskFinalizer、ResultVerifier
│   ├── agents/         # AgentInterface 契约、CodexAdapter (0.171.0)、AgentRouter 五因子路由
│   ├── runner/         # 宿主机 systemd Runner 服务与 ProcessIdentity 崩溃对账
│   ├── memory/         # ExperienceManager、HybridRetrievalEngine、InformationGainEvaluator、LettaAdapter、ONNX Embedding
│   ├── perception/     # System/Agent/Environment 传感器与 WorldEventPipeline / ExplorationManager / SSRF 防护
│   ├── self_model/     # Beta-Binomial SelfModelManager 与 SkillRegistryManager
│   ├── skills/         # 外接进化区：SkillRuntime、SkillEvolutionEngine、PlaybookManager
│   ├── tools/          # MCPClientManager 与内置只读/沙箱 MCP 工具 + VALIDATED Skill MCP 桥接
│   └── storage/        # ORM 模型、Unit-of-Work StateRepository 与事务 Outbox/Inbox
├── config/             # 只读 YAML 策略配置 (settings / drives / agents / policy / sensors / affect / self_model / memory / runner)
├── frontend/           # Control Center Web UI (React + Vite + Vitest)
├── migrations/         # Alembic 数据库迁移 (0001_initial_schema .. 0010_v4_skill_rsi_evolution)
├── scripts/            # 运维与评估脚本 (故障注入、100 周期、pgvector 迁移、V4 技能审计与清理)
└── tests/              # 单元测试 (179)、故障注入、kill -9 集成与端到端验收
```

---

## 四、快速启动与验证

### 1. Docker Compose 生产部署

```bash
# 构建并启动 postgres (pgvector) / letta / aoc-core（入口自动执行 alembic upgrade head）
docker compose build aoc
docker compose up -d

# 检查健康状态与前端资源
curl -sf http://127.0.0.1:9200/health
```

> 关键环境变量在 `docker-compose.yml` 中默认开启 V3/V4 特性：`AOC_KERNEL_V3__SKILL_RUNTIME_ENABLED`、`AOC_KERNEL_V3__SKILL_FIRST_PLANNING_ENABLED`、`AOC_KERNEL_V3__SKILL_MCP_BRIDGE_ENABLED`、`AOC_KERNEL_V3__SKILL_EVOLUTION_ENABLED`、`AOC_KERNEL_V3__PLAYBOOKS_ENABLED`、`AOC_KERNEL_V3__OPEN_PUBLIC_PERCEPTION_ENABLED`。

### 2. 全量验证命令（V4.0 发布实测基线）

```bash
# 1. 全量 Python 单元测试（179 passed，SQLite 离线沙箱）
.venv/bin/pytest tests/unit/ -q

# 2. 10 类真实故障注入回归套件（10/10 passed）
.venv/bin/python scripts/run_v2_fault_injection.py

# 3. 前端 Vitest 单元测试（3/3 passed）与生产构建
cd frontend && npm test -- --run && npm run build && cd ..

# 4. V4.0 技能资产只读审计与去重清理工具
.venv/bin/python scripts/v4_skill_audit.py --help
.venv/bin/python scripts/v4_cleanup_skills.py            # 默认 dry-run 预览
.venv/bin/python scripts/v4_cleanup_skills.py --apply    # 正式规范化合并重复技能
```

---

## 五、文档索引

### 核心设计与参考文档
- [系统架构设计（V4.0） (`docs/architecture.md`)](docs/architecture.md)
- [核心驱动器、检索、自模型、路由与 RSI 进化算法（V4.0） (`docs/algorithms.md`)](docs/algorithms.md)
- [外接行动层：AgentProvider、MCP ToolProvider 与 SkillRuntime 指南（V4.0） (`docs/agent_adapters.md`)](docs/agent_adapters.md)
- [HTTP API 完整参考（V1 / V2 / Control Center） (`docs/api.md`)](docs/api.md)
- [历史里程碑结项核对与后续演进路线图 (`docs/known_issues_and_roadmap.md`)](docs/known_issues_and_roadmap.md)

### 历代任务书与 V4.0 基线快照
- [V4.0 开发任务书 (`docs/AOC_V4_TASK_BOOK.md`)](docs/AOC_V4_TASK_BOOK.md) · [V3.2 基线快照 (`docs/v4_baseline_snapshot_v3_2.json`)](docs/v4_baseline_snapshot_v3_2.json) · [技能审计报告 (`docs/v4_skill_audit_v3_2.json`)](docs/v4_skill_audit_v3_2.json)
- [V3.0 开发任务书 (`docs/AOC_V3_TASK_BOOK.md`)](docs/AOC_V3_TASK_BOOK.md) · [V2.1 开发任务书 (`docs/AOC_V2_TASK_BOOK_v2.1.md`)](docs/AOC_V2_TASK_BOOK_v2.1.md) · [V1.0 开发任务书 (`docs/AOC_V1_TASK_BOOK.md`)](docs/AOC_V1_TASK_BOOK.md)

### 历史阶段验证报告（V2.0 / V2.1）
- [V2.1 架构变更](docs/v2_1_architecture_changes.md) · [V2.1 部署手册](docs/v2_1_deployment_runbook.md) · [V2.1 修复报告](docs/v2_1_fix_report.md) · [V2.1 测试报告](docs/v2_1_test_report.md) · [pgvector 迁移方案](docs/v2_1_pgvector_migration_plan.md)
- [V2.0 迁移报告](docs/v2_migration_report.md) · [kill -9 报告](docs/v2_kill9_report.md) · [资源与监控报告](docs/v2_resource_report.md) · [安全评估报告](docs/v2_security_report.md) · [72h 长稳报告](docs/v2_soak_report_72h.md)
