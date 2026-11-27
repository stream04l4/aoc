"""External Isolated SkillRuntime (V4.0 M1).

Strictly lives OUTSIDE the thin human-like kernel (`app/core/`, `app/drives/`, `app/affect/`).
Executes integrity-verified VALIDATED Skill packages as first-class callable tools for:
  - Skill-first Planning (direct execution bypassing Codex code generation)
  - MCP ToolProvider bridge (`skill:<name>`)
  - Future external CLI Agents

Hard safety guarantees:
  1. Only `VALIDATED` skills with intact SHA-256 checksums may run (tampered artifacts auto-deprecated).
  2. Input & Output JSON Schema validation before and after execution.
  3. Governance L0–L3 check on every call (L3 hard-denied, L2 requires explicit `approved_l2=True`).
  4. Runs inside a fresh isolated temp directory via `python -I` with sanitized env, strict timeout,
     and output byte cap. Subprocess crashes or timeouts NEVER crash AutonomousLoop.
  5. Every invocation is persisted in `skill_invocations` with deterministic cross-task reuse accounting.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import sanitize_env
from app.cognition.interfaces import ActionPlan
from app.core.enums import DriveType, PermissionLevel
from app.governance.interfaces import GovernanceInterface
from app.observability.logging import get_logger
from app.self_model.interfaces import SkillStatus
from app.self_model.skills import SkillRegistryManager
from app.storage.repository import StateRepository
from app.tools.mcp_manager import _validate_json_schema

logger = get_logger("aoc.skills.runtime")

_RUNNER_SCRIPT = r"""
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path

tmp_dir = Path(os.environ["AOC_SKILL_WORKDIR"])
sys.path.insert(0, str(tmp_dir))

art_path = Path(os.environ["AOC_SKILL_ARTIFACT"])
input_path = Path(os.environ["AOC_SKILL_INPUT_FILE"])
output_path = Path(os.environ["AOC_SKILL_OUTPUT_FILE"])

raw_in = json.loads(input_path.read_text(encoding="utf-8")) if input_path.exists() else {}

