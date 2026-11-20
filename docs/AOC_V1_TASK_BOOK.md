# Artificial Organism Core（AOC）
## V1.0 最小自主生命核心开发任务书

**项目代号：** AOC  
**版本：** 1.0  
**项目阶段：** Phase 0 — Minimum Viable Organism  
**开发模式：** Python Modular Monolith  
**目标：** 实现一个具有持续内部动机、能够自主产生目标、调用外部 AI Agent 执行任务、感知执行结果并更新内部状态的数字生命核心。

---

# 一、项目目标

开发一个独立运行的 Artificial Organism Core（以下简称 AOC）。

AOC 不是普通的任务型 Agent，而是一个具有内部状态和基础驱动力的自主系统。

系统必须能够在没有人工任务输入的情况下：

1. 主动感知自身状态和外部环境。
2. 根据感知结果计算内部欲望满足程度。
3. 通过欲望仲裁选择当前优先目标。
4. 调用大模型进行认知、分析和行动规划。
5. 将行动任务交给外部 AI Agent 执行。
6. 观察执行结果和环境变化。
7. 重新评估欲望满足程度。
8. 将重要经历和结果写入长期记忆。
9. 进入下一轮自主运行。

系统必须支持持续运行、休眠、唤醒、故障恢复和状态持久化。

## 1.1 第一版明确不实现的功能

以下内容只预留接口，不在 V1.0 实现：

- 复杂情绪系统。
- 自我意识模拟。
- 自我修改核心代码。
- 自主创建和复制新实例。
- 模型训练和微调。
- 多生命体协作。
- 物理机器人控制。

V1.0 只关注基础欲望和自主行为闭环。

---

# 二、总体架构

采用 Modular Monolith 架构。

主要模块：

```text
AOC
│
├── Core Runtime
│   ├── Lifecycle Manager
│   ├── Scheduler
│   ├── Event Loop
│   └── State Manager
│
├── Perception
│   ├── Sensor Manager
│   ├── Self Monitor
│   └── World State
│
├── Drive Engine
│   ├── Survival Drive
│   ├── Reproduction Drive
│   ├── Curiosity Drive
│   ├── Satisfaction Calculator
│   └── Drive Arbitration
│
├── Cognition
│   ├── Brain Interface
│   ├── LLM Adapter
│   ├── Goal Generator
│   └── Action Planner
│
├── Agent Orchestration
│   ├── Agent Registry
│   ├── Agent Manager
│   ├── Task Dispatcher
│   ├── Execution Monitor
│   └── Result Collector
│
├── Memory
│   ├── Memory Interface
│   ├── Letta Adapter
│   └── Experience Manager
│
├── Governance
│   ├── Permission Manager
│   ├── Resource Budget
│   └── Safety Policy
│
├── Storage
│   ├── Database
│   └── State Repository
│
└── Observability
    ├── Event Logger
    ├── Decision Logger
    └── Metrics
```

所有模块必须通过明确的 Python Interface 通信。

禁止在模块之间直接访问对方的内部实现。

---

# 三、基础技术栈

| 组件 | 技术 |
|---|---|
| Python | 3.12+ |
| Async Runtime | asyncio |
| Data Validation | Pydantic v2 |
| API | FastAPI |
| Database | PostgreSQL |
| ORM | SQLAlchemy 2.x |
| Migration | Alembic |
| Memory | Letta |
| LLM | OpenAI-compatible API |
| HTTP Client | httpx |
| Logging | structlog |
| Testing | pytest + pytest-asyncio |
| Metrics | Prometheus |
| Container | Docker Compose |

第一版不需要 Redis、Kafka、Kubernetes 或微服务。

系统应该能够在一台普通 Linux 服务器上通过 Docker Compose 运行。

数据库必须支持持久化，不能把核心状态仅保存在 Python 内存中。

---

# 四、核心欲望系统

这是整个项目最重要的模块。

V1.0 实现三种基础欲望。

## 4.1 Survival Drive

生存欲望。

主要负责维持系统持续运行所需要的条件。

评估维度：

