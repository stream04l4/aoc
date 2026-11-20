# AOC V1.0 — HTTP API Reference

Default bind: `http://127.0.0.1:9200` (configured via `config/settings.yaml`).

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
| `POST` | `/control/cycle` | Run one autonomous cycle synchronously |
| `POST` | `/approvals/{task_id}/approve` | Approve an L2 task awaiting human approval |
| `POST` | `/approvals/{task_id}/reject` | Reject an L2 task awaiting human approval |
