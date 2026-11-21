"""Guarded SkillRegistryManager with isolated assertion testing & SHA-256 tamper detection (V2.0 P1-7).

Hard invariants (Task Book Section 5.8 & Section 7 P1-7):
  - Every newly registered skill starts strictly as `DRAFT`.
  - Skills are subject to L0–L3 Governance validation (L3 hard-denied).
  - Promotion to `VALIDATED` requires executing real assertion tests in an isolated
    temporary workspace and recording `skill_validation_results(passed=true)`.
  - Artifact SHA-256 checksum is persisted on registration and verified on every
    validation & integrity check — silent tampering invalidates the skill immediately.
  - Validation script timeouts, crashes, or non-zero exit codes NEVER crash the caller
    or AutonomousLoop; the skill stays `DRAFT` with failure audit logged.
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from app.agents.base import sanitize_env
from app.cognition.interfaces import ActionPlan
from app.core.enums import DriveType, PermissionLevel
from app.governance.interfaces import GovernanceInterface
from app.observability.logging import get_logger
from app.self_model.interfaces import SelfModelConfig, SkillRegistryInterface, SkillStatus
from app.storage.repository import StateRepository

logger = get_logger("aoc.self_model.skills")


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class SkillGovernanceError(PermissionError):
    """Raised when a skill violates L3 governance, static safety scan, or path isolation rules."""


_FORBIDDEN_SKILL_IMPORTS = frozenset(
    {
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "psycopg2",
        "ctypes",
        "socket",
        "paramiko",
        "fabric",
        "docker",
    }
)

_FORBIDDEN_SKILL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("core_source_access", re.compile(r"(/home/ubuntu/aoc/app\b|app/governance|app/core/loop)", re.IGNORECASE)),
    ("dangerous_shell_cmd", re.compile(r"\b(sudo\s+|rm\s+-rf|chmod\s+777|systemctl\b|mkfs\b|dd\s+if=)", re.IGNORECASE)),
    ("destructive_sql", re.compile(r"\b(DROP\s+TABLE|TRUNCATE\s+TABLE|ALTER\s+TABLE|DELETE\s+FROM\s+organism)", re.IGNORECASE)),
    ("dynamic_exec", re.compile(r"\b(os\.system|os\.popen|eval|exec|__import__)\s*\(")),
    ("subprocess_shell_true", re.compile(r"shell\s*=\s*True")),
)


def scan_skill_code_safety(code: str) -> list[str]:
    """Deterministic AST + regex static safety scanner for candidate Skill artifacts (V3.0 M4)."""
    violations: list[str] = []
    text = code or ""
    for tag, pat in _FORBIDDEN_SKILL_PATTERNS:
        if pat.search(text):
            violations.append(f"forbidden_pattern:{tag}")

    # AST inspection for forbidden module imports & dangerous calls (when valid Python)
    try:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in _FORBIDDEN_SKILL_IMPORTS:
                        violations.append(f"forbidden_import:{root}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    if root in _FORBIDDEN_SKILL_IMPORTS:
                        violations.append(f"forbidden_import:{root}")
    except SyntaxError:
        violations.append("syntax_error:invalid_python")

    return list(dict.fromkeys(violations))


class SkillRegistryManager(SkillRegistryInterface):
    def __init__(
        self,
        *,
        repo: StateRepository,
        governance: GovernanceInterface | None = None,
        config: SelfModelConfig | dict[str, Any] | None = None,
        self_model: Any = None,
    ) -> None:
        self.repo = repo
        self.governance = governance
        if isinstance(config, SelfModelConfig):
            self.cfg = config
        elif isinstance(config, dict):
            self.cfg = SelfModelConfig.model_validate(config)
        else:
            self.cfg = SelfModelConfig()
        self.self_model = self_model
        self.storage_dir = Path(self.cfg.skill_storage_dir).resolve()

    def _safe_artifact_path(self, raw_path: str) -> Path:
        p = Path(raw_path).resolve()
        # Allow artifacts inside configured skill_storage_dir OR explicit existing path if not sensitive
        if ".." in Path(raw_path).parts:
            raise SkillGovernanceError("Path traversal ('..') is forbidden in skill artifact_path")
        return p

    async def register_skill(
        self,
        *,
        skill_key: str,
        version: str = "1.0.0",
        capability_key: str,
        description: str = "",
        applicable_scenarios: list[str] | None = None,
        steps: list[str] | None = None,
        artifact_path: str | None = None,
        artifact_code: str | None = None,
        artifact_filename: str = "skill.py",
        test_spec: dict[str, Any] | None = None,
        permission_level: str = PermissionLevel.L1.value,
        source_task_id: uuid.UUID | str | None = None,
        source_execution_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        """Register a skill strictly in `DRAFT` status after L0–L3 governance checks."""
        skey = str(skill_key).strip()
        ver = str(version).strip()
        ckey = str(capability_key).strip()
        if not skey or not ver or not ckey:
            raise ValueError("skill_key, version, and capability_key are required")

        # V3.0 M4: Deterministic static safety scan on Python skill code before persisting
        if artifact_code is not None and str(artifact_filename).endswith(".py"):
            code_violations = scan_skill_code_safety(artifact_code)
            if code_violations:
                raise SkillGovernanceError(
                    f"Skill '{skey}@{ver}' rejected by static safety scanner: {', '.join(code_violations)}"
                )

        # Governance check (L0-L3): skills can NEVER bypass policy
        if self.governance is not None:
            check_text = (
                f"Skill {skey}@{ver} ({ckey}): {description}\n"
                f"Steps: {steps or []}\n"
                f"Code: {(artifact_code or '')[:1000]}"
            )
            plan = ActionPlan(
                should_execute=True,
                goal_title=f"Register skill {skey}@{ver}",
                objective=check_text,
                description=description,
                permission_level=permission_level,
            )
            verdict = await self.governance.validate_plan(plan, DriveType.REPRODUCTION.value)
            if verdict.denied or verdict.effective_level == PermissionLevel.L3.value:
                raise SkillGovernanceError(
                    f"Skill '{skey}@{ver}' denied by governance ({verdict.effective_level}): {verdict.reason}"
                )
            effective_perm = verdict.effective_level
        else:
            effective_perm = permission_level

        resolved_path: Path | None = None
        checksum: str | None = None

        if artifact_code is not None:
            safe_dir = (self.storage_dir / skey.replace("/", "_") / ver.replace("/", "_")).resolve()
            if self.storage_dir not in safe_dir.parents and safe_dir != self.storage_dir:
                raise SkillGovernanceError("Skill directory escapes skill_storage_dir")
            safe_dir.mkdir(parents=True, exist_ok=True)
            fname = Path(artifact_filename).name
            resolved_path = (safe_dir / fname).resolve()
            resolved_path.write_text(artifact_code, encoding="utf-8")
            checksum = compute_file_sha256(resolved_path)
        elif artifact_path is not None:
            resolved_path = self._safe_artifact_path(artifact_path)
            if not resolved_path.exists() or not resolved_path.is_file():
                raise FileNotFoundError(f"Skill artifact file does not exist: {resolved_path}")
            checksum = compute_file_sha256(resolved_path)

        task_uuid = uuid.UUID(str(source_task_id)) if source_task_id else None
        exec_uuid = uuid.UUID(str(source_execution_id)) if source_execution_id else None

        # Always starts strictly as DRAFT; must pass validate_skill() to become VALIDATED
        row = await self.repo.create_or_update_skill(
            skill_key=skey,
            version=ver,
            capability_key=ckey,
            description=description,
            applicable_scenarios=applicable_scenarios or [],
            steps=steps or [],
            artifact_path=str(resolved_path) if resolved_path else None,
            artifact_checksum=checksum,
            test_spec=test_spec or {},
            permission_level=effective_perm,
            status=SkillStatus.DRAFT.value,
            source_task_id=task_uuid,
            source_execution_id=exec_uuid,
        )
        return row

    async def verify_skill_integrity(
        self, skill_id: uuid.UUID | str, *, session: Any = None
    ) -> tuple[bool, str]:
        """Verify persisted SHA-256 checksum matches the artifact on disk (detects silent tampering)."""
        sid = uuid.UUID(str(skill_id))
        skill = await self.repo.get_skill(sid, session=session)
        if skill is None:
            return False, "skill_not_found"
        art_path = skill.get("artifact_path")
        expected = skill.get("artifact_checksum")
        if not art_path:
            # Metadata-only skill without artifact file
            return (True, "no_artifact") if not expected else (False, "missing_artifact_path")
        p = Path(art_path)
        if not p.exists() or not p.is_file():
            return False, f"artifact_missing:{art_path}"
        if not expected:
            return False, "missing_expected_checksum"
        actual = compute_file_sha256(p)
        if actual != expected:
            return False, f"checksum_mismatch:expected={expected[:12]}..actual={actual[:12]}.."
        return True, "ok"

    async def validate_skill(
        self,
        skill_id: uuid.UUID | str,
        *,
        timeout_seconds: float | None = None,
        approved_l2_promotion: bool = False,
    ) -> dict[str, Any]:
        """Execute skill's assertion test_spec in an isolated temp workspace.

        Transitions status to `VALIDATED` ONLY when:
          1. SHA-256 artifact checksum is intact, AND
          2. Executable test_spec is present and passes cleanly within timeout.
        On timeout, non-zero exit, missing test_spec, or tamper -> stays `DRAFT` (never crashes loop).
        """
        sid = uuid.UUID(str(skill_id))
        skill = await self.repo.get_skill(sid)
        if skill is None:
            raise ValueError(f"Skill {skill_id} not found")

        checks: list[dict[str, Any]] = []
        stdout_parts: list[str] = []

        # 1. Integrity check
        intact, reason = await self.verify_skill_integrity(sid)
        checks.append({"name": "artifact_checksum", "passed": intact, "detail": reason})
        if not intact:
            val_row = await self.repo.record_skill_validation(
                skill_id=sid, passed=False, checks=checks, stdout_excerpt=f"Integrity failed: {reason}"
            )
            if skill["status"] == SkillStatus.VALIDATED.value:
                await self.repo.update_skill_status(sid, SkillStatus.DEPRECATED.value)
            return {
                "skill_id": str(sid),
                "skill_key": skill["skill_key"],
                "version": skill["version"],
                "status": SkillStatus.DEPRECATED.value if skill["status"] == SkillStatus.VALIDATED.value else SkillStatus.DRAFT.value,
                "passed": False,
                "validation": val_row,
            }

        # 2. Must have executable test_spec when skill_validation_required=True
        test_spec = skill.get("test_spec") or {}
        kind = str(test_spec.get("kind") or "").strip().lower()
        if not test_spec or kind not in {"python_assertions", "command"}:
            checks.append(
                {
                    "name": "test_spec_present",
                    "passed": False,
                    "detail": "Missing executable test_spec (supported kinds: python_assertions, command)",
                }
            )
            val_row = await self.repo.record_skill_validation(
                skill_id=sid,
                passed=False,
                checks=checks,
                stdout_excerpt="Validation rejected: no executable test_spec",
            )
            return {
                "skill_id": str(sid),
                "skill_key": skill["skill_key"],
                "version": skill["version"],
                "status": SkillStatus.DRAFT.value,
                "passed": False,
                "validation": val_row,
            }

        eff_timeout = float(timeout_seconds or test_spec.get("timeout_seconds") or self.cfg.skill_test_timeout_seconds)
        passed = False
        try:
            with tempfile.TemporaryDirectory(prefix="aoc-skill-val-") as tmpdir:
                tmp_path = Path(tmpdir)
                env = sanitize_env(
                    {
                        "AOC_SKILL_WORKDIR": str(tmp_path),
                        "AOC_SKILL_KEY": skill["skill_key"],
                        "AOC_SKILL_VERSION": skill["version"],
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                )
                # Copy artifact into isolated workspace so tests never mutate the canonical artifact
                if skill.get("artifact_path"):
                    src = Path(skill["artifact_path"])
                    dst = tmp_path / src.name
                    shutil.copy2(src, dst)
                    env["AOC_SKILL_ARTIFACT"] = str(dst)

                if kind == "python_assertions":
                    test_code = str(test_spec.get("code") or test_spec.get("script") or "").strip()
                    if not test_code:
                        raise ValueError("test_spec.code is empty")
                    runner_file = tmp_path / "_test_skill.py"
                    # Allow importing sibling artifact in the isolated tmpdir while keeping `python -I`
                    # completely isolated from user site-packages, PYTHONPATH, and AOC Core source.
                    preamble = f"import sys as _aoc_sys; _aoc_sys.path.insert(0, {str(tmp_path)!r})\n"
                    runner_file.write_text(preamble + test_code, encoding="utf-8")
                    argv = [sys.executable, "-I", str(runner_file)]
                else:
                    raw_argv = test_spec.get("argv")
                    if not isinstance(raw_argv, list) or not raw_argv:
                        raise ValueError("test_spec.argv must be a non-empty list")
                    argv = [
                        sys.executable if str(a) == "{python}" else str(a).replace("{artifact}", env.get("AOC_SKILL_ARTIFACT", ""))
                        for a in raw_argv
                    ]

                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(tmp_path),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=eff_timeout)
                    out_txt = out_b.decode("utf-8", errors="replace")
                    err_txt = err_b.decode("utf-8", errors="replace")
                    stdout_parts.append(out_txt)
                    if err_txt:
                        stdout_parts.append(f"[stderr]\n{err_txt}")

                    expected_rc = int(test_spec.get("expected_exit_code", 0))
                    rc_ok = proc.returncode == expected_rc
                    checks.append(
                        {
                            "name": "exit_code",
                            "passed": rc_ok,
                            "detail": f"returncode={proc.returncode} expected={expected_rc}",
                        }
                    )
                    substrs = test_spec.get("expected_stdout_contains") or []
                    substr_ok = True
                    for needle in substrs:
                        found = str(needle) in out_txt
                        checks.append(
                            {
                                "name": f"stdout_contains:{str(needle)[:40]}",
                                "passed": found,
                                "detail": "matched" if found else f"missing substring '{needle}'",
                            }
                        )
                        if not found:
                            substr_ok = False
                    passed = rc_ok and substr_ok
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                    checks.append(
                        {
                            "name": "execution_timeout",
                            "passed": False,
                            "detail": f"Skill validation timed out after {eff_timeout:.2f}s",
                        }
                    )
                    stdout_parts.append(f"TIMEOUT after {eff_timeout:.2f}s")
                    passed = False
        except Exception as exc:
            logger.warning("skill.validation_exception", skill_id=str(sid), error=str(exc))
            checks.append({"name": "validation_runner", "passed": False, "detail": str(exc)})
            stdout_parts.append(f"EXCEPTION: {exc}")
            passed = False

        # Re-verify canonical artifact wasn't altered during validation
        intact_post, post_reason = await self.verify_skill_integrity(sid)
        checks.append({"name": "post_checksum", "passed": intact_post, "detail": post_reason})
        passed = bool(passed and intact_post)

        excerpt = "\n".join(p for p in stdout_parts if p).strip()[:4000]
        val_row = await self.repo.record_skill_validation(
            skill_id=sid, passed=passed, checks=checks, stdout_excerpt=excerpt
        )

        # V3.0 M4 Policy (Joel Decision #1):
        #   - L0 / L1 skills passing static scan + isolated sandbox assertions + SHA-256 auto-promote to VALIDATED
        #   - L2 / L3 skills stay DRAFT until explicitly approved (approved_l2_promotion=True for L2; L3 never)
        perm_lvl = str(skill.get("permission_level") or PermissionLevel.L1.value).upper()
        awaiting_approval = False
        if passed:
            if perm_lvl in {PermissionLevel.L0.value, PermissionLevel.L1.value}:
                new_status = SkillStatus.VALIDATED.value
            elif perm_lvl == PermissionLevel.L2.value and approved_l2_promotion:
                new_status = SkillStatus.VALIDATED.value
            else:
                new_status = SkillStatus.DRAFT.value
                awaiting_approval = True
        else:
            new_status = SkillStatus.DRAFT.value
        await self.repo.update_skill_status(sid, new_status)

        self_model_update: dict[str, Any] | None = None
        if new_status == SkillStatus.VALIDATED.value and self.self_model is not None:
            try:
                self_model_update = await self.self_model.on_skill_validated(
                    capability_key=skill["capability_key"],
                    skill_id=sid,
                    trigger_execution_id=skill.get("source_execution_id"),
                )
            except Exception as exc:
                logger.warning("skill.self_model_notify_error", skill_id=str(sid), error=str(exc))

        return {
            "skill_id": str(sid),
            "skill_key": skill["skill_key"],
            "version": skill["version"],
            "capability_key": skill["capability_key"],
            "status": new_status,
            "passed": passed,
            "awaiting_human_approval": awaiting_approval,
            "validation": val_row,
            "self_model_version": (self_model_update or {}).get("version"),
        }

    async def revoke_skill(self, skill_id: uuid.UUID | str, reason: str = "revoked") -> dict[str, Any]:
        """Deprecate/revoke a skill cleanly so it is immediately excluded from reuse."""
        sid = uuid.UUID(str(skill_id))
        sk = await self.repo.get_skill(sid)
        if sk is None:
            raise ValueError(f"Skill {skill_id} not found")
        await self.repo.update_skill_status(sid, SkillStatus.DEPRECATED.value)
        await self.repo.log_event(
            "skill.revoked",
            message=f"Revoked skill {sk['skill_key']}@{sk['version']}: {reason}",
            payload={"skill_id": str(sid), "skill_key": sk["skill_key"], "version": sk["version"], "reason": reason},
        )
        return {"skill_id": str(sid), "skill_key": sk["skill_key"], "status": SkillStatus.DEPRECATED.value, "reason": reason}

    async def list_reusable_skills_for_context(self, limit: int = 8) -> list[dict[str, Any]]:
        """Return integrity-verified VALIDATED skills for injection into CognitiveContext."""
        rows = await self.list_skills_with_integrity(status=SkillStatus.VALIDATED.value, limit=max(limit * 2, 16))
        reusable: list[dict[str, Any]] = []
        for sk in rows:
            if not sk.get("integrity_ok"):
                continue
            reusable.append(
                {
                    "skill_key": sk["skill_key"],
                    "version": sk["version"],
                    "capability_key": sk["capability_key"],
                    "description": str(sk.get("description") or "")[:240],
                    "artifact_path": sk.get("artifact_path"),
                    "steps": list(sk.get("steps") or [])[:5],
                    "permission_level": sk.get("permission_level", "L1"),
                }
            )
            if len(reusable) >= limit:
                break
        return reusable

    async def crystallize_from_workspace(
        self,
        *,
        workspace: str,
        skill_key: str,
        capability_key: str,
        description: str,
        permission_level: str = PermissionLevel.L1.value,
        source_task_id: uuid.UUID | str | None = None,
        source_execution_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any] | None:
        """Auto-extract a candidate Skill + isolated assertion test from a verified task workspace (V3.0 M4).

        Convention: external Agent writes `skill.py` (or a single non-test `*.py` file) AND
        `test_skill.py` (or `test_*.py`) inside its isolated task workspace.
        Runs static safety scan -> registers DRAFT -> executes isolated sandbox validation ->
        L0/L1 auto-promotes to VALIDATED without human approval.
        """
        ws = Path(workspace)
        if not ws.exists() or not ws.is_dir():
            return None

        # Locate artifact script and matching assertion test inside workspace
        art_file = ws / "skill.py"
        if not art_file.is_file():
            py_candidates = [
                p for p in sorted(ws.glob("*.py"))
                if not p.name.startswith("test_") and not p.name.startswith("_") and not p.name.startswith(".aoc_")
            ]
            art_file = py_candidates[0] if len(py_candidates) == 1 else None  # type: ignore[assignment]

        test_file = ws / "test_skill.py"
        if not test_file.is_file():
            test_candidates = sorted(ws.glob("test_*.py"))
            test_file = test_candidates[0] if test_candidates else None  # type: ignore[assignment]

        if art_file is None or test_file is None:
            return None

        art_code = art_file.read_text(encoding="utf-8", errors="replace")
        test_code = test_file.read_text(encoding="utf-8", errors="replace")
        if not art_code.strip() or not test_code.strip():
            return None

        # Static safety scan on BOTH artifact and test code
        test_violations = scan_skill_code_safety(test_code)
        if test_violations:
            logger.warning("skill.crystallize_test_unsafe", skill_key=skill_key, violations=test_violations)
            return {
                "crystallized": False,
                "rejected_unsafe": True,
                "violations": test_violations,
            }

        slug = re.sub(r"[^a-z0-9_]+", "_", skill_key.strip().lower()).strip("_")[:64] or "skill_artifact"
        try:
            reg_row = await self.register_skill(
                skill_key=slug,
                version="1.0.0",
                capability_key=capability_key[:120] or "coding",
                description=description[:500],
                artifact_code=art_code,
                artifact_filename=art_file.name,
                test_spec={"kind": "python_assertions", "code": test_code, "expected_exit_code": 0},
                permission_level=permission_level,
                source_task_id=source_task_id,
                source_execution_id=source_execution_id,
            )
            val_res = await self.validate_skill(reg_row["id"])
            return {
                "crystallized": True,
                "skill_id": reg_row["id"],
                "skill_key": slug,
                "status": val_res["status"],
                "passed": val_res["passed"],
                "awaiting_human_approval": val_res.get("awaiting_human_approval", False),
            }
        except SkillGovernanceError as exc:
            logger.warning("skill.crystallize_governance_blocked", skill_key=slug, error=str(exc))
            return {"crystallized": False, "rejected_unsafe": True, "error": str(exc)}

    async def count_validated_skills(
        self,
        *,
        capability_key: str | None = None,
        verify_integrity: bool = True,
        session: Any = None,
    ) -> int:
        skills = await self.repo.list_skills(
            status=SkillStatus.VALIDATED.value,
            capability_key=capability_key,
            limit=500,
            session=session,
        )
        if not verify_integrity:
            return len(skills)
        count = 0
        for sk in skills:
            ok, _ = await self.verify_skill_integrity(sk["id"], session=session)
            if ok:
                count += 1
        return count

    async def list_skills_with_integrity(
        self, *, status: str | None = None, capability_key: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        skills = await self.repo.list_skills(status=status, capability_key=capability_key, limit=limit)
        out: list[dict[str, Any]] = []
        for sk in skills:
            ok, reason = await self.verify_skill_integrity(sk["id"])
            out.append({**sk, "integrity_ok": ok, "integrity_detail": reason})
        return out

    async def get_skill_detail(self, skill_id: uuid.UUID | str) -> dict[str, Any] | None:
        sid = uuid.UUID(str(skill_id))
        sk = await self.repo.get_skill(sid)
        if sk is None:
            return None
        ok, reason = await self.verify_skill_integrity(sid)
        validations = await self.repo.list_skill_validations(sid, limit=20)
        return {
            **sk,
            "integrity_ok": ok,
            "integrity_detail": reason,
            "validations": validations,
        }