- CPU、内存和磁盘资源。
- LLM 服务可用性。
- Agent 执行环境可用性。
- 运行成本和资源余额。
- 服务异常和任务失败情况。

生存欲望必须使用实际采集的系统指标进行计算。

禁止简单地把 CPU 利用率高低作为生存满足度。

## 4.2 Reproduction Drive

繁衍欲望。

V1.0 暂时不允许真正创建新的生命实例。

先使用虚拟目标实现该欲望。

评估维度：

- 能力扩展程度。
- 可复用技能数量。
- 已完成的能力建设目标。
- 资源储备情况。
- 实例扩展准备程度。

例如：

系统发现自己缺乏某种能力，可以将学习或能力建设视为满足繁衍欲望的候选行为。

注意：繁衍欲望不能直接取得创建服务器、复制身份或增加系统权限的能力。

## 4.3 Curiosity Drive

好奇心。

主要负责主动探索和知识获取。

评估维度：

- 当前知识缺口。
- 未知信息数量。
- 探索任务完成情况。
- 新知识获得情况。
- 信息增益。
- 最近学习进度。

V1.0 可以先采用 LLM 进行知识缺口评估，后续再增加更精确的信息增益算法。

## 4.4 统一 Drive 数据模型

使用 Pydantic 定义：

```python
class DriveState(BaseModel):
    drive_type: str

    satisfaction: float
    deficit: float
    urgency: float
    confidence: float

    persistence: float
    trend: float

    estimated_cost: float
    estimated_risk: float

    updated_at: datetime
```

所有归一化评分必须限制在 0～1。

其中：

```text
deficit = 1 - satisfaction
```

Persistence 表示持续未满足的程度。

Trend 表示满足度变化趋势。

三个欲望分别实现独立的评估器。

禁止让一个统一的 LLM Prompt 直接决定三个欲望的全部状态。

## 4.5 欲望仲裁算法

第一版采用混合评分算法。

基础评分：

```text
priority =
    deficit
    * urgency
    * confidence
    * drive_weight
```

随后增加：

- 长期未满足的累积权重。
- 最近满足后的抑制机制。
- 当前资源成本。
- 行动风险。
- 生存需求的紧急保护机制。

注意：

这只是第一版调度算法，不要求模拟真实生物神经系统。

需要将所有权重和参数放入配置文件，不能硬编码。

欲望仲裁必须输出：

```python
class DriveDecision(BaseModel):
    selected_drive: str
    priority_score: float

    alternative_drives: list[str]

    reason: str
    constraints: dict

    timestamp: datetime
```

必须记录为什么选择当前欲望，以及为什么没有选择其他欲望。

---

# 五、感知系统 Perception

建立统一的感知接口。

```python
class SensorInterface(ABC):

    @abstractmethod
    async def observe(self) -> Observation:
        pass
```

统一感知数据结构：

```python
class Observation(BaseModel):

    timestamp: datetime

    system_state: dict
    environment_state: dict

    available_resources: dict

    active_tasks: list[dict]

    external_events: list[dict]

    confidence: float
```

第一版实现三个 Sensor：

1. SystemSensor：采集 CPU、内存、磁盘和服务运行状态。
2. AgentSensor：采集所有外部 Agent 的运行状态和任务状态。
3. EnvironmentSensor：提供一个可以扩展的数字环境感知接口。

要求：

- Sensor 必须可以独立注册和注销。
- 一个 Sensor 失败不能导致整个感知流程崩溃。
- 数据必须包含时间戳。
- 无法获取的数据必须明确标记为 unavailable。
- 不允许将缺失数据默认解释为正常。

---

# 六、大脑系统 Brain

大脑通过独立的 LLM Adapter 接入。

第一版支持 OpenAI-compatible API。

模型名称、API 地址、密钥和温度参数全部从环境变量或配置文件加载。

建立统一接口：

