# AOC V2.0 — 外部 Agent 适配器与受控 MCP ToolProvider 指南

## 1. 严格区分 `AgentProvider` 与 `ToolProvider`（任务书决策 4）

AOC V2.0 在架构与代码契约上严格区分两类外部执行接口：

1. **`AgentProvider`（`app/agents/`，实现 `AgentInterface`）**：
   - 具备独立认知规划、多步工具调用与代码仓操作能力的通用自主 Agent（如 `codex-cli 0.171.0`）。
   - 由 `AgentRegistry` 注册探测，经 `AgentRouter` 五因子可解释评分选定后，交由宿主机 `systemd` Agent Runner（`aoc-agent-runner.service`）在 cgroup v2 隔离子树中异步执行。
2. **`ToolProvider`（`app/tools/`，实现 `ToolProviderInterface`）**：
   - 通过 Model Context Protocol（MCP，stdio JSON-RPC 2.0）提供的**受控确定性原子工具**，由 `MCPClientManager` 统一管理。
   - **重要声明**：接入 3 个受控 MCP 工具建立了**第二条受控工具执行路径**，**不等于**实现了第二个通用自主 Agent，也**不等于**完成了多 Agent 协作。

---

## 2. `AgentProvider` 适配器清单

### 2.1 `CodexAdapter`（`codex-cli 0.171.0`，生产主力 `enabled: true`）
- **调用方式**：通过 stdin 传入 prompt（避免命令行参数泄露），支持 `--sandbox <read-only|workspace-write> -C <workspace> --skip-git-repo-check --json -o <workspace>/.aoc_last_message.txt -`，并可按需开启 `--worktree` 与非临时会话（`persist_session: true`）。
- **JSONL 事件流解析（适配 `0.171.0`）**：
  - 提取 `{"type": "thread.started", "thread_id": "..."}` 并持久化至 `agent_executions.thread_id`；
  - 提取 `{"type": "item.completed"}` 中的工具调用轨迹与 `agent_message`；
  - 提取 `{"type": "turn.completed", "usage": {...}}` 并通过 `LLMGateway.record_external_usage()` 统一计入每日 Token 预算。
- **受控 `codex exec resume`**：`build_resume_spec(..., idempotent_or_confirmed=False)` 强制要求仅在幂等或经人工确认的任务上才允许恢复会话，严禁对未知副作用任务自动 resume。

### 2.2 `HermesAdapter`（`hermes`，保持 `enabled: false` / `DISABLED`）
- 当前宿主机未安装 Hermes，且按《开发任务书 v2.1》决策 4 批准保持 `enabled: false`（`DISABLED`），不伪造兼容垫片；命令行构建已移除任何明文 `--api-key` 参数。

### 2.3 `GenericCLIAdapter`（`generic-dsh`，扩展预留 `enabled: false`）
- 通用命令行模板适配器，默认关闭，供未来接入经实测验证的 CLI Agent。

---

## 3. 宿主机 `systemd` Agent Runner（`app/runner/`）

- **服务单元**：`/etc/systemd/system/aoc-agent-runner.service`（`User=ubuntu`, `Delegate=yes`, `KillMode=process`, `MemoryMax=2G`, `TasksMax=128`）。
- **通信协议**：Unix Domain Socket `/run/aoc-runner/runner.sock`（权限 `0660`，零 root 权限、零 Docker Socket 挂载）。
- **进程身份与崩溃恢复**：每次执行采集 `ProcessIdentity` (`pid`, `process_group_id`, `host_boot_id`, `/proc/<pid>/stat` `process_start_ticks`, `cgroup_path`) 并通过 POSIX `/bin/sh` shim 原子落盘 `.child.pid` 与 `.exitcode`；无论 Core 或 Runner 遭遇 `kill -9`，重启后均可 100% 精确对账。

---

## 4. 受控 MCP `ToolProvider`（`app/tools/`，3 个内置 stdio JSON-RPC 2.0 工具）

所有 MCP 工具调用必须通过 `MCPClientManager.invoke()`，强制执行：默认拒绝未注册工具 → JSON Schema 校验 → `GovernanceManager`（L0–L3 + 中英语义风控）→ `BudgetLedger` → `policy_evaluations(subject_type='mcp_tool')` 与 `resource_usage(category='mcp_tool')` 审计落库：

1. **`web_fetch_readonly`**（Server: `aoc-mcp-web`，权限 `L0`）：仅允许抓取 `config/sensors.yaml` 白名单域名，复用完整 SSRF / DNS Rebinding / 私网 IP / 重定向阻断与 14KB 净荷上限。
2. **`workspace_filesystem`**（Server: `aoc-mcp-fs`，`list_dir`/`read_file` 为 `L0`，`write_file` 为 `L1`）：严格锁死在 `/home/ubuntu/aoc-workspaces/<task_id>` 内，阻断 `..` 路径穿越与符号链接逃逸。
3. **`git_docs_search`**（Server: `aoc-mcp-gitdocs`，权限 `L0`）：仅允许对白名单仓库根目录执行只读 `git_log`、`grep -F` 与 `read_doc`，强制 `-c core.hooksPath=/dev/null` 并拦截 `.env`/密钥文件读取。