spec = importlib.util.spec_from_file_location("_aoc_skill_mod", str(art_path))
if spec is None or spec.loader is None:
    raise RuntimeError(f"Failed to load skill module from {art_path}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fn = None
for cand in ("execute", "run", "main"):
    attr = getattr(mod, cand, None)
    if callable(attr):
        fn = attr
        break

if fn is None:
    # Fallback: single public function defined in the module
    public_fns = [
        v for k, v in vars(mod).items()
        if callable(v) and not k.startswith("_") and getattr(v, "__module__", "") == mod.__name__
    ]
    if len(public_fns) == 1:
        fn = public_fns[0]

if fn is None:
    result = {"status": "executed_module", "module": art_path.name}
else:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if not params:
        result = fn()
    elif len(params) == 1 and params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        pname = params[0].name
        if isinstance(raw_in, dict) and pname in raw_in and len(raw_in) == 1:
            result = fn(raw_in[pname])
        else:
            result = fn(raw_in)
    elif isinstance(raw_in, dict):
        result = fn(**raw_in)
    else:
        result = fn(raw_in)

output_path.write_text(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str), encoding="utf-8")
"""


def _parse_uuid_or_none(val: Any) -> uuid.UUID | None:
    if not val:
        return None
    try:
        return uuid.UUID(str(val))
    except (ValueError, TypeError):
        return None


def _digest_payload(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_utc(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"url", "target_url", "uri", "link", "href"}),
    frozenset({"text", "content", "body", "raw_text", "document", "markdown"}),
    frozenset({"items", "data", "records", "rows", "entries", "list"}),
    frozenset({"query", "q", "search_query", "keyword", "keywords", "topic"}),
    frozenset({"path", "file_path", "filepath", "target_path"}),
    frozenset({"payload", "input", "params", "arguments", "config"}),
)


def align_input_args_to_schema(
    raw_args: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Deterministic 1-shot input argument repair (unwrap/wrap, aliases, defaults, safe scalar coercions)."""
    args = dict(raw_args or {})
    if not isinstance(input_schema, dict) or not input_schema:
        return args, []

    props = input_schema.get("properties") if isinstance(input_schema.get("properties"), dict) else {}
    required = [str(r) for r in (input_schema.get("required") or []) if r]
    fixes: list[str] = []

    # Step A: Single wrapper unwrap (e.g. {"payload": {"target_url": "..."}})
    if len(args) == 1:
        only_key = next(iter(args.keys()))
        only_val = args[only_key]
        if (
            only_key in {"payload", "input", "params", "arguments", "data"}
            and only_key not in props
            and isinstance(only_val, dict)
            and any(ik in props for ik in only_val.keys())
        ):
            args = dict(only_val)
            fixes.append(f"unwrap:{only_key}")

    # Step B: Single required object wrapper (e.g. caller sent flat dict, schema wants {"payload": {...}})
    if len(required) == 1 and args:
        rk = required[0]
        r_spec = props.get(rk) if isinstance(props.get(rk), dict) else {}
        if r_spec.get("type") == "object" and rk not in args and not any(k in props for k in args.keys()):
            args = {rk: dict(args)}
            fixes.append(f"wrap:{rk}")

    # Step C: Alias key remap (e.g. url -> target_url, text -> content)
    for prop_name in list(props.keys()):
        if prop_name in args:
            continue
        matched_group = next((grp for grp in _ALIAS_GROUPS if prop_name.lower() in grp), None)
        if matched_group is None:
            continue
        for k in list(args.keys()):
            if k not in props and k.lower() in matched_group:
                args[prop_name] = args.pop(k)
                fixes.append(f"alias:{k}->{prop_name}")
                break

    # Step D: Fill explicit schema defaults for omitted required fields or explicit None values
    for pk, pspec_raw in props.items():
        if not isinstance(pspec_raw, dict):
            continue
        if pk in args and args[pk] is None:
            if "default" in pspec_raw:
                args[pk] = pspec_raw["default"]
                fixes.append(f"default:{pk}")
            elif pk not in required:
                del args[pk]
                fixes.append(f"drop_none:{pk}")
        elif pk not in args and pk in required and "default" in pspec_raw:
            args[pk] = pspec_raw["default"]
            fixes.append(f"default:{pk}")

    # Step E: Conservative scalar / array coercion matching declared JSON Schema type
    for k in list(args.keys()):
        if k not in props or not isinstance(props[k], dict):
            continue
        expected = props[k].get("type")
        val = args[k]
        if val is None or not expected:
            continue
        if expected == "integer" and isinstance(val, str) and re.fullmatch(r"-?\d+", val.strip()):
            args[k] = int(val.strip())
            fixes.append(f"coerce_int:{k}")
        elif expected == "number" and isinstance(val, str):
            try:
                args[k] = float(val.strip())
                fixes.append(f"coerce_float:{k}")
            except ValueError:
                pass
        elif expected == "boolean" and isinstance(val, str):
            low = val.strip().lower()
            if low in {"true", "1", "yes"}:
                args[k] = True
                fixes.append(f"coerce_bool:{k}")
            elif low in {"false", "0", "no"}:
                args[k] = False
                fixes.append(f"coerce_bool:{k}")
        elif expected == "array" and isinstance(val, str):
            args[k] = [val]
            fixes.append(f"coerce_array:{k}")
        elif expected == "string" and isinstance(val, (int, float, bool)):
            args[k] = str(val)
            fixes.append(f"coerce_str:{k}")

    return args, fixes


class SkillExecutionResult(BaseModel):
    skill_id: str | None = None
    skill_key: str = ""
    canonical_name: str | None = None
    version: str = ""
    ok: bool = False
    denied: bool = False
    requires_approval: bool = False
    auto_healed: bool = False
    auto_heal_fixes: list[str] = Field(default_factory=list)
    refactor_queued: bool = False
    output: Any = None
    stdout_excerpt: str = ""
    error: str | None = None
    latency_ms: float = 0.0
    is_cross_task_reuse: bool = False
    estimated_saved_tokens: int = 0
    invocation_id: str | None = None
    policy_evaluation_id: str | None = None