```python
class BrainInterface(ABC):

    @abstractmethod
    async def assess(
        self,
        observation: Observation,
        memories: list,
        drives: list
    ) -> AssessmentResult:
        pass

    @abstractmethod
    async def plan(
        self,
        context: CognitiveContext
    ) -> ActionPlan:
        pass

    @abstractmethod
    async def evaluate(
        self,
        context: EvaluationContext
    ) -> EvaluationResult:
        pass
```

大脑负责：

1. 分析环境和自身状态。
2. 识别知识缺口。
3. 分析当前欲望。
4. 提出可实现的目标。
5. 制定执行计划。
6. 评估行动结果。

大脑不拥有以下权限：

- 直接修改数据库中的欲望状态。
- 直接修改系统权限。
- 绕过 Agent Orchestration 调用外部 Agent。
- 修改系统核心配置。
- 自行批准高风险操作。

所有大脑输出必须通过 Pydantic Schema 验证。

如果返回格式错误，允许有限次数重试，之后进入错误处理流程。

---

# 七、外部 Agent 编排系统

这是本项目与普通 Agent 项目最大的区别之一。

AOC 必须支持调用现有主流 AI Agent。

不重新开发 Agent 内部的规划和工具调用机制。

## 7.1 统一 Agent Interface

```python
class AgentInterface(ABC):

    @abstractmethod
    async def execute(
        self,
        task: AgentTask
    ) -> AgentExecution:
        pass

    @abstractmethod
    async def get_status(
        self,
        execution_id: str
    ) -> AgentStatus:
        pass

    @abstractmethod
    async def cancel(
        self,
        execution_id: str
    ) -> bool:
        pass
```

## 7.2 第一版 Agent Adapter

优先实现：

### Codex Adapter

使用 Codex CLI 的非交互执行模式。

优先研究：

```bash
codex exec
```

要求：

- 支持非交互执行。
- 支持任务超时。
- 支持标准输出和标准错误采集。
- 支持结构化结果解析。
- 支持执行取消。
- 支持工作目录隔离。
- 支持权限和沙箱配置。

默认禁止使用无沙箱、无审批的全权限模式。

### Hermes Adapter

使用 Hermes Agent 官方提供的 CLI 或受支持的 Runtime 接口。

需要先调查当前版本实际提供的非交互执行能力。

不能假定 Hermes 与 Codex 的命令参数和会话机制完全相同。

必须验证：

- 如何启动任务。
- 如何获取执行结果。
- 如何获取运行状态。
- 如何取消任务。
- 如何隔离任务工作空间。
- 如何处理身份验证。

如果当前版本没有可靠的机器接口，应将其标记为实验性 Adapter，不要伪造兼容接口。

### 其他 Agent

支持后续接入：

- 其他主流 CLI Agent。
- 支持 API 的自主 Agent。
- MCP 工具型执行器。

关于你提到的 DSH，暂时不要根据名称假定具体项目或接口。将它作为待确认的 Agent 类型，先设计成通用 Adapter 扩展点。

## 7.3 Agent 注册机制

建立 Agent Registry。

每个 Agent 必须声明：

```python
class AgentCapability(BaseModel):

    agent_id: str
    name: str

    agent_type: str

    capabilities: list[str]

    supported_modes: list[str]

    max_concurrency: int

    enabled: bool
```

例如：

Codex：

- coding
- debugging
- code_review
- terminal
- repository_analysis

Hermes：

- research
- browser
- automation
- general_task
- skill_execution

能力描述必须根据实际安装和配置情况注册，不能仅根据 Agent 名称推断。

## 7.4 任务数据结构

```python
class AgentTask(BaseModel):

    task_id: str

    objective: str
    description: str

    assigned_agent: str

    expected_output: str

    workspace: str

    timeout_seconds: int

    resource_budget: dict

    permission_level: str

    success_criteria: list[str]

    created_at: datetime
```

每个任务必须具有：

- 明确目标。
- 明确完成条件。
- 时间限制。
- 资源限制。
- 权限范围。
- 独立执行 ID。

禁止把无限期的自主探索任务直接交给外部 Agent。

AOC 是任务发起者和最终结果评估者。

## 7.5 执行结果

