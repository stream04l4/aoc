"""OpenAI-compatible LLM adapter with strict Pydantic schema validation and retries."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.cognition.interfaces import BrainError, BrainSchemaError, LLMUsage
from app.core.config import LLMSettings
from app.observability.logging import get_logger
from app.observability.metrics import LLM_CALLS, LLM_TOKENS

logger = get_logger("aoc.cognition.llm")
T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> str:
    """Strip <think> tags / markdown fences and isolate the top-level JSON object."""
    cleaned = _THINK_RE.sub("", text or "").strip()
    m = _FENCE_RE.search(cleaned)
    if m:
        return m.group(1).strip()

    # Fallback: scan for the first balanced {...}
    start = cleaned.find("{")
    if start == -1:
        return cleaned
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return cleaned[start : i + 1]
    return cleaned[start:]


UsageCallback = Callable[[str, LLMUsage], Awaitable[None]]
SchemaRetryCallback = Callable[[dict[str, Any]], Awaitable[None]]


def classify_schema_retry_error(exc: Exception, raw_content: str | None) -> str:
    """Classify structured LLM validation failures into coarse diagnostic categories (V5.3 M3)."""
    if not str(raw_content or "").strip():
        return "empty_content"
    msg = str(exc or "").lower()
    if "expecting value: line 1 column 1" in msg:
        return "empty_content"
    if isinstance(exc, json.JSONDecodeError) or "unterminated string" in msg or "eof" in msg:
        return "unterminated_json"
    return "validation_error"


class OpenAICompatibleLLMAdapter:
    """Calls any OpenAI-compatible /chat/completions endpoint and enforces Pydantic output."""

    def __init__(
        self,
        settings: LLMSettings,
        http_client: httpx.AsyncClient | None = None,
        on_usage: UsageCallback | None = None,
        on_schema_retry: SchemaRetryCallback | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client
        self._on_usage = on_usage
        self.on_schema_retry = on_schema_retry

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            secret = self.settings.api_key.get_secret_value()
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
        return headers

    async def complete_structured(
        self,
        operation: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        schema_cls: type[T],
        *,
        http_call_fn: Callable[[str, list[dict[str, str]], int], Awaitable[tuple[str, LLMUsage]]] | None = None,
    ) -> T:
        """Request JSON conforming to ``schema_cls`` with bounded retries on schema error."""
        schema_json = json.dumps(schema_cls.model_json_schema(), ensure_ascii=False)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"{system_prompt}\n\n"
                    "OUTPUT RULES:\n"
                    "- Return ONLY one valid JSON object matching the JSON Schema below.\n"
                    "- Do NOT include markdown prose outside JSON.\n"
                    f"JSON Schema:\n{schema_json}"
                ),
            },
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
        ]

        max_attempts = 1 + max(0, int(self.settings.max_schema_retries))
        last_error = ""
        accum_usage = LLMUsage(model=self.settings.model)

        for attempt in range(1, max_attempts + 1):
            if http_call_fn is not None:
                raw_text, call_usage = await http_call_fn(operation, messages, attempt)
            else:
                raw_text, call_usage = await self.call_chat_completions(operation, messages)
            accum_usage.prompt_tokens += call_usage.prompt_tokens
            accum_usage.completion_tokens += call_usage.completion_tokens
            accum_usage.total_tokens += call_usage.total_tokens
            accum_usage.latency_ms += call_usage.latency_ms

            try:
                json_str = extract_json_object(raw_text)
                data = json.loads(json_str)
                if not isinstance(data, dict):
                    raise ValueError("top-level JSON must be an object")
                parsed = schema_cls.model_validate(data)
                if hasattr(parsed, "usage"):
                    parsed = parsed.model_copy(update={"usage": accum_usage})
                LLM_CALLS.labels(operation=operation, result="ok").inc()
                if self._on_usage is not None:
                    await self._on_usage(operation, accum_usage)
                return parsed
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = str(exc)
                category = classify_schema_retry_error(exc, raw_text)
                LLM_CALLS.labels(operation=operation, result="schema_error").inc()
                logger.warning(
                    "llm.schema_retry",
                    operation=operation,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    category=category,
                    error=last_error[:300],
                )
                if self.on_schema_retry is not None:
                    try:
                        await self.on_schema_retry(
                            {
                                "operation": operation,
                                "attempt": attempt,
                                "max_attempts": max_attempts,
                                "category": category,
                                "error": last_error[:300],
                                "raw_chars": len(raw_text or ""),
                                "model": self.settings.model,
                            }
                        )
                    except Exception:
                        pass
                if attempt < max_attempts:
                    messages.append({"role": "assistant", "content": raw_text[:2000]})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your previous response failed JSON/schema validation:\n{last_error[:800]}\n"
                                "Return ONLY a corrected valid JSON object matching the schema."
                            ),
                        }
                    )

        raise BrainSchemaError(
            f"Operation '{operation}' failed schema validation after {max_attempts} attempt(s): {last_error}"
        )

    async def call_chat_completions(self, operation: str, messages: list[dict[str, str]]) -> tuple[str, LLMUsage]:
        """Execute a single OpenAI-compatible /chat/completions HTTP request (invoked via LLMGateway)."""
        return await self._call_api(operation, messages)

    async def _call_api(self, operation: str, messages: list[dict[str, str]]) -> tuple[str, LLMUsage]:
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            # Ask vLLM/OpenAI-compatible backends for JSON object when supported
            "response_format": {"type": "json_object"},
        }

        t0 = time.perf_counter()
        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    url, headers=self._headers(), json=payload, timeout=self.settings.timeout_seconds
                )
            else:
                async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                    resp = await client.post(url, headers=self._headers(), json=payload)

            # Some OpenAI-compatible servers reject response_format; retry once without it on HTTP 400/422
            if resp.status_code in (400, 422):
                payload.pop("response_format", None)
                if self._http_client is not None:
                    resp = await self._http_client.post(
                        url, headers=self._headers(), json=payload, timeout=self.settings.timeout_seconds
                    )
                else:
                    async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                        resp = await client.post(url, headers=self._headers(), json=payload)

            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            LLM_CALLS.labels(operation=operation, result="http_error").inc()
            raise BrainError(f"LLM HTTP request failed for '{operation}': {exc}") from exc

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        choices = body.get("choices") or []
        if not choices:
            raise BrainError(f"LLM returned empty choices for '{operation}'")
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        raw_usage = body.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=int(raw_usage.get("completion_tokens", 0)),
            total_tokens=int(raw_usage.get("total_tokens", 0)),
            latency_ms=latency_ms,
            model=str(body.get("model") or self.settings.model),
        )
        if usage.total_tokens > 0:
            LLM_TOKENS.inc(usage.total_tokens)
        return content, usage