class SkillRuntime:
    """Isolated external runtime executing VALIDATED Skill packages safely."""

    DEFAULT_CONSERVATIVE_SAVED_TOKENS = 40_000

    def __init__(
        self,
        *,
        repo: StateRepository,
        skill_registry: SkillRegistryManager,
        governance: GovernanceInterface,
        default_timeout_seconds: float = 15.0,
        max_output_bytes: int = 32768,
        auto_heal_enabled: bool = False,
        consecutive_fail_refactor_threshold: int = 2,
    ) -> None:
        self.repo = repo
        self.skill_registry = skill_registry
        self.governance = governance
        self.default_timeout_seconds = max(1.0, float(default_timeout_seconds))
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self.auto_heal_enabled = bool(auto_heal_enabled)
        self.consecutive_fail_refactor_threshold = max(1, int(consecutive_fail_refactor_threshold))

    async def _maybe_enqueue_refactor_on_consecutive_failures(
        self,
        skill: dict[str, Any],
        error_msg: str | None,
        cycle_id: uuid.UUID | None,
    ) -> bool:
        """Auto-enqueue skill_refactor_queue when a VALIDATED skill hits >= N consecutive 24h failures."""
        if not self.auto_heal_enabled or not skill.get("id"):
            return False
        try:
            sid = uuid.UUID(str(skill["id"]))
            recent = await self.repo.list_skill_invocations(skill_id=sid, limit=10)
            now_dt = datetime.now(timezone.utc)
            cutoff = now_dt - timedelta(hours=24)
            streak = 0
            for iv in recent:
                iv_dt = _ensure_utc(iv.get("created_at"))
                if iv_dt is not None and iv_dt < cutoff:
                    break
                if iv.get("passed"):
                    break
                streak += 1
            if streak < self.consecutive_fail_refactor_threshold:
                return False

            skey = str(skill["skill_key"])
            cname = str(skill.get("canonical_name") or skey)
            q_key = f"refactor:{cname}"
            reason = (
                f"{streak} consecutive skill_runtime failures in 24h: "
                f"{str(error_msg or 'execution failed')[:220]}"
            )
            payload = {
                "skill_id": str(sid),
                "skill_key": skey,
                "canonical_name": cname,
                "current_version": skill.get("version"),
                "reason": reason,
                "target_generalization": (
                    "Inspect ./.aoc_base_skill/failure_cases.json and resolve recurring "
                    "input schema / edge-case / runtime failures"
                ),
                "priority": 2,
                "consecutive_failures": streak,
                "enqueued_at": now_dt.isoformat(),
                "created_at": now_dt.isoformat(),
            }
            await self.repo.upsert_world_item(
                kind="skill_refactor_queue",
                key=q_key,
                value=payload,
                status="open",
                source="skill_runtime_auto_queue",
            )
            await self.repo.log_event(
                "skill.refactor_auto_queued",
                level="warning",
                message=f"Auto-queued skill '{cname}' for refactor after {streak} consecutive failures",
                payload={"skill_id": str(sid), "skill_key": skey, "queue_key": q_key, "streak": streak},
                cycle_id=cycle_id,
            )
            return True
        except Exception as exc:
            logger.warning("skill_runtime.auto_queue_degraded", error=str(exc))
            return False

    async def resolve_skill(
        self, skill_ref: str | uuid.UUID, *, version: str | None = None
    ) -> dict[str, Any] | None:
        as_uuid = _parse_uuid_or_none(skill_ref)
        if as_uuid is not None:
            return await self.repo.get_skill(as_uuid)
        ref = str(skill_ref or "").strip()
        if ref.startswith("skill:"):
            ref = ref[len("skill:") :].strip()
        return await self.repo.get_skill_by_name(ref, version=version, validated_only=False)

    async def execute(
        self,
        skill_ref: str | uuid.UUID,
        input_data: dict[str, Any] | None = None,
        *,
        version: str | None = None,
        invocation_source: str = "skill_first",
        task_id: uuid.UUID | str | None = None,
        cycle_id: uuid.UUID | str | None = None,
        timeout_seconds: float | None = None,
        approved_l2: bool = False,
        estimated_saved_tokens: int | None = None,
    ) -> SkillExecutionResult:
        t0 = time.perf_counter()
        args = dict(input_data or {})
        task_uuid = _parse_uuid_or_none(task_id)
        cycle_uuid = _parse_uuid_or_none(cycle_id)
        in_digest = _digest_payload(args)

        # 1. Resolve skill & enforce VALIDATED status
        skill = await self.resolve_skill(skill_ref, version=version)
        if skill is None:
            return SkillExecutionResult(
                skill_key=str(skill_ref),
                ok=False,
                denied=True,
                error=f"Skill '{skill_ref}' not found in skill_registry",
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        sid = uuid.UUID(skill["id"])
        skey = skill["skill_key"]
        cname = skill.get("canonical_name")
        sver = skill["version"]

        if skill["status"] != SkillStatus.VALIDATED.value:
            return SkillExecutionResult(
                skill_id=str(sid),
                skill_key=skey,
                canonical_name=cname,
                version=sver,
                ok=False,
                denied=True,
                error=f"Skill '{cname or skey}@{sver}' is not VALIDATED (status={skill['status']})",
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        # 2. Verify artifact SHA-256 checksum (reject & deprecate silently tampered files immediately)
        intact, reason = await self.skill_registry.verify_skill_integrity(sid)
        if not intact:
            await self.repo.update_skill_status(sid, SkillStatus.DEPRECATED.value)
            err = f"Skill artifact integrity check failed ({reason}); auto-deprecated"
            inv = await self.repo.record_skill_invocation(
                skill_id=sid,
                skill_version=sver,
                invocation_source=invocation_source,
                task_id=task_uuid,
                cycle_id=cycle_uuid,
                passed=False,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                input_digest=in_digest,
                error=err,
            )
            return SkillExecutionResult(
                skill_id=str(sid),
                skill_key=skey,
                canonical_name=cname,
                version=sver,
                ok=False,
                denied=True,
                error=err,
                invocation_id=inv["id"],
                is_cross_task_reuse=inv["is_cross_task_reuse"],
                latency_ms=inv["latency_ms"],
            )

        # 3. Validate input_schema (with deterministic 1-shot auto-heal when enabled, V5.3 M2)
        auto_healed = False
        auto_heal_fixes: list[str] = []
        in_schema = skill.get("input_schema") or {}
        if in_schema:
            in_err = _validate_json_schema(args, in_schema)
            if in_err is not None and self.auto_heal_enabled:
                healed_args, fixes = align_input_args_to_schema(args, in_schema)
                if fixes and _validate_json_schema(healed_args, in_schema) is None:
                    args = healed_args
                    in_digest = _digest_payload(args)
                    in_err = None
                    auto_healed = True
                    auto_heal_fixes = fixes
                    await self.repo.log_event(
                        "skill.auto_healed",
                        level="info",
                        message=f"Auto-healed input args for {cname or skey}@{sver}: {', '.join(fixes)}",
                        payload={"skill_id": str(sid), "skill_key": skey, "fixes": fixes},
                        cycle_id=cycle_uuid,
                    )
                else:
                    await self.repo.log_event(
                        "skill.auto_heal_failed",
                        level="warning",
                        message=f"Input schema auto-heal failed for {cname or skey}@{sver}: {in_err}",
                        payload={"skill_id": str(sid), "skill_key": skey, "error": in_err, "fixes_attempted": fixes},
                        cycle_id=cycle_uuid,
                    )
            if in_err is not None:
                err = f"Input schema validation failed: {in_err}"
                inv = await self.repo.record_skill_invocation(
                    skill_id=sid,
                    skill_version=sver,
                    invocation_source=invocation_source,
                    task_id=task_uuid,
                    cycle_id=cycle_uuid,
                    passed=False,
                    latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                    input_digest=in_digest,
                    error=err,
                )
                queued = await self._maybe_enqueue_refactor_on_consecutive_failures(skill, err, cycle_uuid)
                return SkillExecutionResult(
                    skill_id=str(sid),
                    skill_key=skey,
                    canonical_name=cname,
                    version=sver,
                    ok=False,
                    denied=True,
                    auto_healed=auto_healed,
                    auto_heal_fixes=auto_heal_fixes,
                    refactor_queued=queued,
                    error=err,
                    invocation_id=inv["id"],
                    is_cross_task_reuse=inv["is_cross_task_reuse"],
                    latency_ms=inv["latency_ms"],
                )

        # 4. Governance L0–L3 validation (ZERO bypass!)
        req_level = str(skill.get("permission_level") or PermissionLevel.L1.value).upper()
        syn_plan = ActionPlan(
            should_execute=True,
            goal_title=f"Execute skill {cname or skey}@{sver}",
            objective=f"Execute validated skill {cname or skey}@{sver} with args {json.dumps(args, ensure_ascii=False)[:400]}",
            description=str(skill.get("description") or ""),
            assigned_agent="skill_runtime",
            permission_level=req_level,
            timeout_seconds=int(timeout_seconds or self.default_timeout_seconds),
        )
        verdict = await self.governance.validate_plan(syn_plan, DriveType.REPRODUCTION.value)
        eff_level = PermissionLevel(verdict.effective_level)

        if eff_level == PermissionLevel.L3 or verdict.denied:
            eval_id = await self.repo.record_policy_evaluation(
                subject_type="skill_runtime",
                subject_id=task_uuid,
                cycle_id=cycle_uuid,
                requested_level=req_level,
                deterministic_level=verdict.deterministic_level or verdict.effective_level,
                semantic_level=verdict.semantic_level,
                effective_level=verdict.effective_level,
                verdict="denied",
                violations=verdict.violations,
                reason=verdict.reason,
            )
            return SkillExecutionResult(
                skill_id=str(sid),
                skill_key=skey,
                canonical_name=cname,
                version=sver,
                ok=False,
                denied=True,
                auto_healed=auto_healed,
                auto_heal_fixes=auto_heal_fixes,
                error=f"Governance denied skill execution ({verdict.effective_level}): {verdict.reason}",
                policy_evaluation_id=str(eval_id),
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        if verdict.requires_approval and not approved_l2:
            eval_id = await self.repo.record_policy_evaluation(
                subject_type="skill_runtime",
                subject_id=task_uuid,
                cycle_id=cycle_uuid,
                requested_level=req_level,
                deterministic_level=verdict.deterministic_level or verdict.effective_level,
                semantic_level=verdict.semantic_level,
                effective_level=verdict.effective_level,
                verdict="requires_approval",
                violations=verdict.violations,
                reason=f"Skill '{cname or skey}@{sver}' requires explicit L2 approval",
            )
            return SkillExecutionResult(
                skill_id=str(sid),
                skill_key=skey,
                canonical_name=cname,
                version=sver,
                ok=False,
                requires_approval=True,
                auto_healed=auto_healed,
                auto_heal_fixes=auto_heal_fixes,
                error=f"Skill '{cname or skey}@{sver}' requires explicit L2 approval",
                policy_evaluation_id=str(eval_id),
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        eval_id = await self.repo.record_policy_evaluation(
            subject_type="skill_runtime",
            subject_id=task_uuid,
            cycle_id=cycle_uuid,
            requested_level=req_level,
            deterministic_level=verdict.deterministic_level or verdict.effective_level,
            semantic_level=verdict.semantic_level,
            effective_level=verdict.effective_level,
            verdict="allowed",
            violations=verdict.violations,
            reason=f"Allowed skill '{cname or skey}@{sver}' at {verdict.effective_level}",
        )

        # 5. Execute in fresh isolated temp sandbox (`python -I`, sanitized env, strict timeout)
        art_path = skill.get("artifact_path")
        if not art_path or not Path(art_path).is_file():
            err = f"Skill artifact file missing: {art_path}"
            inv = await self.repo.record_skill_invocation(
                skill_id=sid,
                skill_version=sver,
                invocation_source=invocation_source,
                task_id=task_uuid,
                cycle_id=cycle_uuid,
                passed=False,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                input_digest=in_digest,
                error=err,
            )
            queued = await self._maybe_enqueue_refactor_on_consecutive_failures(skill, err, cycle_uuid)
            return SkillExecutionResult(
                skill_id=str(sid),
                skill_key=skey,
                canonical_name=cname,
                version=sver,
                ok=False,
                auto_healed=auto_healed,
                auto_heal_fixes=auto_heal_fixes,
                refactor_queued=queued,
                error=err,
                invocation_id=inv["id"],
                policy_evaluation_id=str(eval_id),
                latency_ms=inv["latency_ms"],
            )

        eff_timeout = max(0.2, float(timeout_seconds or self.default_timeout_seconds))
        ok = False
        parsed_output: Any = None
        stdout_txt = ""
        err_msg: str | None = None

        try:
            with tempfile.TemporaryDirectory(prefix="aoc-skill-run-") as tmpdir:
                tmp_path = Path(tmpdir)
                src = Path(art_path)
                dst = tmp_path / src.name
                shutil.copy2(src, dst)
                in_file = tmp_path / "_input.json"
                out_file = tmp_path / "_output.json"
                runner_file = tmp_path / "_runner.py"
                in_file.write_text(json.dumps(args, ensure_ascii=False, default=str), encoding="utf-8")
                runner_file.write_text(_RUNNER_SCRIPT, encoding="utf-8")

                env = sanitize_env(
                    {
                        "AOC_SKILL_WORKDIR": str(tmp_path),
                        "AOC_SKILL_ARTIFACT": str(dst),
                        "AOC_SKILL_INPUT_FILE": str(in_file),
                        "AOC_SKILL_OUTPUT_FILE": str(out_file),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                )
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    str(runner_file),
                    cwd=str(tmp_path),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=eff_timeout)
                    stdout_txt = out_b.decode("utf-8", errors="replace")[: self.max_output_bytes]
                    stderr_txt = err_b.decode("utf-8", errors="replace")[:4000]
                    if proc.returncode != 0:
                        err_msg = f"Skill exited with code {proc.returncode}: {stderr_txt or stdout_txt}".strip()[:800]
                    elif out_file.is_file():
                        raw_out = out_file.read_bytes()[: self.max_output_bytes].decode("utf-8", errors="replace")
                        envelope = json.loads(raw_out)
                        parsed_output = envelope.get("result")
                        ok = True
                    else:
                        parsed_output = {"stdout": stdout_txt}
                        ok = True
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                    err_msg = f"Skill execution timed out after {eff_timeout:.2f}s"
        except Exception as exc:
            logger.warning("skill_runtime.exec_exception", skill_id=str(sid), error=str(exc))
            err_msg = f"Skill runtime error: {exc}"

        # 6. Validate output_schema if declared
        out_schema = skill.get("output_schema") or {}
        if ok and out_schema:
            if not isinstance(parsed_output, dict):
                ok = False
                err_msg = f"Output schema validation failed: expected object, got {type(parsed_output).__name__}"
            else:
                out_err = _validate_json_schema(parsed_output, out_schema)
                if out_err is not None:
                    ok = False
                    err_msg = f"Output schema validation failed: {out_err}"

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        saved_est = (
            int(estimated_saved_tokens)
            if estimated_saved_tokens is not None
            else (self.DEFAULT_CONSERVATIVE_SAVED_TOKENS if (ok and invocation_source == "skill_first") else 0)
        )
        out_excerpt = json.dumps(parsed_output, ensure_ascii=False, default=str)[:2000] if parsed_output is not None else stdout_txt[:2000]

        inv = await self.repo.record_skill_invocation(
            skill_id=sid,
            skill_version=sver,
            invocation_source=invocation_source,
            task_id=task_uuid,
            cycle_id=cycle_uuid,
            passed=ok,
            latency_ms=latency_ms,
            estimated_saved_tokens=saved_est if ok else 0,
            input_digest=in_digest,
            output_excerpt=out_excerpt,
            error=err_msg,
        )
        queued = False
        if not ok:
            queued = await self._maybe_enqueue_refactor_on_consecutive_failures(skill, err_msg, cycle_uuid)
        await self.repo.record_usage(
            category="skill_runtime",
            amount=1.0,
            seconds=latency_ms / 1000.0,
            details={
                "skill_id": str(sid),
                "skill_key": skey,
                "canonical_name": cname,
                "version": sver,
                "invocation_source": invocation_source,
                "is_cross_task_reuse": inv["is_cross_task_reuse"],
                "ok": ok,
                "auto_healed": auto_healed,
                "estimated_saved_tokens": inv["estimated_saved_tokens"],
            },
            cycle_id=cycle_uuid,
            task_id=task_uuid,
        )

        return SkillExecutionResult(
            skill_id=str(sid),
            skill_key=skey,
            canonical_name=cname,
            version=sver,
            ok=ok,
            auto_healed=auto_healed,
            auto_heal_fixes=auto_heal_fixes,
            refactor_queued=queued,
            output=parsed_output,
            stdout_excerpt=stdout_txt[:2000],
            error=err_msg,
            latency_ms=latency_ms,
            is_cross_task_reuse=inv["is_cross_task_reuse"],
            estimated_saved_tokens=inv["estimated_saved_tokens"],
            invocation_id=inv["id"],
            policy_evaluation_id=str(eval_id),
        )