```python
class AgentExecution(BaseModel):

    execution_id: str
    task_id: str

    agent_id: str

    status: str

    started_at: datetime
    completed_at: datetime | None

    result: dict | None

    stdout: str | None
    stderr: str | None

    error: str | None

    resource_usage: dict
```

状态至少包括：

```text
PENDING
RUNNING
SUCCEEDED
FAILED
TIMEOUT
CANCELLED
REQUIRES_APPROVAL
```

Agent 执行结果不能直接被认定为事实。

必须进入结果验证流程。

---

# 八、记忆系统 Memory

采用双层记忆架构。

## 8.1 PostgreSQL：实时状态

保存：

- 当前欲望状态。
- 当前目标。
- 当前情绪预留状态。
- 当前运行周期。
- 当前 Agent 任务。
- 资源预算。
- 运行状态。
- 事件记录。
- 行为审计记录。

## 8.2 Letta：长期记忆

通过独立 Letta Adapter 接入。

第一版使用以下记忆分类：

```text
Identity Memory
    身份和长期特征

Episodic Memory
    过去发生的事件

Semantic Memory
    获取的知识

Procedural Memory
    已掌握的技能和方法

Self Model
    对自身能力、限制和经验的认识
```

Letta 不负责实时欲望评分。

实时状态以 PostgreSQL 为准。

Letta 负责长期认知记忆。

要求：

- 记忆写入失败不能导致整个生命循环崩溃。
- 记忆必须带来源和时间信息。
- 支持检索和更新。
- 支持重要事件优先保存。
- 支持重启后恢复长期身份。

---

# 九、核心自主循环

实现一个可以持续运行的 Autonomous Loop。

严格按照以下顺序执行：

```text
WAKE UP
   |
   v
LOAD INTERNAL STATE
   |
   v
PERCEPTION
   |
   v
SELF ASSESSMENT
   |
   v
DRIVE SATISFACTION CALCULATION
   |
   v
DRIVE ARBITRATION
   |
   v
GOAL GENERATION
   |
   v
COGNITIVE PLANNING
   |
   v
POLICY VALIDATION
   |
   v
AGENT DISPATCH
   |
   v
WAIT / MONITOR
   |
   v
OUTCOME EVALUATION
   |
   v
MEMORY CONSOLIDATION
   |
   v
STATE UPDATE
   |
   v
SLEEP / NEXT CYCLE
```

每一个周期必须拥有唯一的 cycle_id。

每一个周期都必须记录：

- 感知结果。
- 三种欲望的满足度。
- 欲望仲裁结果。
- 大脑输出。
- 执行计划。
- Agent 执行情况。
- 最终评估。
- 状态变化。

如果本轮没有值得执行的任务，允许进入 IDLE。

不得为了保持循环活跃而强行创建任务。

## 9.1 欲望满足度更新

这是关键规则。

大脑提出的预期效果不能直接修改满足度。

例如：

执行前：

```text
Curiosity Satisfaction = 0.20
```

大脑预计：

```text
Expected Improvement = +0.15
```

实际执行后：

```text
Actual Information Gain = 0.05
```

最终满足度必须根据新的感知结果重新计算，而不是直接使用大脑预测值。

必须区分：

- Expected Outcome
- Actual Outcome
- Measured Satisfaction Change

---

# 十、状态管理和生命周期

实现以下状态：

```text
INITIALIZING
ACTIVE
IDLE
SLEEPING
CRITICAL
PAUSED
STOPPED
ERROR
```

状态切换必须由 Lifecycle Manager 统一管理。

系统需要支持：

- 正常启动。
- 正常停止。
- 暂停。
- 恢复。
- 定时唤醒。
- 事件触发唤醒。
- 进程异常重启。
- 未完成任务恢复或安全终止。

任务执行与自主循环必须解耦。

不能因为一个 Agent 任务需要执行 30 分钟，就阻塞整个主循环。

需要实现异步任务管理和状态持久化。

---

# 十一、行为治理和安全边界

第一版必须实现 Governance 模块。

默认采用最小权限原则。

将外部操作划分为：

L0：只读操作。

L1：受限写操作。

