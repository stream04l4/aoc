# AOC V1.0 — External Agent Adapters Guide

Every external agent integrates through `AgentInterface` (`app/agents/base.py`) and is registered/probed by `AgentRegistry` (`app/agents/registry.py`) using `config/agents.yaml`.

## 1. Codex Adapter (`codex`) — Production

- **Invocation**:
  ```bash
  codex exec --sandbox <read-only|workspace-write> -C <workspace> \
    --skip-git-repo-check --ephemeral --json -o <workspace>/.aoc_last_message.txt -
  ```
- **Security**:
  - Refuses `danger-full-access` and never passes `--dangerously-bypass-approvals-and-sandbox`.
  - Strips `AOC_*` and `POSTGRES_*` environment variables before launch.
  - Runs in an isolated workspace `/home/ubuntu/aoc-workspaces/<task_id>`.
- **Lifecycle**: Tracks subprocess PID & process group (`start_new_session=True`), enforces `timeout_seconds`, and supports `get_status()` and `cancel()`.
- **Output**: Parses JSONL events + `-o` final message file + workspace file listing.

## 2. Hermes Adapter (`hermes`) — Experimental

- **Verification Findings (v0.23.2)**:
  - `--run-budget` is rejected by Hermes gateway CLI mode; AOC enforces timeouts externally via process-group `SIGTERM`/`SIGKILL`.
  - `--safe-mode` requires explicit `--provider custom --base-url ... -m ...`, and in v0.23.2 on this host `hermes chat --oneshot --safe-mode` currently exits with code 1 (`"Hermes couldn't confirm how this turn ended"`).
  - Per Task Book 7.2, Hermes is truthfully flagged `experimental: true` and no fake compatibility shim is fabricated; `AgentRegistry` prefers verified non-experimental agents (`codex`) first.

## 3. Generic CLI Adapter (`generic_cli`) — Extension Point (DSH etc.)

- Configured via `command_template` in `config/agents.yaml` with placeholders `{binary}`, `{workspace}`, `{prompt}`, `{timeout}`.
- Remains disabled until a real binary and verified `command_template` pass `probe()`.
