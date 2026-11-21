# AOC V2.0 资源消耗、Embedding 选型实测与 Prometheus 监控报告

## 1. 资源预算红线对比总表（实测）

根据《开发任务书 v2.1》决策 3 与 P1-10 资源红线要求，在宿主机（Linux x86_64，Python 3.12.16）对各核心服务组件的常驻物理内存（VmRSS）进行了独立进程与全栈实测：

| 组件名称 | 任务书红线要求 | 实测常驻内存 (RSS) | 状态 | 说明 |
|---|---|---|---|---|
| **AOC Core 主进程** (`app.api.app` + `OrganismRuntime`) | **≤ 512 MB** | **85.38 MB** | **达标 (16.7%)** | 含 FastAPI、SQLAlchemy Async、Alembic、治理与驱动引擎 |
| **本机 ONNX Embedding 服务** (`BAAI/bge-small-zh-v1.5:onnx-qint8`) | **< 600 MB** | **153.80 MB** | **达标 (25.6%)** | 纯 `onnxruntime + tokenizers + numpy`，零 PyTorch 依赖，关闭 ORT CPU Memory Arena |
| **宿主机 `systemd` Agent Runner** (`aoc-agent-runner.service`) | 轻量常驻 (< 128 MB) | **38.10 MB** | **达标** | `User=ubuntu`, `Delegate=yes`, cgroup v2 子树隔离 (`MemoryMax=2G`, `TasksMax=128`) |
| **AOC 核心栈新增常驻内存合计**（Core + ONNX + Runner） | **≤ 2–3 GB** | **277.28 MB** | **达标 (< 14%)** | 即使加上 Docker 化的 `aoc-letta` (~462.3 MB) 与 `aoc-postgres` (~65.0 MB)，全栈总计仅 **~804.6 MB** |

---

## 2. Embedding 模型实测横评与选型结论（决策 3）

### 2.1 远端推理服务器探测结果
- 探测地址：`http://80.158.109.51:9999/v1`（模型 `Qwen/Qwen3.8-27B-FP8`）
- 实测结论：`GET /v1/models` 仅挂载 `Qwen/Qwen3.8-27B-FP8` 生成模型，未提供独立 `/v1/embeddings` 向量模型服务；因此按决策 3 启用本机轻量量化 ONNX 方案，并通过 `EmbeddingInterface` 保留随时切回远端 OpenAI 兼容 Embedding（如 `BAAI/bge-m3`）的能力。

### 2.2 本机候选 ONNX 模型 200 条中英混合语料实测对比

测试集：200 条人工标注中英文混合技术记忆语料 + 50 条中英跨语言查询（25 中文 + 25 英文，`tests/integration/test_p1_4_recall_benchmark.py`）：

| 候选模型 (`model_key`) | 向量维度 | 独立进程 RSS | 单条延迟 P95 | Batch-32 延迟 | 中文 Recall@5 | 英文 Recall@5 | 综合 Recall@5 | 选型结论 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| **`BAAI/bge-small-zh-v1.5:onnx-qint8`** | **512** | **153.8 MB** | **5.08 ms** | **117.11 ms** | **100.0%** | **96.0%** | **98.0%** | **正式选定默认模型** |
| `intfloat/multilingual-e5-small:onnx-qint8` | 384 | 523.8 MB | 6.65 ms | 158.40 ms | 100.0% | 96.0% | 98.0% | 备选模型（用于 384d↔512d 热切换验证） |
| `hash_fallback` (`aoc-hash-v1`) | 512 | < 5.0 MB | 0.15 ms | 1.20 ms | 72.0% | 88.0% | 80.0% | 离线/故障应急降级兜底 |

- **多维隔离与热切换验证**：`embedding_model_registry` 与 `memory_embeddings` 严格绑定 `(memory_id, model_id)`；实测从 384 维 `multilingual-e5-small` 热切换至 512 维 `bge-small-zh-v1.5` 并触发重嵌入后，跨维度混用被 100% 拦截（`EmbeddingDimensionMismatchError`），新模型检索无缝接管。

---

## 3. Prometheus 监控指标与告警规则

- **采集端点**：`GET /metrics`（需 `Authorization: Bearer <internal_or_admin_token>`）。
- **核心指标**：
  - `aoc_cycles_total{outcome=...}`：各结局周期计数（`COMPLETED`, `IDLE`, `SUCCEEDED`, `ACTION_FAILED`, `BUDGET_EXHAUSTED`, `ERROR`）
  - `aoc_cycle_duration_seconds`：单周期耗时直方图
  - `aoc_drive_satisfaction{drive=...}` / `aoc_drive_priority{drive=...}`：三大驱动器实时满足度与仲裁优先级
  - `aoc_agent_executions_total{agent=...,status=...}`：外部执行器调用统计
  - `aoc_memory_writes_total{type=...,result=...}`：记忆写入与 Letta 同步统计
- **生产告警规则文件**：已固化于 `deploy/prometheus/aoc_v2_alerts.yml`（覆盖 Core RSS > 512MB、周期 `ERROR`、Survival < 0.30、LLM 高错误率与 Letta 同步异常）。