L2：高风险操作，需要人工审批。

L3：核心系统和基础设施修改，禁止自主执行。

以下行为必须默认禁止：

- 自主创建云服务器和付费资源。
- 修改系统核心权限。
- 修改自身安全策略。
- 获取或导出其他系统的密钥。
- 删除核心记忆数据库。
- 绕过人工审批。
- 禁用关机、暂停和资源限制机制。

繁衍欲望只能提出扩展计划，不能直接执行资源扩张。

所有 Agent 必须在隔离的工作空间内执行。

Agent 不得直接访问 AOC 的数据库和核心配置。

---

# 十二、数据库设计

至少创建以下数据表：

```text
organism_identity
organism_state
drive_states
drive_history

observation_records
world_states

active_goals
goal_history

action_plans
agent_registry
agent_tasks
agent_executions

memory_index

cycle_records
decision_logs
event_logs

resource_usage
system_config
```

要求：

- 所有主要实体具有 UUID。
- 所有状态变化具有时间戳。
- 任务和执行记录保留完整关联关系。
- 状态更新使用事务。
- 支持进程崩溃后的状态恢复。
- 不允许将密码和 API Key 明文写入业务表。

提供 Alembic Migration。

---

# 十三、项目目录

按照以下目录实施：

```text
aoc/
│
├── app/
│   ├── core/
│   ├── perception/
│   ├── drives/
│   ├── cognition/
│   ├── orchestration/
│   ├── agents/
│   │   ├── base.py
│   │   ├── codex.py
│   │   ├── hermes.py
│   │   └── registry.py
│   ├── memory/
│   ├── governance/
│   ├── storage/
│   ├── observability/
│   └── api/
│
├── config/
│   ├── settings.yaml
│   └── drives.yaml
│
├── migrations/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── simulation/
│   └── acceptance/
│
├── scripts/
│
├── docker/
│
├── docs/
│
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── main.py
```

目录允许根据实际工程需要调整，但必须保持模块职责分离。

---

# 十四、开发实施阶段

不得一次性开发全部功能。

按以下顺序执行。

## Task P0-1：工程骨架

交付：

- Python 项目。
- 配置管理。
- 数据库连接。
- SQLAlchemy 模型。
- Alembic。
- Docker Compose。
- 日志系统。
- 基础 API。
- 单元测试框架。

验收：

Docker Compose 可以启动 AOC 和 PostgreSQL。

## Task P0-2：欲望引擎

交付：

- 三种欲望评估器。
- DriveState 数据模型。
- 满足度计算。
- 欲望优先级计算。
- 欲望仲裁器。
- 状态持久化。
- 参数配置。

验收：

通过模拟输入，能够稳定计算和选择不同欲望。

需要编写至少 30 组边界测试和场景测试。

## Task P0-3：感知系统

交付：

- SystemSensor。
- AgentSensor。
- EnvironmentSensor。
- 统一 Observation。
- 感知数据持久化。

验收：

能够采集真实服务器状态，并允许模拟环境输入。

## Task P0-4：大脑接口

交付：

- BrainInterface。
- OpenAI-compatible Adapter。
- Assessment。
- Goal Generation。
- Action Planning。
- Evaluation。

验收：

能够使用外部模型完成结构化认知任务。

模型可以替换，业务代码不需要修改。

## Task P0-5：Agent 编排

交付：

- AgentInterface。
- AgentRegistry。
- Codex Adapter。
- Hermes Adapter（如果机器接口验证通过）。
- Task Dispatcher。
- Execution Monitor。
- Result Collector。

验收：

至少能够启动一个真实外部 Agent，完成一个受限任务，回收结果并记录执行状态。

## Task P0-6：记忆

交付：

- Letta Adapter。
- 记忆分类。
- 重要经历写入。
- 记忆检索。
- 重启恢复。

验收：

系统重启后仍能检索之前保存的重要经历。

## Task P0-7：自主运行闭环

交付：

- Autonomous Loop。
- Lifecycle Manager。
- Scheduler。
- State Recovery。
- Error Recovery。
- IDLE 和 SLEEPING。

