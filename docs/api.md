# AOC V2.0 (P1-1a) — HTTP API Reference

Default bind: `http://127.0.0.1:9200` (configured via `config/settings.yaml`; non-loopback binding is refused unless `api.auth_enabled=true` and an admin key or active `api_credentials` record exists).

## Authentication & Scopes

- `GET /health`: public minimal status summary (no credentials required).
- All other endpoints require `Authorization: Bearer <token>` validated against `AOC_API__ADMIN_KEY` (`admin` scope), `AOC_API__INTERNAL_TOKEN` (`internal` scope), or SHA-256 hashed rows in `api_credentials`.
- Scopes: `read` (GET observability/world), `control` (world mutation & `/control/*`), `approve` (`/approvals/*`), `internal` (`/metrics`, `/v1/embeddings`), `admin` (all scopes).

## Observability

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health status of AOC, PostgreSQL, Letta, lifecycle state, and scheduler |
| `GET` | `/state` | Current organism identity, lifecycle state, 3 drive states, and daily budget status |
| `GET` | `/drives` | Latest persisted `DriveState` records |
| `GET` | `/cycles?limit=20` | Recent autonomous cycle audit records |
| `GET` | `/goals?status=&limit=50` | Active and historical goals |
| `GET` | `/tasks?status=&limit=50` | External agent tasks and verification outcomes |
| `GET` | `/v2/tasks/{task_id}/lifecycle` | Immutable `task_lifecycle_events` state machine history (`read` scope) |
| `GET` | `/v2/tasks/{task_id}/attempts` | All `agent_executions` attempts for a task (`read` scope) |
| `POST` | `/v2/tasks/{task_id}/cancel` | Cancel an active/queued/pending task and enqueue finalization (`control` scope) |
| `GET` | `/agents` | Registered external agents and live probe results |
| `GET` | `/memories?memory_type=&query=&limit=20` | Recall memories across Letta and PostgreSQL index |
| `GET` | `/events?limit=50&event_type=` | Structured system event log |
| `GET` | `/decisions?limit=50` | Arbitration, policy, fallback, and approval decisions |
| `GET` | `/metrics` | Prometheus metrics endpoint |

## Digital Environment Injection

| Method | Path | Description |
|---|---|---|
| `GET` | `/world?kind=&status=` | List world items (`knowledge_topic`, `capability`, `simulation`, `event`, `fact`) |
| `POST` | `/world` | Inject or update a world item (optionally wakes a sleeping organism) |
| `DELETE` | `/world/{kind}/{key}` | Remove a world item |

## Lifecycle & Governance Control

| Method | Path | Description |
|---|---|---|
| `POST` | `/control/pause` | Pause autonomous loop (`PAUSED`) |
| `POST` | `/control/resume` | Resume autonomous loop (`ACTIVE`) |
| `POST` | `/control/wake` | Wake sleeping organism immediately |
| `POST` | `/control/cycle` | Run one autonomous cycle synchronously (`409 Conflict` if a cycle is already running) |
| `POST` | `/approvals/{task_id}/approve` | Approve an L2 task via `approve_and_dispatch()` (re-validates policy, budget, `plan_hash`, agent health, workspace; atomic claim) |
| `POST` | `/approvals/{task_id}/reject` | Reject an L2 task and sync plan/goal/approval records |
| `POST` | `/v2/tasks/{task_id}/reconcile` | Reconcile task/execution state against Host Agent Runner (Bearer `control`) |
| `GET` | `/v2/runners` | List registered host systemd Agent Runners and heartbeat status (Bearer `read`) |
| `GET` | `/v2/runners/{runner_id}/executions` | Inspect executions tracked by a specific Host Agent Runner (Bearer `read`) |
| `POST` | `/v2/memories/search` | Hybrid semantic + trigram + recency + reliability + importance memory search (Bearer `read`) |
| `GET` | `/v2/memories/embeddings/status` | Active embedding provider, registered models, and embedding coverage stats (Bearer `read`) |
| `GET` | `/v2/memories/sync/status` | PostgreSQL memory_index vs Letta Outbox synchronization stats (Bearer `read`) |
| `POST` | `/v2/memories/sync/replay` | Replay dead-letter/pending memory embedding or Letta sync outbox events (Bearer `control`) |
| `GET` | `/v2/world/events` | List deduplicated, scored, and provenance-tracked external world events (Bearer `read`) |
| `GET` | `/v2/world/sources` | List configured digital world sensors with checkpoint and health status (Bearer `read`) |
| `POST` | `/v2/world/sources` | Register or update an allowlisted RSS/Git/Web/System world source (Bearer `control`) |
| `POST` | `/v2/world/sources/{source_id}/enable` | Enable a world sensor source (Bearer `control`) |
| `POST` | `/v2/world/sources/{source_id}/disable` | Circuit-break / disable a world sensor source (Bearer `control`) |
| `POST` | `/v2/world/poll` | Trigger a fault-isolated poll of due/selected world sensors (Bearer `control`) |
| `GET` | `/v2/governance/policies` | Inspect active read-only governance policy rules and semantic classifier status (Bearer `read`) |
| `GET` | `/v2/governance/audit` | Query `policy_evaluations` audit records across plan, approval_recheck, and mcp_tool (Bearer `read`) |
| `GET` | `/v2/approvals` | List bound L2 `approval_records` (optional `status` filter; Bearer `read`) |
| `POST` | `/v2/approvals/{approval_id}/approve` | Approve a bound single-use L2 request by `approval_id` (Bearer `approve`) |
| `POST` | `/v2/approvals/{approval_id}/reject` | Reject a bound L2 request by `approval_id` (Bearer `approve`) |
| `POST` | `/v2/webhooks/telegram` | Telegram Bot approval callback webhook (`X-Telegram-Bot-Api-Secret-Token` + chat allowlist + nonce TTL) |
| `GET` | `/v2/tools` | List registered MCP `ToolProvider` tools (explicitly distinct from `GET /agents` `AgentProvider`; Bearer `read`) |
| `POST` | `/v2/tools/{tool_name}/invoke` | Invoke a guarded MCP tool via `MCPClientManager` with schema, Governance, Budget, and audit checks (Bearer `control`) |
| `GET` | `/v2/self-model` | Latest versioned dynamic Self Model snapshot (`self_model_versions`) (Bearer `read`) |
| `GET` | `/v2/self-model/history` | Historical Self Model snapshots ordered by `version_number` descending (Bearer `read`) |
| `GET` | `/v2/capabilities` | Evidence-driven capability profiles with Beta-Binomial posterior & 90% confidence bounds (Bearer `read`) |
| `GET` | `/v2/skills` | Registered skills with live SHA-256 artifact integrity checks (Bearer `read`) |
| `GET` | `/v2/skills/{skill_id}` | Detailed skill metadata, artifact checksum integrity, and validation test history (Bearer `read`) |
