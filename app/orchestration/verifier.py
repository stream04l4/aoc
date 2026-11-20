"""Deterministic Result Verifier (task book 7.5 & 17.7).

An external agent's own claim of success is NEVER accepted as ground truth.
Every execution goes through deterministic checks (exit status + workspace/output assertions).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import AgentExecution
from app.cognition.interfaces import VerificationSpec
from app.core.enums import ExecutionStatus


class VerificationResult(BaseModel):
    passed: bool
    checks: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""


class ResultVerifier:
    def verify(
        self,
        execution: AgentExecution,
        workspace: str,
        specs: list[VerificationSpec] | list[dict[str, Any]],
    ) -> VerificationResult:
        checks: list[dict[str, Any]] = []

        # 1. Execution status must be SUCCEEDED
        status_ok = execution.status == ExecutionStatus.SUCCEEDED.value
        checks.append(
            {
                "kind": "execution_status",
                "passed": status_ok,
                "detail": f"status={execution.status}" + (f" error={execution.error}" if execution.error else ""),
            }
        )
        if not status_ok:
            return VerificationResult(
                passed=False,
                checks=checks,
                summary=f"Execution did not succeed (status={execution.status})",
            )

        output_text = self._extract_text(execution)
        parsed_specs: list[VerificationSpec] = [
            s if isinstance(s, VerificationSpec) else VerificationSpec.model_validate(s)
            for s in (specs or [])
        ]
        # Always require at least non-empty output if no explicit checks were specified
        if not parsed_specs:
            parsed_specs = [VerificationSpec(kind="non_empty_output")]

        ws_root = Path(workspace).resolve()
        for spec in parsed_specs:
            if spec.kind == "non_empty_output":
                ok = len(output_text.strip()) > 0
                checks.append(
                    {"kind": "non_empty_output", "passed": ok, "detail": f"output_chars={len(output_text.strip())}"}
                )
            elif spec.kind == "min_output_chars":
                ok = len(output_text.strip()) >= spec.min_value
                checks.append(
                    {
                        "kind": "min_output_chars",
                        "passed": ok,
                        "detail": f"{len(output_text.strip())} >= {spec.min_value}",
                    }
                )
            elif spec.kind == "file_exists":
                target = (ws_root / spec.target).resolve() if spec.target else None
                in_ws = target is not None and (ws_root == target or ws_root in target.parents)
                exists = bool(in_ws and target is not None and os.path.isfile(target) and os.path.getsize(target) > 0)
                checks.append(
                    {
                        "kind": "file_exists",
                        "target": spec.target,
                        "passed": exists,
                        "detail": "present & non-empty" if exists else f"missing/empty or outside workspace: {spec.target}",
                    }
                )
            elif spec.kind in {"contains_keywords", "keywords"}:
                haystack = output_text.lower()
                # Also check workspace text files if target file specified
                if spec.target:
                    target = (ws_root / spec.target).resolve()
                    if ws_root in target.parents and os.path.isfile(target):
                        try:
                            haystack += "\n" + target.read_text(encoding="utf-8", errors="replace").lower()
                        except OSError:
                            pass
                missing = [kw for kw in spec.keywords if kw.lower() not in haystack]
                ok = len(missing) == 0
                checks.append(
                    {
                        "kind": "contains_keywords",
                        "passed": ok,
                        "detail": "all keywords matched" if ok else f"missing: {missing}",
                    }
                )
            else:
                # Unknown check kinds default to safe check (non_empty_output)
                ok = len(output_text.strip()) > 0
                checks.append(
                    {"kind": spec.kind, "passed": ok, "detail": f"fallback non-empty check ({len(output_text)} chars)"}
                )

        all_passed = all(c["passed"] for c in checks)
        passed_n = sum(1 for c in checks if c["passed"])
        return VerificationResult(
            passed=all_passed,
            checks=checks,
            summary=f"{passed_n}/{len(checks)} verification checks passed",
        )

    @staticmethod
    def _extract_text(execution: AgentExecution) -> str:
        parts: list[str] = []
        if isinstance(execution.result, dict):
            summary = execution.result.get("summary")
            if isinstance(summary, str) and summary.strip():
                parts.append(summary.strip())
        if execution.stdout:
            parts.append(execution.stdout.strip())
        return "\n".join(parts)
