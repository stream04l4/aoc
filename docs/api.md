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
