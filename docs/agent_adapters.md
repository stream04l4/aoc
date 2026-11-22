# AOC V4.0 — 外接行动层：AgentProvider、MCP ToolProvider 与 SkillRuntime 指南

## 1. 三类受控外部执行路径的清晰边界

AOC V4.0 在架构与代码契约上严格区分三类外接执行路径，各司其职、互不混淆：

1. **通用自主 Agent（`AgentProvider`：`app/agents/`，`AgentInterface`）**：
   - 具备开放式规划、代码编写与多轮工具操作能力（当前生产主力为 `CodexAdapter` `codex-cli 0.171.0`）。
   - 经 `AgentRouter` 五因子可解释路由评分选定后，委托宿主机 `systemd` Agent Runner（`aoc-agent-runner.service`）在 cgroup v2 隔离子树中执行。
2. **外接高速技能运行时（`SkillRuntime`：`app/skills/runtime.py`，V4.0 新增）**：
   - AOC 外接自进化区中经真实沙箱验证为 `VALIDATED` 的确定性技能包（`manifest.json + skill.py + test_skill.py`）。
   - **Skill-First Planning 优先执行路径**：规划匹配已有技能时直接在隔离 `python -I` 沙箱执行，**零 Codex 调用、零 Codex Token 消耗**，并真实审计记录 `skill_invocations`。**声明**：`SkillRuntime` 是轻量高速确定性技能执行器，用于沉淀复利技能，而非伪造第二个通用 CLI Agent。
3. **受控原子工具协议（`ToolProvider`：`app/tools/`，MCP stdio JSON-RPC 2.0）**：
   - 由 `MCPClientManager` 统一承载的确定性原子工具（3 个内置工具 + 动态自动桥接的 `VALIDATED` 技能工具 `skill:<canonical_name>`）。

---

## 2. `AgentProvider` 适配器清单与宿主机 Runner

### 2.1 `CodexAdapter`（`codex-cli 0.171.0`，生产主力 `enabled: true`）
- **标准输入与沙箱参数**：Prompt 经 stdin 传递，按治理等级映射 `--sandbox <read-only|workspace-write> -C <workspace> --skip-git-repo-check --json -o <workspace>/.aoc_last_message.txt -`。
- **JSONL 事件与预算记账**：实时解析 `thread.started`（持久化 `agent_executions.thread_id`）、`item.completed` 与 `turn.completed.usage`，并通过 `LLMGateway.record_external_usage()` 并入每日 Token 账本。
- **受控恢复**：仅对幂等或经明确确认的任务启用 `codex exec resume`。

### 2.2 `HermesAdapter` & `GenericCLIAdapter`（`enabled: false` / 扩展预留）
- 因宿主机尚未部署 Hermes，`HermesAdapter` 按既定原则保持 `DISABLED`，不伪造垫片；`GenericCLIAdapter` 作为扩展槽位保留，供未来真实接入其他 CLI Agent。

### 2.3 宿主机 `systemd` Agent Runner（`app/runner/`）
- **部署形态**：`/etc/systemd/system/aoc-agent-runner.service`（非特权 `User=ubuntu`、`Delegate=yes`、`KillMode=process`、`MemoryMax=2G`、`TasksMax=128`），经 Unix Domain Socket `/run/aoc-runner/runner.sock` 与 `aoc-core` 容器通信（零 root、零 Docker Socket）。
- **OS 级崩溃对账**：持久化 `ProcessIdentity` (`pid`, `pgid`, `host_boot_id`, `/proc/<pid>/stat starttime`, `cgroup_path`) 及 `.child.pid` / `.exitcode` 哨兵文件，Core 或 Runner 意外 `kill -9` 重启后仍能 100% 精确对账回收。

---

## 3. 外接隔离 `SkillRuntime`（V4.0 M1 – M2）

所有新结晶技能均由 `SkillRegistryManager.crystallize_from_workspace()` 规范落盘在 `<workspace_root>/skills/<skill_key>/<version>/` 下（`manifest.json` + `skill.py` + `test_skill.py`）：

1. **前置强制安全门禁**：
   - 仅执行数据库状态为 `VALIDATED` 的技能；
   - **SHA-256 防篡改实时核验**：执行前校验磁盘 `artifact_path` SHA-256，若与登记不符立即标记 `DEPRECATED` 并拒绝执行；
   - **Input & Output JSON Schema 校验**：入参与出参分别按 `input_schema` / `output_schema` 校验；
   - **Governance L0–L3 校验**：L3 绝对禁止，L2 需显式 `approved_l2=True`。
2. **隔离沙箱与异常处理**：
   - 使用 `python -I` 启动子进程，清空不安全环境变量（`sanitize_env`）、临时隔离工作目录、严格超时（默认 `15s`）与输出字节上限（`32KB`）；子进程崩溃或超时绝不会导致 `AutonomousLoop` 中断。
3. **Skill-First 执行与无感回退**：
   - 当 `ActionPlan.skill_invocation` 命中且成功，`AutonomousLoop._try_skill_first_execution()` 直接产出完整验证结果，入账 `skill_invocations(invocation_source='skill_first', is_cross_task_reuse=..., estimated_saved_tokens=...)`；
   - 当技能缺失、校验异常或运行失败时，记录 `skill_first_fallback` 决策事件并自动回退至 `CodexAdapter` 兜底完成任务。

---

## 4. 受控 MCP `ToolProvider`（内置 3 工具 + 动态 Skill 桥接）

所有 MCP 工具经 `MCPClientManager.invoke()` 统一执行“未注册默认拒绝 → JSON Schema 校验 → `GovernanceManager`（含 L0–L3 与双语语义风控）→ `BudgetLedger` → `policy_evaluations(subject_type='mcp_tool')` 与 `resource_usage(category='mcp_tool')` 全链路审计”：

### 4.1 3 个内置 stdio JSON-RPC 2.0 工具
1. **`web_fetch_readonly`**（Server: `aoc-mcp-web`，`L0`）：受允许域、DNS 解析 SSRF/私网拦截及 14KB 净荷上限保护的只读网页抓取。
2. **`workspace_filesystem`**（Server: `aoc-mcp-fs`，读 `L0` / 写 `L1`）：严格限制在任务隔离工作区 `/home/ubuntu/aoc-workspaces/<task_id>` 内读写文件，阻止 `..` 穿越与符号链接逃逸。
3. **`git_docs_search`**（Server: `aoc-mcp-gitdocs`，`L0`）：仅允许针对白名单仓库根目录执行只读 `git_log`、`grep -F` 与 `read_doc`（强制 `-c core.hooksPath=/dev/null` 并屏蔽 `.env`/密钥文件）。

### 4.2 V4.0 动态 `skill:<canonical_name>` MCP 工具桥
- 当开启 `skill_mcp_bridge_enabled=True` 且 `SkillRegistryManager` 发生验证/晋升/撤销/回滚变更时，`MCPClientManager.sync_validated_skills_as_tools()` 自动将最新的 `VALIDATED` 规范技能暴露为标准 MCP 工具（名称格式 `skill:<canonical_name>`），底层直接代理至统一的 `SkillRuntime.execute(..., invocation_source="mcp")`。
- `AutonomousLoop` 在行动前可经 `plan.mcp_prefetch_calls` 发起受数量上限与 L0 严格限制的只读预取（结果同样经过 Prompt Injection 扫描与 `<untrusted_world_event>` 围栏隔离）。
