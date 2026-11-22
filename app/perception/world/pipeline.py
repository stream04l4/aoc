"""WorldEventPipeline: fault-isolated ingestion, dedup, Prompt-Injection filtering, scoring & topic queuing (V2.0 P1-5).

Pipeline flow (Task Book Section 5.5):
  RAW EVENT -> Schema validation -> (source_id, content_hash) dedup -> text sanitization ->
  Prompt Injection check -> importance & keyword scoring (+ optional Embedding novelty check) ->
  persist in world_events -> high-value clean events queued into world_states(kind='knowledge_topic') ->
  Curiosity drive evaluation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Sequence

from app.drives.interfaces import clamp01
from app.memory.embedding import EmbeddingError, EmbeddingService, cosine_similarity
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.perception.world.interfaces import DigitalWorldSensor, SensorPollResult, WorldEvent, WorldSourceConfig
from app.perception.world.security import (
    detect_prompt_injection,
    sanitize_external_text,
    wrap_untrusted_world_event,
)
from app.storage.repository import StateRepository

logger = get_logger("aoc.perception.world.pipeline")


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _slugify_topic_key(source_id: str, title: str, content_hash: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{source_id}-{title[:48]}".lower()).strip("-")
    slug = slug[:80] if slug else source_id[:40]
    return f"world:{slug}:{content_hash[:8]}"


class WorldEventPipeline:
    def __init__(
        self,
        repo: StateRepository,
        sensors: Sequence[DigitalWorldSensor] = (),
        *,
        interest_keywords: Sequence[str] = (),
        importance_threshold: float = 0.45,
        embedding_service: EmbeddingService | None = None,
        allowed_domains: Sequence[str] = (),
        allowed_git_roots: Sequence[str] = (),
        open_public_https: bool = False,
        max_dynamic_sources: int = 20,
    ) -> None:
        self.repo = repo
        self._sensors: dict[str, DigitalWorldSensor] = {s.sensor_type: s for s in sensors}
        self.interest_keywords = [kw.strip().lower() for kw in interest_keywords if kw and kw.strip()]
        self.importance_threshold = clamp01(importance_threshold)
        self.embedding_service = embedding_service
        self.allowed_domains = list(allowed_domains)
        self.allowed_git_roots = list(allowed_git_roots)
        self.open_public_https = bool(open_public_https)
        self.max_dynamic_sources = max(1, int(max_dynamic_sources))

    def register_sensor(self, sensor: DigitalWorldSensor) -> None:
        self._sensors[sensor.sensor_type] = sensor

    async def register_dynamic_source(
        self,
        *,
        source_id: str,
        sensor_type: str,
        name: str,
        config: dict[str, Any],
        reliability_score: float = 0.75,
        poll_interval_seconds: int = 3600,
        enabled: bool = True,
        resolver: Any = None,
    ) -> dict[str, Any]:
        """Guarded extension port for dynamically adding external perception sources (V3.0 M3 / V4.0 M3).

        - When `open_public_https=False` (strict mode): requires hostname in `allowed_domains`.
        - When `open_public_https=True` (V4.0 M3 Curiosity mode): permits ANY public HTTPS domain
          while strictly enforcing SSRF/private/loopback/metadata IP blocking and max source cap,
          and propagates the approved public hostname to active RSS/Web sensors.
        """
        import ipaddress
        from urllib.parse import urlparse
        from pathlib import Path
        from app.perception.world.security import is_blocked_ip, validate_and_resolve_url

        stype = str(sensor_type or "").strip().lower()
        if stype not in {"rss", "web", "git"}:
            raise ValueError(f"Unsupported dynamic sensor_type '{stype}'")

        existing_sources = await self.repo.list_world_sources()
        if len(existing_sources) >= self.max_dynamic_sources and not any(
            s.get("source_id") == source_id.strip()[:120] for s in existing_sources
        ):
            raise PermissionError(f"Dynamic source cap reached ({len(existing_sources)}/{self.max_dynamic_sources})")

        if stype in {"rss", "web"}:
            url = str(config.get("url") or "").strip()
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme != "https" or not host:
                raise PermissionError(f"Dynamic {stype} source requires https URL with valid host")
            if parsed.username or parsed.password:
                raise PermissionError("Credentials embedded in URL are forbidden")
            if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".lan")):
                raise PermissionError(f"Internal hostname '{host}' is forbidden")
            # Reject bare IP literals outright
            try:
                ipaddress.ip_address(host)
                if is_blocked_ip(host):
                    raise PermissionError(f"Blocked private/loopback/metadata IP literal: {host}")
                raise PermissionError(f"Bare IP literal '{host}' is not allowed for dynamic perception sources")
            except ValueError:
                pass

            allowed_low = {d.lower() for d in self.allowed_domains}
            if not self.open_public_https:
                if allowed_low and host not in allowed_low:
                    raise PermissionError(f"Domain '{host}' is not in allowed_domains allowlist")
            else:
                # V4.0 M3: Verify DNS resolution blocks ALL private/loopback/link-local/metadata IPs
                await validate_and_resolve_url(url, [host], resolver=resolver, allow_http=False)
                if host not in allowed_low:
                    self.allowed_domains.append(host)
                    for sensor in self._sensors.values():
                        doms = getattr(sensor, "allowed_domains", None)
                        if isinstance(doms, list) and host not in {d.lower() for d in doms}:
                            doms.append(host)
        elif stype == "git":
            repo_raw = str(config.get("repo_path") or "").strip()
            if not repo_raw:
                raise ValueError("Dynamic git source requires repo_path")
            resolved = Path(repo_raw).resolve()
            if self.allowed_git_roots and not any(
                resolved == Path(r).resolve() or Path(r).resolve() in resolved.parents
                for r in self.allowed_git_roots
            ):
                raise PermissionError(f"Git root '{resolved}' is outside allowed_git_roots")

        src = WorldSourceConfig(
            source_id=source_id.strip()[:120],
            sensor_type=stype,
            name=name.strip()[:240],
            config=dict(config),
            reliability_score=clamp01(reliability_score),
            poll_interval_seconds=max(300, int(poll_interval_seconds)),
            enabled=bool(enabled),
        )
        await self.repo.upsert_world_source(
            source_id=src.source_id,
            sensor_type=src.sensor_type,
            name=src.name,
            config=src.config,
            reliability_score=src.reliability_score,
            poll_interval_seconds=src.poll_interval_seconds,
            enabled=src.enabled,
        )
        return src.model_dump(mode="json")

    async def sync_configured_sources(self, sources_cfg: Sequence[dict[str, Any]]) -> int:
        """Seed/update world_event_sources from config/sensors.yaml without overwriting operator enable/disable."""
        count = 0
        for raw in sources_cfg:
            src = WorldSourceConfig.model_validate(raw)
            existing = await self.repo.get_world_source(src.source_id)
            if existing is None:
                await self.repo.upsert_world_source(
                    source_id=src.source_id,
                    sensor_type=src.sensor_type,
                    name=src.name,
                    config=src.config,
                    reliability_score=src.reliability_score,
                    poll_interval_seconds=src.poll_interval_seconds,
                    enabled=src.enabled,
                )
                count += 1
        return count

    async def _compute_importance(self, ev: WorldEvent) -> tuple[float, float | None]:
        """Compute deterministic relevance + optional semantic novelty score in [0, 1]."""
        haystack = f"{ev.title} {ev.content}".lower()
        kw_hits = sum(1 for kw in self.interest_keywords if kw in haystack) if self.interest_keywords else 0
        kw_score = clamp01(0.25 + min(0.75, kw_hits * 0.20)) if self.interest_keywords else 0.65

        # Optional semantic novelty boost/suppression via active EmbeddingService (degrades silently if offline)
        novelty_score: float | None = None
        if self.embedding_service is not None:
            try:
                model_id, vec = await self.embedding_service.embed_query(f"{ev.title}\n{ev.content[:500]}")
                known = await self.repo.list_embeddings_for_model(model_id, limit=50)
                if known:
                    max_sim = max(cosine_similarity(vec, row["embedding"]) for row in known)
                    novelty_score = clamp01(1.0 - max(0.0, max_sim))
                else:
                    novelty_score = 1.0
            except (EmbeddingError, Exception):
                novelty_score = None

        rel = clamp01(ev.source_reliability)
        if novelty_score is not None:
            raw = (0.55 * kw_score + 0.45 * novelty_score) * (0.5 + 0.5 * rel)
        else:
            raw = kw_score * (0.5 + 0.5 * rel)
        return round(clamp01(raw), 4), (round(novelty_score, 4) if novelty_score is not None else None)

    async def ingest_events(self, source: WorldSourceConfig, events: Sequence[WorldEvent]) -> dict[str, int]:
        """Filter, deduplicate, guard against Prompt Injection, persist, and queue high-value topics."""
        inserted = 0
        duplicates = 0
        queued_topics = 0
        injection_blocked = 0
        ignored = 0

        for ev in events:
            # Normalize & sanitize external content
            ev.title = sanitize_external_text(ev.title, max_chars=400) or "(untitled)"
            ev.content = sanitize_external_text(ev.content, max_chars=3000)
            ev.source_reliability = clamp01(ev.source_reliability or source.reliability_score)
            chash = ev.ensure_hash()

            # 1. Exact DB dedup by (source_id, content_hash)
            if await self.repo.world_event_exists(source.source_id, chash):
                duplicates += 1
                continue

            # 2. Bilingual Prompt Injection check (NEVER promote adversarial payloads to open curiosity topics!)
            inj_flags = detect_prompt_injection(f"{ev.title}\n{ev.content}")
            meta = dict(ev.metadata or {})
            meta["untrusted_wrapped"] = wrap_untrusted_world_event(
                source.source_id, ev.title, ev.content, metadata=meta
            )

            if inj_flags:
                injection_blocked += 1
                meta["injection_flags"] = inj_flags
                await self.repo.create_world_event(
                    source_id=source.source_id,
                    event_type=ev.event_type,
                    title=ev.title,
                    content=ev.content,
                    content_hash=chash,
                    source_reliability=ev.source_reliability,
                    importance_score=0.0,
                    status="ignored",
                    metadata=meta,
                    observed_at=ev.observed_at,
                )
                await self.repo.log_event(
                    "world.prompt_injection_flagged",
                    message=f"Quarantined suspected prompt injection from source '{source.source_id}': {inj_flags}",
                    payload={"source_id": source.source_id, "title": ev.title[:160], "flags": inj_flags},
                    level="warning",
                )
                inserted += 1
                continue

            # 3. Importance + optional semantic novelty scoring
            importance, sem_novelty = await self._compute_importance(ev)
            if sem_novelty is not None:
                meta["semantic_novelty"] = sem_novelty

            should_queue = importance >= self.importance_threshold
            status = "queued_topic" if should_queue else "ignored"

            event_id = await self.repo.create_world_event(
                source_id=source.source_id,
                event_type=ev.event_type,
                title=ev.title,
                content=ev.content,
                content_hash=chash,
                source_reliability=ev.source_reliability,
                importance_score=importance,
                status=status,
                metadata=meta,
                observed_at=ev.observed_at,
            )
            if event_id is None:
                duplicates += 1
                continue
            inserted += 1

            # 4. Promote high-value verified-clean event to world_states(kind='knowledge_topic') for Curiosity
            if should_queue:
                topic_key = _slugify_topic_key(source.source_id, ev.title, chash)
                await self.repo.upsert_world_item(
                    kind="knowledge_topic",
                    key=topic_key,
                    value={
                        "title": ev.title,
                        "description": ev.content[:1000],
                        "importance": importance,
                        "source_id": source.source_id,
                        "event_id": str(event_id),
                        "event_type": ev.event_type,
                        "untrusted": True,
                    },
                    status="open",
                    source=f"world_sensor:{source.source_id}",
                    is_simulated=False,
                )
                queued_topics += 1
            else:
                ignored += 1

        return {
            "inserted": inserted,
            "duplicates": duplicates,
            "queued_topics": queued_topics,
            "injection_blocked": injection_blocked,
            "ignored": ignored,
        }

    async def poll_source(self, source_id: str, *, force: bool = False) -> dict[str, Any]:
        """Poll a single registered source with complete fault isolation and checkpoint persistence."""
        row = await self.repo.get_world_source(source_id)
        if row is None:
            return {"source_id": source_id, "ok": False, "error": "source not found"}
        if not row["enabled"] and not force:
            return {"source_id": source_id, "ok": True, "skipped": "disabled"}

        sensor = self._sensors.get(row["sensor_type"])
        if sensor is None:
            err = f"no sensor registered for type '{row['sensor_type']}'"
            await self.repo.update_world_source_health(source_id, health_status="error", last_error=err)
            return {"source_id": source_id, "ok": False, "error": err}

        cp = await self.repo.get_sensor_checkpoint(source_id)
        now = utcnow()
        if not force and cp and cp.get("last_polled_at") is not None:
            elapsed = (now - _ensure_utc(cp["last_polled_at"])).total_seconds()
            if elapsed < float(row["poll_interval_seconds"]):
                return {"source_id": source_id, "ok": True, "skipped": "not_due"}

        src_cfg = WorldSourceConfig(
            source_id=row["source_id"],
            sensor_type=row["sensor_type"],
            name=row["name"],
            config=row["config"],
            reliability_score=row["reliability_score"],
            poll_interval_seconds=row["poll_interval_seconds"],
            enabled=row["enabled"],
        )
        cursor = dict((cp or {}).get("cursor") or {})

        try:
            res: SensorPollResult = await sensor.poll(src_cfg, cursor)
            if res.error:
                raise RuntimeError(res.error)
            stats = await self.ingest_events(src_cfg, res.events)
            await self.repo.upsert_sensor_checkpoint(
                source_id=source_id,
                cursor=res.new_cursor,
                last_polled_at=now,
                last_success_at=now,
                consecutive_failures=0,
            )
            await self.repo.update_world_source_health(source_id, health_status="healthy", last_error=None)
            return {
                "source_id": source_id,
                "sensor_type": row["sensor_type"],
                "ok": True,
                "not_modified": res.not_modified,
                "fetched": len(res.events),
                **stats,
            }
        except Exception as exc:
            prev_fail = int((cp or {}).get("consecutive_failures", 0))
            err_msg = str(exc)[:500]
            await self.repo.upsert_sensor_checkpoint(
                source_id=source_id,
                cursor=cursor,
                last_polled_at=now,
                consecutive_failures=prev_fail + 1,
            )
            await self.repo.update_world_source_health(source_id, health_status="error", last_error=err_msg)
            await self.repo.log_event(
                "world.sensor_poll_failed",
                message=f"World sensor '{source_id}' failed: {err_msg}",
                payload={"source_id": source_id, "sensor_type": row["sensor_type"], "consecutive_failures": prev_fail + 1},
                level="warning",
            )
            logger.warning("world.sensor_poll_failed", source_id=source_id, error=err_msg)
            return {"source_id": source_id, "sensor_type": row["sensor_type"], "ok": False, "error": err_msg}

    async def poll_all(self, *, force: bool = False) -> list[dict[str, Any]]:
        sources = await self.repo.list_world_sources(enabled_only=True)
        results: list[dict[str, Any]] = []
        for s in sources:
            results.append(await self.poll_source(s["source_id"], force=force))
        return results
