# AOC V1.0 — Known Issues & Next-Stage Technical Recommendations

## 1. Known Issues (V1.0)

1. **Hermes CLI Non-Interactive Mode (`v0.23.2`) — Experimental**:
   - During verification, `hermes chat --oneshot --safe-mode --format stream-json` rejected `--run-budget` (gateway CLI restriction) and exited with code `1` (`"Hermes couldn't confirm how this turn ended"`).
   - Per Task Book 7.2, `HermesAdapter` is explicitly marked `experimental: true` with no fabricated shim; `AgentRegistry` prioritizes verified non-experimental agents (`codex`).
2. **Letta Image Tagging (`letta/letta:latest` vs `0.16.4`)**:
   - The `latest` tag on Docker Hub transitioned to Letta Code and retired the old Python REST server. `docker-compose.yml` explicitly pins `letta/letta:0.16.4`.
3. **Self-Hosted Embedding Endpoint**:
   - The configured vLLM endpoint (`http://127.0.0.1:8000/v1`) serves `Qwen/Qwen3.8-27B-FP8` only (no embedding model). AOC provides a lightweight deterministic OpenAI-compatible `/v1/embeddings` endpoint (`aoc-local-embed-1024`) so Letta pgvector works offline with zero extra GPU/RAM overhead; for higher semantic recall quality, point `memory.embedding_endpoint` to a dedicated neural embedding server (e.g. `bge-m3` or `nomic-embed-text`).
4. **Codex CLI Inside Docker Container**:
   - `codex` and `hermes` CLIs are installed on the host (`/home/ubuntu/.local/bin/`). When running AOC inside Docker (`aoc-core`), external CLI agents are either mounted/installed into the image or run via host runtime (`.venv/bin/python main.py`). A future sidecar/runner container or bind-mounted CLI binary is recommended for pure-Docker agent execution.
5. **Budget Accounting Granularity**:
   - Daily budget (`max_agent_tasks_per_day`) is enforced across a rolling 24-hour window in PostgreSQL; retry attempts launched within a single approved task can exceed the soft limit by at most `max_task_retries` attempts.

## 2. Next-Stage Technical Recommendations (Phase 1 / V1.1+)

1. **Dedicated Neural Embedding Service**: Deploy a small local embedding model (`bge-small-zh-v1.5` or `bge-m3`) via TEI/vLLM and switch `memory.embedding_endpoint` for richer semantic memory deduplication and retrieval.
2. **Host/Container Agent Runner Bridge**: Add a lightweight HTTP/Unix-socket Agent Runner daemon (or bake `codex` CLI + workspace mount into the AOC image) so containerized `aoc-core` can dispatch real `codex exec` runs seamlessly without host-mode invocation.
3. **Information Gain Quantification**: Replace V1.0 heuristic/LLM `information_gain` with embedding novelty distance (`1 - max_cosine_sim(new_fact, existing_semantic_memories)`) so Curiosity Drive rewards genuinely novel discoveries and ignores redundant summaries.
4. **MCP Tool Executor & DSH Adapter**: Once DSH's CLI/API contract is finalized, add a verified `DSHAdapter` + an `MCPAdapter` implementing `AgentInterface`.
5. **Human Approval UI / Webhook Notifications**: Push `REQUIRES_APPROVAL` (L2) and `CRITICAL` survival events to Telegram/Feishu/WeChat with one-click approve/reject links.
6. **Emotion & Self-Model Layer (V2.0)**: Hydrate the reserved `organism_state.emotion_reserved` column with bounded arousal/valence signals derived from drive trends and error streaks, while preserving hard L0–L3 governance boundaries.
