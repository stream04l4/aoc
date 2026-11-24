"""EnvironmentSensor: extensible digital environment perception.

Reads persisted digital world state (knowledge topics, capabilities, facts,
external events, active simulations) and supports optional custom async
providers so external feeds can be plugged in without changing core code.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.perception.interfaces import Observation, SensorInterface, unavailable, utcnow
from app.storage.repository import StateRepository

EnvProvider = Callable[[], Awaitable[dict[str, Any]]]


class EnvironmentSensor(SensorInterface):
    name = "environment"

    def __init__(
        self,
        repo: StateRepository,
        providers: dict[str, EnvProvider] | None = None,
        world_pipeline: Any = None,
        poll_on_observe: bool = False,
    ) -> None:
        self.repo = repo
        self._providers: dict[str, EnvProvider] = dict(providers or {})
        self.world_pipeline = world_pipeline
        self.poll_on_observe = poll_on_observe

    def add_provider(self, name: str, provider: EnvProvider) -> None:
        self._providers[name] = provider

    def remove_provider(self, name: str) -> bool:
        return self._providers.pop(name, None) is not None

    async def observe(self) -> Observation:
        now = utcnow()
        poll_summary: list[dict[str, Any]] = []
        if self.world_pipeline is not None and self.poll_on_observe:
            try:
                poll_summary = await self.world_pipeline.poll_all(force=False)
            except Exception:
                poll_summary = []
        try:
            items = await self.repo.list_world_items()
        except Exception as exc:
            return Observation(
                timestamp=now,
                environment_state={"world": unavailable(f"world query failed: {exc}")},
                confidence=0.0,
            )

        unknown_topics: list[dict[str, Any]] = []
        known_topics: list[dict[str, Any]] = []
        capabilities_acquired: list[str] = []
        facts: dict[str, Any] = {}
        active_simulations: list[dict[str, Any]] = []
        external_events: list[dict[str, Any]] = []
        human_inbox: list[dict[str, Any]] = []
        dialogue_turns: list[dict[str, Any]] = []
        social_deferred_replies: list[dict[str, Any]] = []
        social_rhythm: dict[str, Any] = {}

        def _iso(raw_ts: Any) -> str:
            return raw_ts.isoformat() if hasattr(raw_ts, "isoformat") else str(raw_ts or "")

        for item in items:
            kind = item["kind"]
            key = item["key"]
            status = item["status"]
            val = item.get("value") or {}
            if kind == "knowledge_topic":
                entry = {
                    "topic": key,
                    "description": val.get("description", ""),
                    "importance": float(val.get("importance", 0.7)),
                    "source": item.get("source", "environment"),
                    "status": status,
                }
                if status == "open":
                    unknown_topics.append(entry)
                else:
                    known_topics.append(entry)
            elif kind == "capability" and status in {"acquired", "active", "verified"}:
                capabilities_acquired.append(key)
            elif kind == "fact":
                facts[key] = val
            elif kind == "simulation" and status == "active":
                active_simulations.append({"key": key, "value": val})
            elif kind == "event" and status == "open":
                external_events.append(
                    {
                        "key": key,
                        "source": item.get("source", "environment"),
                        "payload": val,
                        "is_simulated": item.get("is_simulated", False),
                        "timestamp": _iso(item["updated_at"]),
                    }
                )
            elif kind == "human_inbox" and status == "open":
                human_inbox.append(
                    {
                        "key": key,
                        "source": item.get("source", "telegram"),
                        "sender": val.get("sender", "joel"),
                        "chat_id": str(val.get("chat_id") or ""),
                        "text": str(val.get("text") or ""),
                        "message_id": val.get("message_id"),
                        "update_id": val.get("update_id"),
                        "reply_to_message_id": val.get("reply_to_message_id"),
                        "received_at": str(val.get("received_at") or _iso(item.get("created_at"))),
                    }
                )
            elif kind == "dialogue_turn":
                dialogue_turns.append(
                    {
                        "key": key,
                        "role": str(val.get("role") or "human"),
                        "sender": str(val.get("sender") or ""),
                        "text": str(val.get("text") or ""),
                        "action": val.get("action"),
                        "reaction_emoji": val.get("reaction_emoji"),
                        "timestamp": str(val.get("timestamp") or _iso(item.get("created_at"))),
                    }
                )
            elif kind == "social_deferred_reply" and status == "open":
                social_deferred_replies.append(
                    {
                        "key": key,
                        "chat_id": str(val.get("chat_id") or ""),
                        "sender": str(val.get("sender") or "joel"),
                        "original_text": str(val.get("original_text") or ""),
                        "message_id": val.get("message_id"),
                        "due_at": str(val.get("due_at") or ""),
                        "reason": str(val.get("reason") or ""),
                        "inbox_key": val.get("inbox_key"),
                        "defer_count": int(val.get("defer_count") or 1),
                        "deferred_activity": str(val.get("deferred_activity") or ""),
                        "created_at": str(val.get("created_at") or _iso(item.get("created_at"))),
                    }
                )
            elif kind == "social_rhythm_state" and key == "primary" and isinstance(val, dict):
                social_rhythm = dict(val)

        # Call any registered external providers (fault-isolated per provider)
        provider_data: dict[str, Any] = {}
        provider_failures = 0
        for pname, fn in self._providers.items():
            try:
                provider_data[pname] = await fn()
            except Exception as exc:
                provider_data[pname] = unavailable(f"provider '{pname}' failed: {exc}")
                provider_failures += 1

        env_state: dict[str, Any] = {
            "unknown_topics": unknown_topics,
            "known_topics": known_topics,
            "capabilities_acquired": sorted(set(capabilities_acquired)),
            "facts": facts,
            "active_simulations": active_simulations,
        }
        if human_inbox or dialogue_turns or social_deferred_replies or social_rhythm:
            human_inbox.sort(key=lambda x: str(x.get("received_at") or ""))
            dialogue_turns.sort(key=lambda x: str(x.get("timestamp") or ""))
            social_deferred_replies.sort(key=lambda x: str(x.get("due_at") or ""))
            env_state["social"] = {
                "inbox": human_inbox,
                "dialogue_turns": dialogue_turns[-16:],
                "deferred_replies": social_deferred_replies,
                "rhythm": social_rhythm,
            }
        if provider_data:
            env_state["providers"] = provider_data
        if poll_summary:
            env_state["world_sensor_poll"] = poll_summary
        try:
            recent_we = await self.repo.list_world_events(status="queued_topic", limit=10)
            if recent_we:
                for we in recent_we:
                    external_events.append(
                        {
                            "event_id": we["id"],
                            "source": we["source_id"],
                            "event_type": we["event_type"],
                            "title": we["title"],
                            "importance_score": we["importance_score"],
                            "untrusted_payload": (we.get("metadata") or {}).get("untrusted_wrapped") or we["content"][:800],
                            "observed_at": we["observed_at"].isoformat()
                            if hasattr(we["observed_at"], "isoformat")
                            else str(we["observed_at"]),
                        }
                    )
        except Exception:
            pass

        total_sources = 1 + len(self._providers)
        confidence = (total_sources - provider_failures) / total_sources

        return Observation(
            timestamp=now,
            environment_state=env_state,
            external_events=external_events,
            confidence=confidence,
        )
