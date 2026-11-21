"""SystemSensor: collects real Linux CPU/memory/disk, monitored services, and LLM probe."""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Awaitable

import httpx
import psutil

from app.core.config import LLMSettings, PerceptionSettings
from app.perception.interfaces import Observation, SensorInterface, unavailable, utcnow
from app.storage.repository import StateRepository


class SystemSensor(SensorInterface):
    name = "system"

    def __init__(
        self,
        perception_cfg: PerceptionSettings,
        llm_cfg: LLMSettings,
        repo: StateRepository | None = None,
        http_client: httpx.AsyncClient | None = None,
        llm_probe_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        runner_health_fn: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
        db_ping_fn: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.cfg = perception_cfg
        self.llm_cfg = llm_cfg
        self.repo = repo
        self._http_client = http_client
        self._llm_probe_fn = llm_probe_fn
        self._runner_health_fn = runner_health_fn
        self._db_ping_fn = db_ping_fn

    async def observe(self) -> Observation:
        now = utcnow()
        sys_state: dict[str, Any] = {}
        avail_count = 0
        total_checks = 4  # cpu, memory, disk, llm

        # 1. CPU (sustained load per core, NOT raw CPU utilization as survival proxy)
        try:
            cores = psutil.cpu_count(logical=True) or 1
            load_1m, load_5m, load_15m = os.getloadavg()
            sys_state["cpu"] = {
                "cores": cores,
                "load_1m": round(load_1m, 3),
                "load_5m": round(load_5m, 3),
                "load_15m": round(load_15m, 3),
                "load_per_core": round(load_5m / cores, 4),
                "percent": psutil.cpu_percent(interval=None),
            }
            avail_count += 1
        except Exception as exc:
            sys_state["cpu"] = unavailable(f"cpu read failed: {exc}")

        # 2. Memory
        try:
            vm = psutil.virtual_memory()
            sys_state["memory"] = {
                "total_bytes": vm.total,
                "available_bytes": vm.available,
                "available_ratio": round(vm.available / vm.total, 4) if vm.total > 0 else 0.0,
                "percent": vm.percent,
            }
            avail_count += 1
        except Exception as exc:
            sys_state["memory"] = unavailable(f"memory read failed: {exc}")

        # 3. Disk (one or more paths; worst path determines overall free_ratio)
        disk_paths: dict[str, Any] = {}
        free_ratios: list[float] = []
        for path in self.cfg.disk_paths or ["/"]:
            try:
                usage = psutil.disk_usage(path)
                ratio = round(usage.free / usage.total, 4) if usage.total > 0 else 0.0
                disk_paths[path] = {
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "free_ratio": ratio,
                    "percent": usage.percent,
                }
                free_ratios.append(ratio)
            except Exception as exc:
                disk_paths[path] = unavailable(f"disk_usage({path}) failed: {exc}")

        if free_ratios:
            sys_state["disk"] = {
                "free_ratio": min(free_ratios),
                "paths": disk_paths,
            }
            avail_count += 1
        else:
            sys_state["disk"] = unavailable("no disk paths readable")

        # 4. LLM endpoint health probe
        if self._llm_probe_fn is not None:
            try:
                sys_state["llm"] = await self._llm_probe_fn()
                avail_count += 1
            except Exception as exc:
                sys_state["llm"] = {"available": False, "error": str(exc)}
                avail_count += 1
        elif self.cfg.llm_probe:
            sys_state["llm"] = await self._probe_llm()
            avail_count += 1
        else:
            sys_state["llm"] = unavailable("llm_probe disabled")

        # 5. Monitored external services + V2.0 P1-5 Runner / DB / Error-rate telemetry
        if self.cfg.monitored_services:
            sys_state["services"] = await self._probe_services()

        if self._db_ping_fn is not None:
            try:
                db_ok = await self._db_ping_fn()
                sys_state["database"] = {"ok": bool(db_ok)}
            except Exception as exc:
                sys_state["database"] = {"ok": False, "error": str(exc)}

        if self._runner_health_fn is not None:
            try:
                r_health = await self._runner_health_fn()
                if r_health is not None:
                    sys_state["runner"] = r_health
            except Exception as exc:
                sys_state["runner"] = {"status": "offline", "error": str(exc)}

        if self.repo is not None:
            try:
                err_15m = await self.repo.count_recent_error_events(minutes=15)
                sys_state["error_rate_15m"] = err_15m
            except Exception:
                pass

        # 6. Apply explicit simulation overrides from world_states (never modifies real host)
        sim_notes: list[str] = []
        if self.repo is not None:
            sims = await self.repo.list_world_items(kind="simulation", status="active")
            for item in sims:
                val = item.get("value") or {}
                key = item["key"]
                if key == "disk_pressure" and "free_ratio" in val:
                    real_disk = sys_state.get("disk")
                    sys_state["disk"] = {
                        "free_ratio": float(val["free_ratio"]),
                        "simulated": True,
                        "real": real_disk,
                    }
                    sim_notes.append(f"simulated disk_pressure free_ratio={val['free_ratio']}")
                elif key == "llm_outage":
                    real_llm = sys_state.get("llm")
                    sys_state["llm"] = {
                        "available": False,
                        "error": str(val.get("error", "simulated LLM outage")),
                        "simulated": True,
                        "real": real_llm,
                    }
                    sim_notes.append("simulated llm_outage")
                elif key == "memory_pressure" and "available_ratio" in val:
                    real_mem = sys_state.get("memory")
                    sys_state["memory"] = {
                        "available_ratio": float(val["available_ratio"]),
                        "simulated": True,
                        "real": real_mem,
                    }
                    sim_notes.append(f"simulated memory_pressure available_ratio={val['available_ratio']}")

        if sim_notes:
            sys_state["simulations_applied"] = sim_notes

        confidence = avail_count / total_checks
        return Observation(
            timestamp=now,
            system_state=sys_state,
            confidence=confidence,
        )

    async def _probe_llm(self) -> dict[str, Any]:
        url = self.llm_cfg.base_url.rstrip("/") + "/models"
        headers: dict[str, str] = {}
        if self.llm_cfg.api_key:
            secret = self.llm_cfg.api_key.get_secret_value()
            if secret:
                headers["Authorization"] = f"Bearer {secret}"

        t0 = time.perf_counter()
        try:
            if self._http_client is not None:
                resp = await self._http_client.get(url, headers=headers, timeout=8.0)
            else:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(url, headers=headers)
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            # 200 OK means endpoint reachable & authenticated
            if resp.status_code == 200:
                return {"available": True, "latency_ms": latency_ms, "status_code": 200}
            return {
                "available": False,
                "latency_ms": latency_ms,
                "status_code": resp.status_code,
                "error": f"HTTP {resp.status_code}",
            }
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            return {"available": False, "latency_ms": latency_ms, "error": str(exc)}

    async def _probe_services(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for svc in self.cfg.monitored_services:
            name = str(svc.get("name") or svc.get("url") or "unknown")
            url = svc.get("url")
            if not url:
                results[name] = unavailable("missing url")
                continue
            t0 = time.perf_counter()
            try:
                if self._http_client is not None:
                    resp = await self._http_client.get(url, timeout=5.0)
                else:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(url)
                results[name] = {
                    "ok": resp.status_code < 500,
                    "status_code": resp.status_code,
                    "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                }
            except Exception as exc:
                results[name] = {"ok": False, "error": str(exc)}
        return results