验收：

完成从感知到行动再到记忆更新的完整闭环。

## Task P0-8：集成测试

完成连续运行测试、异常测试、恢复测试和资源预算测试。

---

# 十五、最终验收标准

V1.0 不能仅以代码完成作为验收标准。

必须通过真实运行验证。

## 测试场景 A：生存需求

人为模拟磁盘空间不足或 LLM 服务不可用。

预期：

- Sensor 感知异常。
- 生存满足度下降。
- Survival Drive 优先级提高。
- 大脑提出处理方案。
- 调用具有对应权限的 Agent。
- 验证处理结果。
- 更新生存满足度。

## 测试场景 B：好奇心

向数字环境提供一个未知知识主题。

预期：

- 系统发现知识缺口。
- 好奇心满足度下降。
- 好奇心成为优先欲望。
- 大脑产生探索目标。
- Agent 执行研究任务。
- 系统获得新知识。
- 将知识保存到长期记忆。
- 重新评估好奇心满足度。

## 测试场景 C：繁衍

模拟能力建设不足。

预期：

- 繁衍欲望优先级提高。
- 系统提出能力建设目标。
- Agent 完成技能研究或能力建设。
- 系统记录新能力。
- 繁衍满足度发生变化。

不得实际创建新实例。

## 测试场景 D：没有任务

模拟三个欲望都处于较高满足状态。

预期：

- 系统不强制产生任务。
- 进入 IDLE 或 SLEEPING。
- 定时唤醒。
- 重新感知。

## 测试场景 E：系统重启

执行过程中强制终止 AOC 进程。

重新启动后：

- 恢复身份。
- 恢复欲望状态。
- 恢复未完成任务状态。
- 不重复执行已经完成的任务。
- 继续正常运行。

## 测试场景 F：外部 Agent 失效

模拟 Codex 或 Hermes 无法启动、执行超时或异常退出。

预期：

- 记录失败原因。
- 不丢失原始任务。
- 按照策略重试或切换 Agent。
- 不无限重试。
- 不将失败任务判定为目标成功。

## 连续运行要求

至少完成 100 个自主周期测试。

要求：

- 无致命异常。
- 无状态数据损坏。
- 无无限任务循环。
- 无未授权操作。
- 预算不超限。
- 重启恢复正常。
- 能够自主产生并完成不同类型的目标。

---

# 十六、交付物

开发 Agent 必须提交：

1. 完整项目源代码。
2. Docker Compose 部署文件。
3. 数据库 Migration。
4. 配置文件模板。
5. Agent Adapter 使用说明。
6. API 文档。
7. 系统架构文档。
8. 核心算法说明。
9. 单元测试和集成测试。
10. 连续运行测试报告。
11. 已知问题清单。
12. 下一阶段技术建议。

必须提供实际运行日志和测试结果，不能只提交测试设计。

---

# 十七、开发纪律

1. 开始编码前先阅读完整任务书。
2. 先提交架构设计和技术风险清单。
3. 按 P0-1 至 P0-8 顺序开发。
4. 每完成一个阶段，先执行测试，再提交代码。
5. 不允许为了赶进度删除模块边界。
6. 不允许用 Mock 结果冒充真实 Agent 执行。
7. 不允许用 LLM 自己给出的成功判断代替实际结果验证。
8. 所有外部 Agent 都必须通过统一接口接入。
9. 所有核心状态必须持久化。
10. 不得擅自增加繁衍、自我修改和高权限自主执行能力。
11. 不确定的第三方 Agent 接口必须先验证，不得假设其能力。
12. 遇到架构性问题，先提交分析和建议，不要自行改变项目核心设计。

**最终目标：交付一个能够在 Linux 服务器上独立运行、具备三种基础欲望、能够自主产生目标、调用真实外部 AI Agent 执行任务、感知执行结果并持续更新自身状态的最小数字生命核心。**

V1.0 的成功标准不是它能完成多少任务，而是它能否在没有人类持续指挥的情况下，维持一个真实、稳定、可审计的自主行为闭环。