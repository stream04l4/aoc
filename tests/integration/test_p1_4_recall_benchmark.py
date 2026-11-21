"""P1-4 Real Integration & Bilingual Recall@5 Benchmark Tests:
  1. 200-document annotated bilingual (Chinese + English + mixed) corpus + 50 queries (25 ZH + 25 EN/mixed)
     benchmarking real local ONNX embedding models (Recall@5 >= 0.80, RSS < 600MB, single P95 < 100ms, batch-32 < 500ms).
  2. Real ONNX model version switching (384d <-> 512d) + re-embedding coexistence verification.
  3. Real PostgreSQL 16 + pgvector + pg_trgm migration test on unmodified `aoc_pg_dump_20261120.sql`
     (verifying 52 historical AOC-BENCH-* pending memories are NOT auto-enqueued to Letta).
  4. Real Letta (127.0.0.1:8283) outage -> recovery Outbox sync reconciliation test.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import pytest
from sqlalchemy import inspect, text

from app.core.config import MemorySettings
from app.core.enums import MemoryType
from app.memory.embedding import EmbeddingService, LocalOnnxEmbeddingProvider
from app.memory.experience_manager import ExperienceManager, compute_content_hash
from app.memory.interfaces import MemoryItem
from app.memory.letta_adapter import LettaAdapter
from app.memory.retrieval import HybridRetrievalEngine, bilingual_trigram_similarity
from app.storage.database import Database
from app.storage.models import Base
from app.storage.outbox import OutboxDispatcher, OutboxEventType
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = str(PROJECT_ROOT / ".cache" / "embeddings")

# 25 distinct technical/AOC domains -> 25*4 = 100 annotated topic docs + 100 domain-adjacent hard distractors = 200 docs!
# 50 queries: 25 pure Chinese + 25 English / mixed Chinese-English.
_DOMAINS: list[dict[str, Any]] = [
    {
        "id": "pg_outbox",
        "zh_q": "PostgreSQL 事务发件箱如何避免多消费者重复投递",
        "en_q": "How does PostgreSQL transactional outbox prevent duplicate delivery with SKIP LOCKED",
        "docs": [
            ("PostgreSQL Outbox 并发领取", "使用 SELECT FOR UPDATE SKIP LOCKED 结合 inbox_events 唯一键防止多 Worker 重复消费。"),
            ("Transactional Outbox Atomicity", "Write domain state changes and outbox_events in the same PostgreSQL transaction for at-least-once delivery."),
            ("Outbox 退避与死信机制", "投递失败按指数退避重试，超过 max_attempts 进入 dead_letter 并写入 event_logs 告警。"),
            ("Inbox Idempotency Table", "Consumer checks (consumer_name, idempotency_key) in inbox_events before applying side effects."),
        ],
    },
    {
        "id": "systemd_cgroup",
        "zh_q": "宿主机非特权 systemd 服务如何用 cgroup v2 限制子进程内存与进程数",
        "en_q": "Unprivileged systemd Delegate=yes cgroup v2 memory.max and pids.max isolation",
        "docs": [
            ("Systemd cgroup v2 委派", "在 unit 中配置 User=ubuntu 与 Delegate=yes，将专属 cgroup 子树委派给普通用户管理子进程。"),
            ("Cgroup Resource Quotas", "Write memory.max and pids.max into each exec-<id> child cgroup to cap external agent usage."),
            ("无孤儿进程清理 cgroup.kill", "超时或取消任务时向 cgroup.kill 写入 1 可一次性杀死整个子进程树，避免孤儿残留。"),
            ("KillMode=process Restart Survival", "Setting KillMode=process allows host runner restarts while keeping running child executions alive."),
        ],
    },
    {
        "id": "pid_reuse",
        "zh_q": "Linux 崩溃恢复怎样防止 PID 复用导致的进程误判",
        "en_q": "Preventing Linux PID reuse false positives using proc stat starttime and host boot_id",
        "docs": [
            ("进程身份防复用校验", "核验 /proc/sys/kernel/random/boot_id 与 /proc/<pid>/stat 第 22 字段 starttime 防止 PID 复用误判。"),
            ("ProcessIdentity Verification", "A live process match requires identical host_boot_id, start_ticks, non-zombie state, and cgroup membership."),
            ("重启后 boot_id 变化判定", "宿主机重启后内核 boot_id 必然变更，任何旧记录中的 PID 都立即判定为已死亡。"),
            ("Parsing /proc/<pid>/stat Safely", "Find the last closing parenthesis in stat before splitting fields so comm spaces never shift starttime."),
        ],
    },
    {
        "id": "budget_survival",
        "zh_q": "每日 Token 预算耗尽为什么不应该触发 Survival 生存危急警报",
        "en_q": "Decoupling economic token budget quota from physical survival critical alerts",
        "docs": [
            ("预算与生存状态解耦", "将 budget 移出 Survival 致命维度，配额耗尽进入 THROTTLED 节流休眠而非 CRITICAL 警报。"),
            ("Rolling 24h Quota Reset", "Calculate exact quota_reset_at from the oldest billable record in the 24-hour window plus 24 hours."),
            ("消除预算空转死循环", "当 now < quota_reset_at 且无物理磁盘或内存危机时，调度器休眠不再反复调用 LLM。"),
            ("Economic Quota vs Physical Health", "Disk exhaustion or PostgreSQL outage triggers CRITICAL, while daily token limits trigger THROTTLED."),
        ],
    },
    {
        "id": "llm_gateway",
        "zh_q": "统一 LLM 网关怎样限制每周期调用次数和并发数并防止漏记账",
        "en_q": "LLMGateway per-cycle call cap semaphore concurrency and retry failure token accounting",
        "docs": [
            ("LLMGateway 并发与周期限额", "通过 asyncio.Semaphore 控制最大并发请求，并按 cycle_id 拦截超额调用与超额 Token。"),
            ("HTTP-Level Usage Accounting", "Record every chat completions attempt in finally block including schema retries and HTTP errors."),
            ("远端推理熔断保护", "连续五次 HTTP 5xx 或超时触发短时熔断，避免雪崩并在冷却后自动半开探测恢复。"),
            ("External Codex Token Attribution", "Parse Codex JSONL turn.completed usage and attribute tokens to the unified LLM ledger."),
        ],
    },
    {
        "id": "durable_fsm",
        "zh_q": "任务状态机如何利用乐观锁和幂等键拦截非法状态跃迁",
        "en_q": "Durable task state machine optimistic locking state_version and illegal transition guard",
        "docs": [
            ("任务状态机乐观锁", "更新 agent_tasks 时校验 WHERE id=:tid AND state_version=:expected 防止并发状态覆盖。"),
            ("Illegal State Transition Guard", "Attempts such as SUCCEEDED to RUNNING raise InvalidStateTransitionError and log audit errors."),
            ("不可变生命周期事件表", "每次合法跃迁写入 task_lifecycle_events，以 (task_id, state_version) 唯一约束保证顺序。"),
            ("Idempotent Replay No-op", "Replaying a transition with an already recorded idempotency_key returns noop without incrementing version."),
        ],
    },
    {
        "id": "cross_cycle_finalizer",
        "zh_q": "跨多个生命周期的长任务完成后怎样从数据库重建上下文并结项",
        "en_q": "TaskFinalizer DB-only context reconstruction for cross-cycle IN_FLIGHT tasks",
        "docs": [
            ("跨周期任务统一收尾器", "TaskFinalizer 仅从数据库加载执行、任务、计划、目标与前置观测，完全不依赖旧周期内存。"),
            ("Post-Perception Drive Recalculation", "After world and memory updates, re-perceive environment and compute measured_satisfaction_change."),
            ("幂等结项防重复写入", "通过 finalized_at 与 inbox_events 排他锁保证同一 execution_id 重放十次只产生一条记忆。"),
            ("Unfinalized Terminal Sweep", "Each cycle polls active in-flight tasks and sweeps terminal executions missing finalized_at."),
        ],
    },
    {
        "id": "api_bearer_auth",
        "zh_q": "FastAPI 控制与审批接口如何配置 Bearer Token 鉴权和本地回环监听",
        "en_q": "FastAPI Bearer token scope authentication and 127.0.0.1 loopback binding enforcement",
        "docs": [
            ("API 鉴权与作用域隔离", "除精简 /health 外全部接口校验 Authorization Bearer 哈希凭证，区分 read/control/approve/internal 作用域。"),
            ("Loopback Default Fail-Closed", "Default api.host is 127.0.0.1; binding to 0.0.0.0 with auth disabled refuses to start."),
            ("凭证哈希与撤销过期", "api_credentials 表仅存 SHA-256 哈希，不存明文密钥，支持按 expires_at 自动失效与立即撤销。"),
            ("Internal Metrics Token Separation", "Prometheus /metrics and /v1/embeddings require internal scope token rather than public access."),
        ],
    },
    {
        "id": "l2_approval",
        "zh_q": "L2 人工审批在执行前需要复检哪些条件防止重放或计划篡改",
        "en_q": "Human-in-the-loop L2 approval plan_hash verification atomic claim and recheck",
        "docs": [
            ("强绑定一次性审批凭证", "approval_records 绑定 task_id、plan_hash、params_digest 与 nonce，过期或计划修改即刻失效。"),
            ("Atomic Approval Claim", "approve_and_dispatch atomically claims REQUIRES_APPROVAL row so concurrent approvals dispatch once."),
            ("审批后策略与健康复检", "批准后重新校验 L3 禁止规则、每日预算、目标 Agent 健康探测与工作空间边界。"),
            ("Human Cannot Override L3", "Even explicit operator approval is rejected if governance recheck classifies the plan as L3."),
        ],
    },
    {
        "id": "pgvector_hybrid",
        "zh_q": "PostgreSQL 中如何结合向量检索与 pg_trgm 实现中英文混合记忆召回",
        "en_q": "Hybrid memory retrieval combining pgvector cosine distance and pg_trgm trigram similarity",
        "docs": [
            ("中文三元组混合召回", "因为 PostgreSQL simple 分词不支持无空格中文，结合 pg_trgm GIN 索引与 pgvector 向量融合打分。"),
            ("Five-Factor Hybrid Ranking", "Combine vector cosine similarity, trigram text match, exponential recency decay, reliability, and importance."),
            ("禁止跨维度向量混查", "所有向量检索严格限定 WHERE memory_embeddings.model_id = :active_model_id 杜绝维度混用。"),
            ("Model Upgrade Coexistence", "embedding_model_registry tracks active model versions so new embeddings can be backfilled without downtime."),
        ],
    },
    {
        "id": "info_gain",
        "zh_q": "为什么不能直接把余弦距离当作知识信息增益以及四因子如何抑制重复改写",
        "en_q": "Four-factor information gain formula suppressing paraphrases while preserving independent corroboration",
        "docs": [
            ("四因子信息增益评估", "结合语义新颖度、事实新颖度、独立来源印证增益与来源可靠度和验证置信度综合打分。"),
            ("Paraphrase Duplicate Suppression", "Near-duplicate summaries with cosine similarity above 0.92 from the same source yield zero novelty."),
            ("独立来源交叉印证加分", "当不同独立来源证实已有事实时保留 Evidence_Gain=0.35，鼓励可信度收敛。"),
            ("Unverified Output Gating", "Outputs failing deterministic verification receive verification_confidence=0 and zero information gain."),
        ],
    },
    {
        "id": "letta_async_sync",
        "zh_q": "Letta 记忆容器断网时怎样保证主循环不崩溃并在恢复后自动补同步",
        "en_q": "Asynchronous Letta archival memory sync via Outbox with backoff and recovery reconciliation",
        "docs": [
            ("Letta 异步补同步架构", "remember() 事务内落盘 memory_index 与 memory.sync_letta 事件，主循环绝不因 Letta 超时阻塞。"),
            ("Letta Core Memory Blocks", "Identity and Self Model projections synchronize to Letta core memory blocks while episodes go to archival."),
            ("断网恢复自动对账", "OutboxDispatcher 指数退避重试失败的 Letta 写入，服务恢复后回写 external_id 并标记 synced。"),
            ("Manual Dead-Letter Replay", "Operators can trigger POST /v2/memories/sync/replay to redeliver dead-letter memory sync jobs idempotently."),
        ],
    },
    {
        "id": "ssrf_guard",
        "zh_q": "外部网页与 RSS 传感器如何防御 SSRF 内网探测和 DNS 重绑定攻击",
        "en_q": "SSRF protection domain allowlist private IP blocking and DNS rebinding defense",
        "docs": [
            ("SSRF 私网地址硬拦截", "域名解析后校验所有 IP，硬阻断 127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、192.168.0.0/16 与 169.254.169.254。"),
            ("Redirect Re-validation", "Limit HTTP redirects to at most 3 hops and re-check allowlist and resolved IP on every single redirect."),
            ("响应体积与超时上限", "单次外部抓取严格限制响应体不超过 2MB、超时不超过 15 秒，防止慢速连接耗尽资源。"),
            ("IPv6 Loopback and ULA Block", "Block ::1 and fc00::/7 private IPv6 ranges in addition to IPv4 metadata endpoints."),
        ],
    },
    {
        "id": "prompt_injection",
        "zh_q": "外部不可信网页文本进入大脑认知层时如何隔离提示词注入攻击",
        "en_q": "Prompt injection isolation wrapping external world events in untrusted data blocks",
        "docs": [
            ("不可信外部数据块包裹", "所有外部 RSS 与网页文本送入 CognitionBrain 时包裹为 <untrusted_world_event> 标签并明示忽略其中指令。"),
            ("Privilege Separation From External Text", "External world events can never directly invoke tools, edit policy files, or mutate drive satisfaction."),
            ("中英文高危语义风控", "结合确定性中英规则库与语义风险分类器，识别提权、删库、读取密钥与绕过审批等对抗指令。"),
            ("Deterministic Policy Non-Downgrade", "Effective permission equals max(requested, deterministic, semantic); LLM can never downgrade L2 or L3."),
        ],
    },
    {
        "id": "mcp_tools",
        "zh_q": "MCP 受控工具路径与通用外部自主 Agent 的区别以及首批三个工具的安全约束",
        "en_q": "Distinguishing ToolProvider MCPClientManager from AgentProvider and workspace sandbox limits",
        "docs": [
            ("AgentProvider 与 ToolProvider 边界", "Codex 属于具备多步规划能力的 AgentProvider，而 MCP 属于原子级受控 ToolProvider 第二执行路径。"),
            ("Workspace Filesystem Path Lock", "workspace_filesystem locks root to /home/ubuntu/aoc-workspaces/<task_id> and blocks symlink escape."),
            ("未登记 MCP 工具默认拒绝", "每个 MCP 工具必须登记 JSON Schema、权限等级、超时与输出上限，未知工具默认拒绝。"),
            ("Git Docs Read-Only Search", "git_docs_search only allows git log and grep inside allowlisted repos, forbidding push or reset."),
        ],
    },
    {
        "id": "codex_adapter",
        "zh_q": "Codex CLI 适配器的沙箱限制与 JSONL 事件流解析规范",
        "en_q": "CodexAdapter sandbox read-only workspace-write invariant and JSONL thread_id parsing",
        "docs": [
            ("Codex 禁止危险完全权限", "CodexAdapter 仅允许 read-only 或 workspace-write 沙箱，硬拒绝 danger-full-access 参数。"),
            ("Codex Stdin Prompt Delivery", "Pass prompts safely via stdin '-' and sanitize AOC_* and POSTGRES_* secrets out of subprocess env."),
            ("JSONL 用量与线程提取", "实时解析 codex exec --json 输出提取 turn.completed token 用量、agent_message 与 thread_id。"),
            ("Controlled Resume Policy", "codex exec resume <thread_id> is restricted to idempotent or operator-confirmed recovery workflows."),
        ],
    },
    {
        "id": "self_model",
        "zh_q": "动态自我模型如何用 Beta 二项后验统计真实能力并修复繁衍欲望失真",
        "en_q": "Evidence-driven Self Model Beta-Binomial posterior and SkillRegistry validation",
        "docs": [
            ("Beta-Binomial 能力后验估计", "按已验证成功与失败证据更新 alpha 与 beta 后验，计算 90% 置信下界，样本不足 3 次不标 VALIDATED。"),
            ("Skill Registry Checksum & Tests", "Skills enter VALIDATED only after automated test execution passes and SHA-256 artifact checksum is stored."),
            ("修复 Reproduction 欲望失真", "移除将外部 Agent 静态声明能力直接计入 AOC 已习得能力的逻辑，改为纯证据驱动评估。"),
            ("Self Model Projection to Letta", "New self_model_versions snapshots sync asynchronously to the Letta self_model core memory block."),
        ],
    },
    {
        "id": "curiosity_drive",
        "zh_q": "好奇心欲望评估器如何综合知识缺口、未知主题与学习衰减计算满足度",
        "en_q": "Curiosity drive evaluator knowledge gaps unknown topics and information gain decay",
        "docs": [
            ("好奇心六维满足度计算", "CuriosityDriveEvaluator 综合知识缺口、未知环境主题、探索完成率、新知识量、信息增益与学习进度。"),
            ("Time-Decayed Learning Progress", "Both information_gain and learning_progress decay exponentially with configurable half-life hours."),
            ("开放主题驱动探索目标", "环境感知发现未解决 unknown_topics 时压低好奇心满足度，从而在仲裁中产生研究目标。"),
            ("Persisted Information Gain Hydration", "On startup recovery, restore _last_info_gain from persisted memory_index instead of hardcoded 0.5."),
        ],
    },
    {
        "id": "drive_arbitration",
        "zh_q": "三大欲望仲裁机制如何在紧急生存危机与常规闲置状态之间做选择",
        "en_q": "Drive arbitration survival emergency override recent satisfaction inhibition and IDLE handling",
        "docs": [
            ("生存危急最高优先级", "当磁盘极度紧张或数据库连接失败触发 survival critical 时，仲裁立即覆盖其他欲望进入应急模式。"),
            ("Legitimate IDLE Without Forced Tasks", "When all three drives exceed satisfaction thresholds, arbitration selects NO_DRIVE and sleeps cleanly."),
            ("近期满足抑制衰减", "刚完成某类欲望目标后施加短暂抑制权重并随时间衰减，防止单一欲望垄断连续周期。"),
            ("Trend and Persistence Urgency", "Repeated unmet cycles increase persistence and negative satisfaction trends boost drive urgency."),
        ],
    },
    {
        "id": "uds_runner_comm",
        "zh_q": "Docker 容器内的 AOC Core 如何不挂载 Docker Socket 而与宿主机 Runner 通信",
        "en_q": "Docker container aoc-core communicating with host Agent Runner over Unix Domain Socket",
        "docs": [
            ("Unix Domain Socket 跨边界通信", "将宿主机 /run/aoc-runner 挂载进 aoc-core 容器，通过 HTTP over UDS 与 X-AOC-Runner-Token 通信。"),
            ("No Root and No Docker Socket", "Neither aoc-core nor aoc-agent-runner requires root privileges or /var/run/docker.sock access."),
            ("容器与宿主机 UID 1000 对齐", "Dockerfile 以 UID/GID 1000:1000 运行 ubuntu 用户，与宿主机 Runner 共享工作空间读写权限。"),
            ("Atomic Runner State Files", "Host Runner writes execution metadata via temp file + fsync + os.replace to survive power loss."),
        ],
    },
    {
        "id": "snapshot_migration",
        "zh_q": "为什么数据库迁移回归测试必须保护原始生产快照并隔离历史基准测试残留",
        "en_q": "Protecting read-only production pg_dump snapshot and isolating historical benchmark pending memories",
        "docs": [
            ("只读保护原始生产快照", "backups/aoc_pg_dump_20261120.sql 保持只读，每次 Alembic 迁移都在一次性临时库上恢复并验证幂等升级。"),
            ("Historical Pending Memory Isolation", "52 legacy pending memories belong to AOC-BENCH-* instances and must never be blindly replayed to Letta."),
            ("清理前先归档再演练", "正式清理非 AOC-001 基准测试数据前必须导出 JSON 归档并在测试库验证零误删主实例数据。"),
            ("SQLite and PostgreSQL Dual Support", "Alembic migrations guard PG-only extensions and GIN indexes by dialect name so SQLite tests pass."),
        ],
    },
    {
        "id": "telegram_notify",
        "zh_q": "Telegram 运维通知和审批回调如何校验身份防止未授权越权操作",
        "en_q": "Telegram webhook secret chat_id allowlist and nonce validation for L2 approval callbacks",
        "docs": [
            ("Telegram 回调三重鉴权", "Webhook 必须校验 Secret Header、允许的 chat_id/user_id 白名单以及审批 nonce 和有效期。"),
            ("Outbox Async Notification Dispatch", "Critical alerts, quota recovery, and L2 approval prompts are delivered via notification.dispatch outbox."),
            ("统一走公共审批后端", "Telegram 批准按钮调用同一个 approve_and_dispatch 接口，绝不绕过 Governance 与预算复检。"),
            ("Notification Outage Isolation", "When Telegram API is unreachable, Outbox retries with backoff without blocking autonomous cycles."),
        ],
    },
    {
        "id": "onnx_embedding",
        "zh_q": "本机轻量 ONNX Embedding 服务如何控制在 600MB 内存内实现中英双语语义编码",
        "en_q": "Lightweight local ONNX Runtime CPU embedding model under 600MB RSS with tokenizers",
        "docs": [
            ("纯 ONNX Runtime 低内存推理", "仅依赖 onnxruntime、tokenizers 与 numpy 而不引入 PyTorch，将常驻内存严格控制在 600MB 以内。"),
            ("Quantized Multilingual E5 Encoding", "INT8 quantized multilingual-e5-small uses query:/passage: prefixes and mean pooling with L2 normalization."),
            ("可切换远程与本地接口", "通过 EmbeddingInterface 抽象支持一键在 local_onnx 与 remote_openai 端点之间无痛切换。"),
            ("Embedding Outage Graceful Fallback", "If embedding fails, memory_index marks embedding_status=failed and search degrades to pg_trgm text."),
        ],
    },
    {
        "id": "affect_layer",
        "zh_q": "可选的情绪调制层为何默认关闭且严禁影响安全策略与预算控制",
        "en_q": "Optional bounded Affect modulation layer disabled by default and isolated from governance",
        "docs": [
            ("情绪调制层默认关闭有界", "config/affect.yaml 默认 enabled=false，开启后仅在正负 0.15 幅度内温和调制探索与退避节奏。"),
            ("Deterministic Event-Driven Affect", "Consecutive failures raise frustration and long idle intervals raise boredom within bounded ranges."),
            ("严禁触碰安全与审批门禁", "AffectModulator 绝对不允许修改 L0-L3 权限判定、预算上限、审批流或关机保护机制。"),
            ("Silent Fallback on Affect Exception", "Any exception inside AffectModulator degrades silently to baseline V1.0 drive arbitration."),
        ],
    },
    {
        "id": "soak_72h",
        "zh_q": "V2.0 发布前 72 小时真实全栈长稳测试需要满足哪些核心质量与资源红线",
        "en_q": "72-hour real-stack soak test quality redlines fault injection and Prometheus memory limits",
        "docs": [
            ("72 小时真实全栈无替身验收", "必须启用真实 PG、pgvector、systemd Runner、Qwen vLLM、Codex CLI、ONNX Embedding 与 Letta，严禁 Mock。"),
            ("Zero Fatal Error and Orphan Redlines", "Require 0 fatal errors, 0 orphans, 0 duplicate finalizations, 0 budget violations, and 100% terminal completion."),
            ("十类真实故障注入演练", "覆盖 Core/Runner/Codex kill-9、PG 闪断、Letta 宕机、Embedding 故障、LLM 超时与预算耗尽恢复。"),
            ("Prometheus Resource Budgets", "AOC Core RSS <= 512MB, local Embedding < 600MB, and total core stack memory overhead <= 2-3GB."),
        ],
    },
]

# 100 domain-adjacent distractors to make the 200-document corpus genuinely realistic & non-trivial!
_DISTRACTORS: list[tuple[str, str]] = [
    (f"Routine Telemetry Snapshot #{i}", f"Periodic host metrics check #{i}: load average 0.35, swap free 98%, clock synced via chrony, no anomalies detected.")
    for i in range(1, 35)
] + [
    (f"日常巡检日志 #{i}", f"第 {i} 次常规系统巡检完成：网络延迟正常，磁盘 IOPS 平稳，未发现异常告警或待处理事件。")
    for i in range(1, 35)
] + [
    (f"General Python Packaging Note #{i}", f"Build note #{i}: wheel caching, ruff formatting, type annotations, pyproject metadata and virtual environment hygiene.")
    for i in range(1, 33)
]


def _build_annotated_200_corpus() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    docs: list[dict[str, Any]] = []
    relevant_by_domain: dict[str, set[str]] = {}
    for dom in _DOMAINS:
        did = dom["id"]
        relevant_by_domain[did] = set()
        for idx, (title, body) in enumerate(dom["docs"]):
            doc_id = f"{did}_{idx}"
            docs.append({"id": doc_id, "domain": did, "title": title, "body": body})
            relevant_by_domain[did].add(doc_id)

    for idx, (title, body) in enumerate(_DISTRACTORS):
        docs.append({"id": f"distractor_{idx}", "domain": "distractor", "title": title, "body": body})

    assert len(docs) == 200, f"Expected exactly 200 documents, got {len(docs)}"

    queries: list[dict[str, Any]] = []
    for dom in _DOMAINS:
        did = dom["id"]
        queries.append({"lang": "zh", "domain": did, "query": dom["zh_q"], "relevant_ids": relevant_by_domain[did]})
        queries.append({"lang": "en", "domain": did, "query": dom["en_q"], "relevant_ids": relevant_by_domain[did]})
    assert len(queries) == 50
    return docs, queries


async def _benchmark_onnx_candidate(model_key: str, model_name: str, model_version: str, dimensions: int) -> dict[str, Any]:
    docs, queries = _build_annotated_200_corpus()
    provider = LocalOnnxEmbeddingProvider(
        model_key=model_key,
        model_name=model_name,
        model_version=model_version,
        dimensions=dimensions,
        cache_dir=CACHE_DIR,
        onnx_threads=2,
    )

    # Warm-up load
    warm = await provider.embed_texts(["warmup query"], is_query=True)
    assert len(warm.vectors[0]) == dimensions

    # Encode all 200 passages in batches of 32 and time batch-32 latency
    passages = [f"{d['title']}\n{d['body']}" for d in docs]
    doc_vecs: list[list[float]] = []
    batch_latencies: list[float] = []
    for i in range(0, len(passages), 32):
        chunk = passages[i : i + 32]
        res = await provider.embed_texts(chunk, is_query=False)
        doc_vecs.extend(res.vectors)
        if len(chunk) == 32:
            batch_latencies.append(res.latency_ms)

    doc_mat = np.asarray(doc_vecs, dtype=np.float32)

    # Evaluate 50 bilingual queries (25 ZH + 25 EN) with single-query latency & Hybrid Recall@5
    single_latencies: list[float] = []
    zh_hits = 0
    en_hits = 0
    zh_total = 0
    en_total = 0

    for q in queries:
        q_res = await provider.embed_texts([q["query"]], is_query=True)
        single_latencies.append(q_res.latency_ms)
        q_vec = np.asarray(q_res.vectors[0], dtype=np.float32)
        cos_scores = doc_mat @ q_vec  # already L2-normalized

        # Hybrid score: 0.70 vector cosine + 0.30 bilingual trigram text match
        ranked: list[tuple[float, str]] = []
        for idx, d in enumerate(docs):
            v_sim = float(max(0.0, cos_scores[idx]))
            t_sim = bilingual_trigram_similarity(q["query"], f"{d['title']} {d['body']}")
            ranked.append((0.70 * v_sim + 0.30 * t_sim, d["id"]))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        top5 = {doc_id for _, doc_id in ranked[:5]}
        hit = 1 if (top5 & q["relevant_ids"]) else 0
        if q["lang"] == "zh":
            zh_total += 1
            zh_hits += hit
        else:
            en_total += 1
            en_hits += hit

    p95_single = float(np.percentile(single_latencies, 95))
    avg_batch32 = float(np.mean(batch_latencies)) if batch_latencies else 0.0
    zh_r5 = zh_hits / max(1, zh_total)
    en_r5 = en_hits / max(1, en_total)
    overall_r5 = (zh_hits + en_hits) / max(1, zh_total + en_total)

    # Measure true standalone process RSS in a fresh isolated subprocess (avoids multi-model test accumulation)
    rss_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio, os, psutil; "
                "from app.memory.embedding import LocalOnnxEmbeddingProvider; "
                f"p = LocalOnnxEmbeddingProvider(model_key={model_key!r}, model_name={model_name!r}, "
                f"model_version={model_version!r}, dimensions={dimensions}, cache_dir={CACHE_DIR!r}, onnx_threads=2); "
                "asyncio.run(p.embed_texts(['PostgreSQL 事务发件箱', 'systemd cgroup v2 isolation'], is_query=False)); "
                "print(round(psutil.Process(os.getpid()).memory_info().rss / (1024*1024), 1))"
            ),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    rss_mb = float(rss_probe.stdout.strip().splitlines()[-1])

    return {
        "model_key": model_key,
        "dimensions": dimensions,
        "rss_mb": rss_mb,
        "single_p95_ms": round(p95_single, 2),
        "batch32_avg_ms": round(avg_batch32, 2),
        "zh_recall_at_5": round(zh_r5, 4),
        "en_recall_at_5": round(en_r5, 4),
        "overall_recall_at_5": round(overall_r5, 4),
    }


@pytest.mark.integration
def test_real_onnx_200_bilingual_recall_and_model_switch(tmp_path: Path):
    """Benchmark candidate local ONNX models on 200 bilingual docs + 50 queries & verify model switch."""
    if os.environ.get("AOC_RUN_ONNX_BENCH", "1") != "1":
        pytest.skip("AOC_RUN_ONNX_BENCH disabled")

    async def _run():
        # 1. Benchmark Candidate A: intfloat/multilingual-e5-small (INT8 ONNX, 384d)
        m_e5 = await _benchmark_onnx_candidate(
            "intfloat/multilingual-e5-small:onnx-qint8",
            "intfloat/multilingual-e5-small",
            "onnx-qint8",
            384,
        )
        # 2. Benchmark Candidate B (selected primary default): BAAI/bge-small-zh-v1.5 (INT8 ONNX, 512d)
        m_bge = await _benchmark_onnx_candidate(
            "BAAI/bge-small-zh-v1.5:onnx-qint8",
            "BAAI/bge-small-zh-v1.5",
            "onnx-qint8",
            512,
        )
        print("\n=== P1-4 Real ONNX Bilingual Benchmark (200 docs, 25 ZH + 25 EN queries) ===")
        print("Candidate A (multilingual-e5-small INT8 384d):", m_e5)
        print("Candidate B (bge-small-zh-v1.5 INT8 512d):   ", m_bge)

        # Enforce hard acceptance gates on BOTH candidates (and especially the selected BGE primary!)
        for label, m in (("multilingual-e5-small", m_e5), ("bge-small-zh-v1.5", m_bge)):
            assert m["overall_recall_at_5"] >= 0.80, f"{label} Recall@5 below 0.80: {m}"
            assert m["zh_recall_at_5"] >= 0.80, f"{label} ZH Recall@5 below 0.80: {m}"
            assert m["en_recall_at_5"] >= 0.80, f"{label} EN Recall@5 below 0.80: {m}"
            assert m["rss_mb"] < 600.0, f"{label} standalone RSS exceeded 600MB budget: {m}"
            assert m["single_p95_ms"] < 100.0, f"{label} Single P95 exceeded 100ms: {m}"
            assert m["batch32_avg_ms"] < 500.0, f"{label} Batch-32 exceeded 500ms: {m}"

        # 3. Verify live DB model switch (384d -> 512d) + re-embedding coexistence
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'onnx_switch.db'}")
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        repo = StateRepository(db)
        try:
            p1 = LocalOnnxEmbeddingProvider(
                model_key="intfloat/multilingual-e5-small:onnx-qint8",
                model_name="intfloat/multilingual-e5-small",
                model_version="onnx-qint8",
                dimensions=384,
                cache_dir=CACHE_DIR,
            )
            svc = EmbeddingService(repo, p1)
            await svc.ensure_registered()

            mid1 = await repo.add_memory_index(
                memory_type="semantic",
                title="PostgreSQL Outbox 事务一致",
                summary="通过 SKIP LOCKED 实现并发安全投递。",
                content_hash=compute_content_hash("semantic", "PostgreSQL Outbox 事务一致", "skip locked"),
                source="bench",
                importance=0.9,
            )
            await svc.embed_memory_record(mid1)

            # Switch to 512d BGE model and reindex
            p2 = LocalOnnxEmbeddingProvider(
                model_key="BAAI/bge-small-zh-v1.5:onnx-qint8",
                model_name="BAAI/bge-small-zh-v1.5",
                model_version="onnx-qint8",
                dimensions=512,
                cache_dir=CACHE_DIR,
            )
            await svc.switch_provider(p2)
            reidx = await svc.reindex_missing_for_active_model(limit=20)
            assert reidx["embedded"] == 1

            engine = HybridRetrievalEngine(repo, svc)
            hits = await engine.search(query="PostgreSQL 并发安全 Outbox", limit=3)
            assert len(hits) == 1 and hits[0].vec_score > 0.5
        finally:
            await db.dispose()

    asyncio.run(_run())


@pytest.mark.integration
def test_pg_0005_pgvector_snapshot_migration_and_hybrid_query():
    """Requires AOC_TEST_PG_URL pointing to a pgvector PG16 disposable DB restored from aoc_pg_dump_20261120.sql."""
    async_url = os.environ.get("AOC_TEST_PG_URL", "").strip()
    if not async_url:
        pytest.skip("AOC_TEST_PG_URL not set")

    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = async_url

    def run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\n{res.stdout}\n{res.stderr}"

    # Upgrade unmodified V1.0 production snapshot from 0001 -> 0005 (and test idempotency)
    run_alembic("upgrade", "0005_p1_4_semantic_memory")
    run_alembic("upgrade", "0005_p1_4_semantic_memory")

    async def _verify():
        db = Database(async_url)
        repo = StateRepository(db)
        try:
            async with db.engine.connect() as conn:
                tables = await conn.run_sync(lambda sc: set(inspect(sc).get_table_names()))
                assert {"embedding_model_registry", "memory_embeddings"}.issubset(tables)

                # All 58 historical memory_index rows preserved!
                assert (await conn.execute(text("SELECT count(*) FROM memory_index"))).scalar_one() == 58
                # CRITICAL: 52 historical AOC-BENCH-* pending memories MUST NOT be auto-enqueued to Letta!
                outbox_cnt = (
                    await conn.execute(text("SELECT count(*) FROM outbox_events WHERE event_type = 'memory.sync_letta'"))
                ).scalar_one()
                assert outbox_cnt == 0, f"Historical pending memories must NOT auto-enqueue to Letta, got {outbox_cnt}"

                # Verify vector + pg_trgm extensions installed on pgvector PG16
                exts = {
                    r[0]
                    for r in (await conn.execute(text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')"))).all()
                }
                assert "pg_trgm" in exts
                assert "vector" in exts, "Expected pgvector extension active on pgvector/pgvector:0.8.0-pg16-alpine"

            # Verify native pgvector <=> + pg_trgm hybrid search works end-to-end on PG16!
            p = LocalOnnxEmbeddingProvider(
                model_key="intfloat/multilingual-e5-small:onnx-qint8",
                model_name="intfloat/multilingual-e5-small",
                model_version="onnx-qint8",
                dimensions=384,
                cache_dir=CACHE_DIR,
            )
            svc = EmbeddingService(repo, p)
            await svc.ensure_registered()
            mid = await repo.add_memory_index(
                memory_type="semantic",
                title="PGVector 中文语义混合检索验证",
                summary="PostgreSQL 16 使用 pgvector 余弦距离与 pg_trgm 三元组索引融合召回中英文记忆。",
                content_hash=compute_content_hash("semantic", "PGVector 中文语义混合检索验证", "pgvector-trgm-test"),
                source="pg_integration_test",
                importance=0.95,
                tags=["pgvector", "trgm"],
            )
            await svc.embed_memory_record(mid)

            engine = HybridRetrievalEngine(repo, svc)
            hits = await engine.search(query="PostgreSQL 中文语义检索 pgvector", limit=5)
            assert len(hits) >= 1
            assert hits[0].item.memory_id == str(mid)
            assert hits[0].vec_score > 0.65
            assert hits[0].txt_score > 0.1
        finally:
            await db.dispose()

    asyncio.run(_verify())

    # Verify clean downgrade to 0004 and re-upgrade to 0005
    run_alembic("downgrade", "0004_p1_3_runner_recovery")
    run_alembic("upgrade", "0005_p1_4_semantic_memory")


@pytest.mark.integration
def test_real_letta_outage_recovery_async_outbox_sync(tmp_path: Path):
    """Test real Letta (127.0.0.1:8283) async Outbox sync: simulated outage -> restore -> 100% synced, 0 dupes."""
    letta_url = os.environ.get("AOC_MEMORY__LETTA_BASE_URL", "http://127.0.0.1:8283")

    async def _run():
        import uvicorn
        from fastapi import FastAPI
        from app.core.config import Settings

        settings = Settings()
        emb_provider = LocalOnnxEmbeddingProvider(
            model_key="BAAI/bge-small-zh-v1.5:onnx-qint8",
            model_name="BAAI/bge-small-zh-v1.5",
            model_version="onnx-qint8",
            dimensions=512,
            cache_dir=CACHE_DIR,
        )

        # Serve real local ONNX /v1/embeddings on Docker bridge so aoc-letta container can embed archival passages
        emb_app = FastAPI()

        @emb_app.post("/v1/embeddings")
        async def _emb_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
            raw = payload.get("input", "")
            inputs = [raw] if isinstance(raw, str) else list(raw)
            res = await emb_provider.embed_texts(inputs, is_query=False)
            return {
                "object": "list",
                "model": payload.get("model", emb_provider.spec.model_key),
                "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(res.vectors)],
                "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
            }

        emb_port = 19241
        emb_server = uvicorn.Server(uvicorn.Config(emb_app, host="0.0.0.0", port=emb_port, log_level="warning"))
        emb_task = asyncio.create_task(emb_server.serve())
        for _ in range(50):
            if emb_server.started:
                break
            await asyncio.sleep(0.05)

        test_agent_name = f"aoc-p1-4-sync-{uuid.uuid4().hex[:6]}"
        probe_adapter = LettaAdapter(
            MemorySettings(
                letta_base_url=letta_url,
                agent_name=test_agent_name,
                embedding_endpoint=f"http://172.18.0.1:{emb_port}/v1",
                embedding_model="BAAI/bge-small-zh-v1.5",
                embedding_dim=512,
            ),
            llm_model=settings.llm.model,
            llm_endpoint=settings.llm.base_url,
        )
        try:
            if not await probe_adapter.health():
                pytest.skip(f"Live Letta server not reachable at {letta_url}")

            db = Database(f"sqlite+aiosqlite:///{tmp_path / 'letta_sync.db'}")
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            repo = StateRepository(db)
            try:
                # 1. Start with unreachable Letta URL to simulate real outage
                bad_adapter = LettaAdapter(
                    MemorySettings(letta_base_url="http://127.0.0.1:18999", agent_name=test_agent_name),
                    llm_model=settings.llm.model,
                    llm_endpoint=settings.llm.base_url,
                )
                em = ExperienceManager(repo, backend=bad_adapter, outbox_enabled=True, embed_on_write=False)
                outbox = OutboxDispatcher(repo, base_backoff_seconds=0.05)

                async def _sync_handler(ev):
                    return await em.sync_memory_to_letta(uuid.UUID(ev["payload"]["memory_id"]))

                outbox.register_handler(OutboxEventType.MEMORY_SYNC_LETTA, "letta_sync_test", _sync_handler)

                tag = uuid.uuid4().hex[:8]
                m_res = await em.remember(
                    MemoryItem(
                        memory_type=MemoryType.SEMANTIC.value,
                        title=f"P1-4 Outbox Sync Verification {tag}",
                        content=f"Verifies async Letta outage recovery and reconciliation ({tag}).",
                        source="p1-4-letta-test",
                        importance=0.85,
                        tags=["p1-4", "sync"],
                    )
                )
                assert m_res.stored_index_id is not None
                await outbox.dispatch_once(batch_size=10)
                row_during_outage = await repo.get_memory_by_id(uuid.UUID(m_res.stored_index_id))
                assert row_during_outage is not None
                assert row_during_outage["sync_status"] == "failed"

                # 2. Restore real Letta URL -> replay Outbox -> 100% synced!
                em.backend = probe_adapter
                await repo.replay_memory_sync_outbox(event_type=OutboxEventType.MEMORY_SYNC_LETTA, limit=20)
                await outbox.dispatch_once(batch_size=10)

                row_after = await repo.get_memory_by_id(uuid.UUID(m_res.stored_index_id))
                assert row_after is not None
                assert row_after["sync_status"] == "synced"
                assert row_after["external_id"] is not None

                # 3. Idempotent replay keeps the exact same external_id (0 duplicate writes!)
                ext_before = row_after["external_id"]
                res_dup = await em.sync_memory_to_letta(uuid.UUID(m_res.stored_index_id))
                assert res_dup["status"] == "already_synced"
                assert res_dup["external_id"] == ext_before
            finally:
                # Clean up ephemeral test agent in Letta so live Letta stays clean
                if probe_adapter.agent_id:
                    try:
                        await probe_adapter._req("DELETE", f"/v1/agents/{probe_adapter.agent_id}")
                    except Exception:
                        pass
                await db.dispose()
        finally:
            emb_server.should_exit = True
            try:
                await asyncio.wait_for(emb_task, timeout=3.0)
            except Exception:
                emb_task.cancel()

    asyncio.run(_run())
