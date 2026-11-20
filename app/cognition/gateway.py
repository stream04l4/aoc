"""Unified LLM Gateway (P1-1 / T9).

All LLM invocations across AOC must pass through LLMGateway:
  - Concurrency control (max_concurrent_llm_calls semaphore)
  - Per-cycle call count cap (max_llm_calls_per_cycle, including retries and fallbacks)
  - Per-cycle token cap (max_tokens_per_cycle)
  - Daily token budget + bounded emergency budget strictly capped by hard_daily_llm_tokens_cap
  - Mandatory HTTP-level accounting (successes, schema retries, and HTTP failures all recorded)
  - External same-endpoint usage attribution (e.g. Codex CLI using AOC's vLLM endpoint)
  - Circuit breaker with cooldown & half-open recovery + shared health_check()
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Iterator, TypeVar

import httpx
from pydantic import BaseModel

from app.cognition.interfaces import BrainError, LLMUsage
from app.cognition.llm_adapter import OpenAICompatibleLLMAdapter
from app.core.config import BudgetSettings, LLMSettings
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.repository import StateRepository, day_start

logger = get_logger("aoc.cognition.gateway")
T = TypeVar("T", bound=BaseModel)

_CALL_SCOPE: ContextVar[dict[str, Any]] = ContextVar("aoc_llm_call_scope", default={})


class LLMGatewayError(BrainError):
    """Base error raised by LLMGateway enforcement."""


class LLMBudgetExceededError(LLMGatewayError):
    """Raised when per-cycle, daily, emergency, or hard-cap LLM budget is exceeded."""


class LLMCircuitOpenError(LLMGatewayError):
    """Raised when the LLM circuit breaker is open after repeated endpoint failures."""


class LLMGatewayInterface(ABC):
    @abstractmethod
    async def complete_structured(
        self,
        operation: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        schema_cls: type[T],
        *,
        cycle_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        emergency: bool = False,
    ) -> T: ...

    @abstractmethod
    async def health_check(self) -> dict[str, Any]: ...

    @abstractmethod
    async def record_external_usage(
        self,
        source: str,
        total_tokens: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        seconds: float = 0.0,
        cycle_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...


class LLMGateway(LLMGatewayInterface):
    """Centralized budget, concurrency, accounting, and circuit-breaker gateway for all LLM traffic."""

    def __init__(
        self,
        llm_settings: LLMSettings,
        budget_settings: BudgetSettings | None = None,
        repo: StateRepository | None = None,
        adapter: OpenAICompatibleLLMAdapter | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.llm_settings = llm_settings
        self.budget = budget_settings or BudgetSettings()
        self.repo = repo
        self._http_client = http_client
        self._adapter = adapter or OpenAICompatibleLLMAdapter(llm_settings, http_client=http_client)

        max_conc = max(1, int(self.budget.max_concurrent_llm_calls))
        self._semaphore = asyncio.Semaphore(max_conc)
        self._state_lock = asyncio.Lock()

        # In-process counters keyed by cycle_id string (also persisted to DB when repo is provided)
        self._cycle_calls: dict[str, int] = {}
        self._cycle_tokens: dict[str, int] = {}
        # Fallback in-memory daily ledger when repo is None (unit tests without DB)
        self._mem_daily_tokens: int = 0
        self._mem_emergency_calls: int = 0
        self._mem_emergency_tokens: int = 0
        self._active_calls: int = 0
        self._peak_concurrent_calls: int = 0

        # Circuit breaker state
        self._consecutive_http_failures: int = 0
        self._circuit_open_until: float = 0.0

    @contextmanager
    def scope(
        self,
        *,
        cycle_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        emergency: bool = False,
    ) -> Iterator[None]:
        """Bind cycle/task/emergency metadata for nested BrainInterface calls without changing signatures."""
        token = _CALL_SCOPE.set({"cycle_id": cycle_id, "task_id": task_id, "emergency": emergency})
        try:
            yield
        finally:
            _CALL_SCOPE.reset(token)

    def get_cycle_stats(self, cycle_id: uuid.UUID | str) -> dict[str, int]:
        key = str(cycle_id)
        return {
            "calls": self._cycle_calls.get(key, 0),
            "tokens": self._cycle_tokens.get(key, 0),
        }

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    @property
    def peak_concurrent_calls(self) -> int:
        return self._peak_concurrent_calls

    async def complete_structured(
        self,
        operation: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        schema_cls: type[T],
        *,
        cycle_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        emergency: bool = False,
    ) -> T:
        ctx = _CALL_SCOPE.get()
        eff_cycle_id = cycle_id if cycle_id is not None else ctx.get("cycle_id")
        eff_task_id = task_id if task_id is not None else ctx.get("task_id")
        eff_emergency = bool(emergency or ctx.get("emergency", False))

        async def _guarded_http_call(op: str, messages: list[dict[str, str]], attempt: int) -> tuple[str, LLMUsage]:
            return await self._execute_http_attempt(
                operation=op,
                messages=messages,
                attempt=attempt,
                cycle_id=eff_cycle_id,
                task_id=eff_task_id,
                emergency=eff_emergency,
            )

        return await self._adapter.complete_structured(
            operation=operation,
            system_prompt=system_prompt,
            user_payload=user_payload,
            schema_cls=schema_cls,
            http_call_fn=_guarded_http_call,
        )

    async def _check_and_reserve_attempt(
        self,
        *,
        operation: str,
        cycle_id: uuid.UUID | None,
        emergency: bool,
    ) -> None:
        async with self._state_lock:
            now_mono = time.monotonic()
            if now_mono < self._circuit_open_until:
                rem = round(self._circuit_open_until - now_mono, 1)
                raise LLMCircuitOpenError(
                    f"LLM circuit breaker open for '{operation}' ({rem}s remaining after "
                    f"{self._consecutive_http_failures} consecutive failures)"
                )

            if cycle_id is not None:
                ckey = str(cycle_id)
                cur_calls = self._cycle_calls.get(ckey, 0)
                if cur_calls >= self.budget.max_llm_calls_per_cycle:
                    raise LLMBudgetExceededError(
                        f"Per-cycle LLM call limit reached ({cur_calls}/{self.budget.max_llm_calls_per_cycle})"
                    )
                cur_tokens = self._cycle_tokens.get(ckey, 0)
                if cur_tokens >= self.budget.max_tokens_per_cycle:
                    raise LLMBudgetExceededError(
                        f"Per-cycle LLM token limit reached ({cur_tokens}/{self.budget.max_tokens_per_cycle})"
                    )

            daily_tokens, em_calls, em_tokens = await self._read_daily_totals()
            hard_cap = max(self.budget.max_llm_tokens_per_day, self.budget.hard_daily_llm_tokens_cap)
            if daily_tokens >= hard_cap:
                raise LLMBudgetExceededError(
                    f"Hard daily LLM token cap reached ({daily_tokens}/{hard_cap})"
                )

            if daily_tokens >= self.budget.max_llm_tokens_per_day:
                if not emergency:
                    raise LLMBudgetExceededError(
                        f"Daily LLM token budget reached ({daily_tokens}/{self.budget.max_llm_tokens_per_day})"
                    )
                if em_calls >= self.budget.emergency_llm_calls_per_day:
                    raise LLMBudgetExceededError(
                        f"Emergency daily LLM call limit reached ({em_calls}/{self.budget.emergency_llm_calls_per_day})"
                    )
                if em_tokens >= self.budget.emergency_llm_tokens_per_day:
                    raise LLMBudgetExceededError(
                        f"Emergency daily LLM token limit reached ({em_tokens}/{self.budget.emergency_llm_tokens_per_day})"
                    )

            # Reserve call slot immediately BEFORE releasing lock so concurrent tasks cannot overshoot
            if cycle_id is not None:
                ckey = str(cycle_id)
                self._cycle_calls[ckey] = self._cycle_calls.get(ckey, 0) + 1
            if emergency:
                self._mem_emergency_calls += 1

    async def _read_daily_totals(self) -> tuple[int, int, int]:
        if self.repo is None:
            return self._mem_daily_tokens, self._mem_emergency_calls, self._mem_emergency_tokens
        since = day_start()
        usage = await self.repo.usage_since("llm", since)
        em = await self.repo.emergency_llm_usage_since(since)
        return int(usage["amount"]), int(em["count"]), int(em["amount"])

    async def _execute_http_attempt(
        self,
        *,
        operation: str,
        messages: list[dict[str, str]],
        attempt: int,
        cycle_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        emergency: bool,
    ) -> tuple[str, LLMUsage]:
        await self._check_and_reserve_attempt(operation=operation, cycle_id=cycle_id, emergency=emergency)

        async with self._semaphore:
            self._active_calls += 1
            if self._active_calls > self._peak_concurrent_calls:
                self._peak_concurrent_calls = self._active_calls

            t0 = time.perf_counter()
            usage = LLMUsage(model=self.llm_settings.model)
            ok = False
            err_msg: str | None = None
            try:
                content, usage = await self._adapter.call_chat_completions(operation, messages)
                ok = True
                async with self._state_lock:
                    self._consecutive_http_failures = 0
                    self._circuit_open_until = 0.0
                return content, usage
            except Exception as exc:
                err_msg = str(exc)
                async with self._state_lock:
                    self._consecutive_http_failures += 1
                    threshold = max(1, int(self.budget.circuit_breaker_threshold))
                    if self._consecutive_http_failures >= threshold:
                        cooldown = max(0.1, float(self.budget.circuit_breaker_cooldown_seconds))
                        self._circuit_open_until = time.monotonic() + cooldown
                        logger.warning(
                            "llm_gateway.circuit_opened",
                            failures=self._consecutive_http_failures,
                            cooldown_seconds=cooldown,
                        )
                raise
            finally:
                self._active_calls -= 1
                elapsed_s = round(time.perf_counter() - t0, 3)
                async with self._state_lock:
                    if cycle_id is not None and usage.total_tokens > 0:
                        ckey = str(cycle_id)
                        self._cycle_tokens[ckey] = self._cycle_tokens.get(ckey, 0) + usage.total_tokens
                    self._mem_daily_tokens += usage.total_tokens
                    if emergency:
                        self._mem_emergency_tokens += usage.total_tokens

                # Mandatory accounting for EVERY attempt (success, retry, or failure)
                if self.repo is not None:
                    try:
                        await self.repo.record_usage(
                            category="llm",
                            amount=float(usage.total_tokens),
                            seconds=elapsed_s,
                            details={
                                "source": "brain_gateway",
                                "operation": operation,
                                "model": usage.model or self.llm_settings.model,
                                "attempt": attempt,
                                "prompt_tokens": usage.prompt_tokens,
                                "completion_tokens": usage.completion_tokens,
                                "success": ok,
                                "emergency": emergency,
                                "error": err_msg[:300] if err_msg else None,
                            },
                            cycle_id=cycle_id,
                            task_id=task_id,
                        )
                    except Exception as log_exc:
                        logger.warning("llm_gateway.usage_record_failed", error=str(log_exc))

    async def record_external_usage(
        self,
        source: str,
        total_tokens: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        seconds: float = 0.0,
        cycle_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Attribute tokens consumed by external agents sharing AOC's LLM endpoint (e.g. Codex)."""
        tokens = max(0, int(total_tokens))
        if tokens == 0 and (prompt_tokens or completion_tokens):
            tokens = max(0, int(prompt_tokens) + int(completion_tokens))
        if tokens <= 0:
            return

        async with self._state_lock:
            self._mem_daily_tokens += tokens
            if cycle_id is not None:
                ckey = str(cycle_id)
                self._cycle_tokens[ckey] = self._cycle_tokens.get(ckey, 0) + tokens

        if self.repo is not None:
            payload = {
                "source": source,
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "external_agent": True,
                **(details or {}),
            }
            await self.repo.record_usage(
                category="llm",
                amount=float(tokens),
                seconds=float(seconds),
                details=payload,
                cycle_id=cycle_id,
                task_id=task_id,
            )

    async def health_check(self) -> dict[str, Any]:
        """Shared LLM endpoint probe respecting the gateway circuit breaker."""
        if self.circuit_open:
            rem = round(max(0.0, self._circuit_open_until - time.monotonic()), 1)
            return {
                "available": False,
                "circuit_open": True,
                "error": f"LLM circuit breaker open ({rem}s remaining)",
            }

        url = self.llm_settings.base_url.rstrip("/") + "/models"
        headers: dict[str, str] = {}
        if self.llm_settings.api_key:
            secret = self.llm_settings.api_key.get_secret_value()
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

    def snapshot_metrics(self) -> dict[str, Any]:
        return {
            "active_calls": self._active_calls,
            "peak_concurrent_calls": self._peak_concurrent_calls,
            "consecutive_http_failures": self._consecutive_http_failures,
            "circuit_open": self.circuit_open,
            "tracked_cycles": len(self._cycle_calls),
            "at": utcnow().isoformat(),
        }
