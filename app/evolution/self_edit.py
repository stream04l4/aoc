"""Autonomous Self-Editing & Self-Approval Manager (V5.4).

Supports dual-channel autonomous self-improvement:
  1. Channel 1 (Meta-Reflection L2): structured YAML/replacement patches auto-apply in an
     isolated worktree; broader improvements enqueue cleanly to `self_edit_queue`.
  2. Channel 2 (AutonomousLoop + Codex Runner): executes coding & unit-test updates inside an
     isolated detached git worktree under `<workspace_root>/self-edit/<proposal_key>`.

Every self-edit is validated deterministically before self-approval:
  - Hard-lock & budget caps (policy_mode='wide_open' locks ONLY .env/secrets, auth.py, lifecycle.py)
  - Four-layer `ConstitutionGuard` (paths/hashes, AST purity & syntax, YAML bounds, drive invariants)
  - Targeted `pytest` suite executed cleanly inside the isolated worktree
  - Optional `Brain.review_self_edit()` diff review gate

Approved commits advance ONLY the isolated branch `evo/auto` (default) with a rollback tag
`evo-rollback-<proposal_key>` — NEVER mutating `main` or restarting Docker automatically.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.evolution.constitution_guard import (
    CONSTITUTIONAL_FILES,
    ConstitutionGuard,
    EVOLVABLE_ALLOWLIST,
    is_hard_locked_path,
)
from app.evolution.interfaces import ProposalStage
from app.evolution.l2_bridge import L2EvolutionBridge
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository

logger = get_logger("aoc.evolution.self_edit")

_HARNESS_IGNORE_FILENAMES: frozenset[str] = frozenset(
    {
        ".aoc_last_message.txt",
        ".aoc_self_edit_summary.json",
        ".ds_store",
    }
)
_HARNESS_IGNORE_PARTS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".aoc_base_skill",
    }
)
_PREFIX_TEST_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("app/skills/", ("tests/unit/test_v5_3_m1_skill_lifecycle.py", "tests/unit/test_v5_3_m2_matcher_auto_heal.py")),
    ("app/self_model/", ("tests/unit/test_v5_3_m1_skill_lifecycle.py",)),
    ("app/cognition/", ("tests/unit/test_v5_3_m3_telemetry_attribution.py", "tests/unit/test_v5_2_m1_brain_research.py")),
    ("app/drives/", ("tests/unit/test_v5_m2_constitution_entropy_funnel.py", "tests/unit/test_v5_3_m3_telemetry_attribution.py")),
    ("app/evolution/", ("tests/unit/test_v5_3_m4_l2_bridge_and_obs.py", "tests/unit/test_v5_4_autonomous_self_edit.py")),
    ("app/orchestration/", ("tests/unit/test_v5_2_m3_skill_refactor_and_reuse.py",)),
    ("app/perception/", ("tests/unit/test_v5_2_m2_interest_portfolio.py",)),
    ("app/notifications/", ("tests/unit/test_v5_telegram_option_cards.py",)),
    ("app/api/", ("tests/unit/test_v5_3_m4_l2_bridge_and_obs.py",)),
    ("config/", ("tests/unit/test_v5_m2_constitution_entropy_funnel.py", "tests/unit/test_v5_3_m4_l2_bridge_and_obs.py")),
)


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _parse_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if not raw or not isinstance(raw, str):
        return None
    try:
        return _ensure_utc(datetime.fromisoformat(raw.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _strip_rel_prefix(raw_path: str) -> str:
    text = str(raw_path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _should_ignore_rel_path(rel_path: str) -> bool:
    norm = _strip_rel_prefix(rel_path)
    if not norm:
        return True
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return True
    low_last = parts[-1].lower()
    if low_last in _HARNESS_IGNORE_FILENAMES or low_last.endswith(".pyc") or parts[-1].startswith(".aoc_"):
        return True
    return any(p in _HARNESS_IGNORE_PARTS for p in parts)


def _needs_container_restart(changed_files: list[str]) -> bool:
    for rel in changed_files:
        norm = rel.strip().lstrip("./")
        if (
            norm.endswith(".py")
            or norm.startswith("config/")
            or norm in {"pyproject.toml", "uv.lock", "Dockerfile", "docker-compose.yml"}
            or norm.startswith("frontend/")
        ):
            return True
    return False


class SelfEditManager:
    """Orchestrates isolated worktree self-editing, verification, self-approval, and evo/auto landing."""

    def __init__(
        self,
        *,
        evo_repo: EvolutionRepository,
        state_repo: StateRepository,
        proposal_manager: ProposalManager,
        constitution_guard: ConstitutionGuard,
        brain: Any = None,
        repo_root: str | Path,
        workspace_root: str | Path = "/home/ubuntu/aoc-workspaces",
        enabled: bool = False,
        auto_approve_enabled: bool = False,
        policy_mode: str = "strict",
        target_branch: str = "evo/auto",
        cooldown_minutes: int = 60,
        max_commits_per_day: int = 6,
        max_changed_files: int = 8,
        max_loc_delta: int = 600,
        pytest_timeout_seconds: int = 180,
        llm_review_enabled: bool = True,
        pytest_runner_fn: Callable[[Path], Awaitable[tuple[bool, str]] | tuple[bool, str]] | None = None,
    ) -> None:
        self.evo_repo = evo_repo
        self.state_repo = state_repo
        self.proposal_manager = proposal_manager
        self.constitution_guard = constitution_guard
        self.brain = brain
        self.repo_root = Path(repo_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.enabled = bool(enabled)
        self.auto_approve_enabled = bool(auto_approve_enabled)
        self.policy_mode = "wide_open" if str(policy_mode or "").strip().lower() == "wide_open" else "strict"
        self.target_branch = str(target_branch or "evo/auto").strip() or "evo/auto"
        self.cooldown_minutes = max(0, int(cooldown_minutes))
        self.max_commits_per_day = max(1, int(max_commits_per_day))
        self.max_changed_files = max(1, int(max_changed_files))
        self.max_loc_delta = max(20, int(max_loc_delta))
        self.pytest_timeout_seconds = max(15, int(pytest_timeout_seconds))
        self.llm_review_enabled = bool(llm_review_enabled)
        self.pytest_runner_fn = pytest_runner_fn

    def _worktrees_base_dir(self) -> Path:
        base = (self.workspace_root / "self-edit").resolve()
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _git(
        self,
        cwd: Path,
        *args: str,
        check: bool = False,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        res = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and res.returncode != 0:
            err = (res.stderr or res.stdout or f"git exited {res.returncode}").strip()
            raise RuntimeError(f"git {' '.join(args[:3])} failed: {err}")
        return res

    async def get_state(self) -> dict[str, Any]:
        rows = await self.state_repo.list_world_items(kind="self_edit_state")
        row = next((r for r in rows if r.get("key") == "primary"), None)
        if row is None or not isinstance(row.get("value"), dict):
            return {}
        return dict(row["value"])

    async def _update_state(self, updates: dict[str, Any]) -> dict[str, Any]:
        cur = await self.get_state()
        merged = {**cur, **updates, "updated_at": utcnow().isoformat()}
        await self.state_repo.upsert_world_item(
            kind="self_edit_state",
            key="primary",
            value=merged,
            status="active",
            source="self_edit_manager",
        )
        return merged

    async def enqueue_self_edit(
        self,
        *,
        title: str,
        target_files: list[str] | None = None,
        problem_summary: str = "",
        desired_change: str = "",
        test_plan: str = "",
        rollback_plan: str = "",
        source: str = "meta_reflection",
        priority: int = 2,
        queue_key: str | None = None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()[:160]
        clean_targets = [
            _strip_rel_prefix(str(p))
            for p in (target_files or [])
            if str(p).strip()
        ][:8]
        q_key = (queue_key or f"self_edit:{uuid.uuid4().hex[:10]}").strip()[:120]
        now_iso = utcnow().isoformat()
        payload = {
            "queue_key": q_key,
            "title": clean_title or "Autonomous self-edit",
            "target_files": clean_targets,
            "problem_summary": str(problem_summary or "")[:500],
            "desired_change": str(desired_change or "")[:1200],
            "test_plan": str(test_plan or "")[:400],
            "rollback_plan": str(rollback_plan or "回滚至对应 evo-rollback-* 标签或执行撤销命令")[:300],
            "priority": int(priority),
            "source": str(source or "meta_reflection")[:64],
            "enqueued_at": now_iso,
        }
        await self.state_repo.upsert_world_item(
            kind="self_edit_queue",
            key=q_key,
            value=payload,
            status="open",
            source=str(source or "meta_reflection")[:64],
        )
        await self.state_repo.log_event(
            "self_edit.enqueued",
            message=f"Queued autonomous self-edit '{payload['title']}'",
            payload=payload,
        )
        return {"enqueued": True, "queue_key": q_key, **payload}

    async def list_pending_self_edits(self, limit: int = 3) -> list[dict[str, Any]]:
        rows = await self.state_repo.list_world_items(kind="self_edit_queue", status="open")
        items: list[dict[str, Any]] = []
        for r in rows:
            val = dict(r.get("value") or {}) if isinstance(r.get("value"), dict) else {}
            if not val:
                continue
            items.append({"queue_key": r.get("key"), **val})
        items.sort(key=lambda x: (-int(x.get("priority") or 1), str(x.get("enqueued_at") or "")))
        return items[: max(0, int(limit))]

    async def mark_self_edit_consumed(
        self,
        queue_key: str,
        *,
        status: str = "completed",
        commit_sha: str | None = None,
        proposal_key: str | None = None,
    ) -> bool:
        clean_key = str(queue_key or "").strip()
        if not clean_key:
            return False
        rows = await self.state_repo.list_world_items(kind="self_edit_queue")
        row = next((r for r in rows if str(r.get("key") or "") == clean_key), None)
        val = dict(row.get("value") or {}) if (row and isinstance(row.get("value"), dict)) else {"queue_key": clean_key}
        val["consumed_at"] = utcnow().isoformat()
        if commit_sha:
            val["commit_sha"] = commit_sha
        if proposal_key:
            val["proposal_key"] = proposal_key
        await self.state_repo.upsert_world_item(
            kind="self_edit_queue",
            key=clean_key,
            value=val,
            status=status,
            source="self_edit_consumed",
        )
        return True

    def validate_target_files(self, target_files: list[str] | None) -> tuple[bool, list[str], list[str]]:
        clean: list[str] = []
        violations: list[str] = []
        const_set = set(CONSTITUTIONAL_FILES)
        for raw in target_files or []:
            text = str(raw or "").strip()
            if not text:
                continue
            if self.policy_mode == "wide_open":
                locked, reason = is_hard_locked_path(text)
                norm = _strip_rel_prefix(text)
                clean.append(norm)
                if locked:
                    violations.append(reason)
            else:
                if text.startswith("/") or "\\" in text or ".." in Path(text).parts:
                    violations.append(f"invalid_target_path:{text}")
                    continue
                norm = Path(_strip_rel_prefix(text)).as_posix()
                clean.append(norm)
                if norm in const_set:
                    violations.append(f"constitutional_file_forbidden:{norm}")
                elif norm not in EVOLVABLE_ALLOWLIST:
                    violations.append(f"outside_evolvable_allowlist:{norm}")
        return len(violations) == 0, violations, clean

    async def can_start_self_edit(self, *, now: datetime | None = None, ignore_cooldown: bool = False) -> tuple[bool, str, dict[str, Any]]:
        if not self.enabled:
            return False, "self_edit_disabled", {}
        cur_now = _ensure_utc(now or utcnow())
        state = await self.get_state()
        today_str = cur_now.strftime("%Y-%m-%d")
        commits_today = int(state.get("commits_today_count") or 0) if state.get("commits_day") == today_str else 0
        if commits_today >= self.max_commits_per_day:
            return (
                False,
                f"daily_commit_cap_reached ({commits_today}/{self.max_commits_per_day})",
                {"commits_today": commits_today, "max_commits_per_day": self.max_commits_per_day},
            )
        if not ignore_cooldown and self.cooldown_minutes > 0:
            last_dt = _parse_dt(state.get("last_commit_at"))
            if last_dt is not None:
                elapsed_min = max(0.0, (cur_now - last_dt).total_seconds() / 60.0)
                if elapsed_min < self.cooldown_minutes:
                    return (
                        False,
                        f"self_edit_cooldown_active ({elapsed_min:.1f}m < {self.cooldown_minutes}m)",
                        {"elapsed_minutes": round(elapsed_min, 2), "cooldown_minutes": self.cooldown_minutes},
                    )
        return True, "ok", {"commits_today": commits_today, "max_commits_per_day": self.max_commits_per_day}

    def _resolve_base_ref(self) -> tuple[str, str]:
        if not (self.repo_root / ".git").exists():
            raise RuntimeError(f"SelfEditManager requires a git repository at {self.repo_root}")
        main_check = self._git(self.repo_root, "rev-parse", "--verify", "refs/heads/main")
        main_ref = "refs/heads/main" if main_check.returncode == 0 else "HEAD"
        main_sha = self._git(self.repo_root, "rev-parse", main_ref, check=True).stdout.strip()

        branch_ref = f"refs/heads/{self.target_branch}"
        br_check = self._git(self.repo_root, "rev-parse", "--verify", branch_ref)
        if br_check.returncode == 0:
            br_sha = br_check.stdout.strip()
            anc = self._git(self.repo_root, "merge-base", "--is-ancestor", main_sha, br_sha)
            if anc.returncode == 0:
                return branch_ref, br_sha
        return main_ref, main_sha

    async def prepare_codex_worktree(
        self,
        *,
        title: str,
        hypothesis: str,
        target_files: list[str] | None = None,
        change_summary: str = "",
        test_targets: list[str] | None = None,
        rollback_plan: str = "",
        queue_key: str | None = None,
        trigger_type: str = "SELF_EDIT_LOOP",
    ) -> dict[str, Any]:
        """Create a Stage 0 proposal and isolated detached worktree for self-editing."""
        ok_t, violations, clean_targets = self.validate_target_files(target_files)
        if not ok_t:
            raise PermissionError(f"Self-edit target blocked by policy ({self.policy_mode}): {violations}")

        base_ref, base_commit = self._resolve_base_ref()
        parent = await self.evo_repo.ensure_primary_instance(workspace_root=str(self.workspace_root))
        prop = await self.proposal_manager.create_proposal(
            parent_instance_id=parent["id"],
            target_generation=int(parent.get("generation", 0)) + 1,
            trigger_type=trigger_type,
            trigger_reason=(title or change_summary or hypothesis or "Autonomous self-edit")[:500],
            hypothesis=(hypothesis or title or "Improve AOC reliability/capability cleanly")[:500],
            genome_delta={
                "files": clean_targets,
                "summary": str(change_summary or title)[:300],
                "policy_mode": self.policy_mode,
                "target_branch": self.target_branch,
                "base_ref": base_ref,
                "base_commit": base_commit,
                "rollback_plan": str(rollback_plan or f"使用回滚标签回退 {self.target_branch} 分支")[:300],
            },
            cooldown_hours=0.0,
        )
        pkey = prop["proposal_key"]
        wt_path = (self._worktrees_base_dir() / pkey).resolve()
        if wt_path.exists():
            self.cleanup_worktree(wt_path)
        self._git(self.repo_root, "worktree", "add", "--detach", str(wt_path), base_commit, check=True)
        return {
            "proposal_id": str(prop["id"]),
            "proposal_key": pkey,
            "worktree_path": str(wt_path),
            "base_ref": base_ref,
            "base_commit": base_commit,
            "target_branch": self.target_branch,
            "target_files": clean_targets,
            "hypothesis": (hypothesis or title)[:500],
            "change_summary": str(change_summary or title)[:400],
            "test_targets": [_strip_rel_prefix(str(t)) for t in (test_targets or []) if str(t).strip()][:8],
            "rollback_plan": str(rollback_plan or "")[:300],
            "queue_key": queue_key,
            "title": str(title or pkey)[:160],
        }

    def cleanup_worktree(self, wt_path: str | Path) -> None:
        target = Path(wt_path).resolve()
        if (self.repo_root / ".git").exists():
            try:
                self._git(self.repo_root, "worktree", "remove", "--force", str(target))
            except Exception:
                pass
        shutil.rmtree(target, ignore_errors=True)
        if (self.repo_root / ".git").exists():
            try:
                self._git(self.repo_root, "worktree", "prune")
            except Exception:
                pass

    def _collect_worktree_changes(self, wt_path: Path) -> dict[str, Any]:
        st = self._git(wt_path, "status", "--porcelain", "-uall", check=True)
        changed_files: list[str] = []
        deleted_files: list[str] = []
        untracked_files: list[str] = []
        tracked_files: list[str] = []
        file_overrides: dict[str, str] = {}

        for raw_line in st.stdout.splitlines():
            if len(raw_line) < 4:
                continue
            code = raw_line[:2]
            raw_path = raw_line[3:].strip()
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1].strip()
            raw_path = _strip_rel_prefix(raw_path.strip('"'))
            if _should_ignore_rel_path(raw_path):
                continue

            full_p = (wt_path / raw_path).resolve()
            if full_p.is_dir():
                continue

            changed_files.append(raw_path)
            if "D" in code and not full_p.exists():
                deleted_files.append(raw_path)
                tracked_files.append(raw_path)
                continue

            try:
                text = full_p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = "<binary>"
            file_overrides[raw_path] = text
            if code == "??":
                untracked_files.append(raw_path)
            else:
                tracked_files.append(raw_path)

        changed_files = sorted(set(changed_files))
        diff_parts: list[str] = []
        if tracked_files:
            dres = self._git(wt_path, "diff", "HEAD", "--", *tracked_files)
            if dres.stdout:
                diff_parts.append(dres.stdout)
        for rel in untracked_files:
            new_lines = file_overrides.get(rel, "").splitlines(keepends=True)
            diff_parts.append(
                "".join(
                    difflib.unified_diff(
                        [],
                        new_lines,
                        fromfile="/dev/null",
                        tofile=f"b/{rel}",
                        n=2,
                    )
                )
            )
        unified_diff = "\n".join(p for p in diff_parts if p).strip()

        changed_lines_count = 0
        for line in unified_diff.splitlines():
            if line.startswith(("+++", "---", "@@", "diff ", "index ")):
                continue
            if line.startswith(("+", "-")):
                changed_lines_count += 1

        return {
            "changed_files": changed_files,
            "deleted_files": deleted_files,
            "file_overrides": file_overrides,
            "unified_diff": unified_diff,
            "changed_lines_count": changed_lines_count,
        }

    def select_tests_for_changes(
        self,
        wt_path: Path,
        changed_files: list[str],
        explicit_test_targets: list[str] | None = None,
    ) -> list[str]:
        selected: list[str] = []

        def _add_if_exists(rel: str) -> None:
            norm = _strip_rel_prefix(str(rel or ""))
            if not norm or norm in selected:
                return
            if (wt_path / norm).is_file():
                selected.append(norm)

        for rel in changed_files:
            if rel.startswith("tests/") and rel.endswith(".py") and Path(rel).name.startswith("test_"):
                _add_if_exists(rel)
        for rel in explicit_test_targets or []:
            _add_if_exists(rel)
        for rel in changed_files:
            for prefix, mapped in _PREFIX_TEST_MAP:
                if rel.startswith(prefix):
                    for t in mapped:
                        _add_if_exists(t)
        if not selected:
            for fallback in (
                "tests/unit/test_v5_4_autonomous_self_edit.py",
                "tests/unit/test_v5_3_m4_l2_bridge_and_obs.py",
                "tests/unit/test_v5_m2_constitution_entropy_funnel.py",
            ):
                _add_if_exists(fallback)
        if not selected:
            unit_dir = wt_path / "tests" / "unit"
            if unit_dir.is_dir():
                for cand in sorted(unit_dir.glob("test_*.py"))[:2]:
                    _add_if_exists(cand.relative_to(wt_path).as_posix())
        return selected

    async def run_worktree_pytest(
        self,
        wt_path: Path,
        changed_files: list[str],
        explicit_test_targets: list[str] | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        if self.pytest_runner_fn is not None:
            res = self.pytest_runner_fn(wt_path)
            if hasattr(res, "__await__"):
                res = await res
            ok, msg = res
            return bool(ok), str(msg or ("pytest passed" if ok else "pytest failed")), {"custom_runner": True}

        test_files = self.select_tests_for_changes(wt_path, changed_files, explicit_test_targets)
        if not test_files:
            return False, "No runnable pytest test files found for self-edit verification", {"tests": []}

        cache_dir = Path(tempfile.mkdtemp(prefix="aoc_se_pytest_cache_"))
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(wt_path)
            env["AOC_CONFIG_DIR"] = str(wt_path / "config")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            cmd = [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                f"cache_dir={cache_dir}",
                *test_files,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(wt_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=self.pytest_timeout_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return False, f"pytest timed out after {self.pytest_timeout_seconds}s", {"tests": test_files}

            stdout_txt = out_b.decode("utf-8", errors="replace").strip()
            stderr_txt = err_b.decode("utf-8", errors="replace").strip()
            combined = (stdout_txt or stderr_txt).splitlines()
            summary_line = combined[-1] if combined else f"exit_code={proc.returncode}"
            passed = proc.returncode == 0
            detail = (stdout_txt + ("\n" + stderr_txt if stderr_txt else "")).strip()[-1000:]
            return passed, summary_line[:240], {"tests": test_files, "returncode": proc.returncode, "output": detail}
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

    async def _run_brain_review(
        self,
        *,
        proposal_key: str,
        title: str,
        hypothesis: str,
        changed_files: list[str],
        diff_preview: str,
        pytest_summary: str,
    ) -> tuple[bool, str, list[str], bool | None]:
        if not (self.llm_review_enabled and self.brain is not None and hasattr(self.brain, "review_self_edit")):
            return True, title or "已通过确定性宪法校验与单元测试", [], None
        try:
            from app.cognition.interfaces import SelfEditReviewContext

            ctx = SelfEditReviewContext(
                proposal_key=proposal_key,
                title=title,
                hypothesis=hypothesis,
                policy_mode=self.policy_mode,
                changed_files=changed_files,
                unified_diff=diff_preview[:6000],
                pytest_summary=pytest_summary,
            )
            dec = await self.brain.review_self_edit(ctx)
            approved = bool(getattr(dec, "approved", True))
            summary = str(getattr(dec, "summary_zh", "") or title)[:300]
            concerns = [str(c)[:160] for c in (getattr(dec, "concerns", None) or []) if str(c).strip()][:5]
            restart_hint = getattr(dec, "restart_required", None)
            return approved, summary, concerns, restart_hint
        except Exception as exc:
            logger.warning("self_edit.brain_review_degraded", proposal_key=proposal_key, error=str(exc))
            return True, f"{title}（确定性测试已通过，AI 复核降级跳过）"[:260], [], None

    async def review_and_finalize_worktree(
        self,
        *,
        self_edit_meta: dict[str, Any],
        execution_succeeded: bool,
        execution_summary: str = "",
        cycle_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Validate worktree diff + constitution + pytest + critic review, then self-approve onto evo/auto."""
        prop_id = str(self_edit_meta.get("proposal_id") or "").strip()
        prop_key = str(self_edit_meta.get("proposal_key") or "").strip()
        wt_path = Path(str(self_edit_meta.get("worktree_path") or "")).resolve()
        target_branch = str(self_edit_meta.get("target_branch") or self.target_branch).strip() or self.target_branch
        base_commit = str(self_edit_meta.get("base_commit") or "").strip()
        queue_key = self_edit_meta.get("queue_key")
        title = str(self_edit_meta.get("title") or self_edit_meta.get("change_summary") or prop_key).strip()
        hypothesis = str(self_edit_meta.get("hypothesis") or title).strip()
        test_targets = list(self_edit_meta.get("test_targets") or [])

        try:
            if not execution_succeeded:
                if prop_id:
                    await self.evo_repo.update_proposal(
                        prop_id,
                        stage=ProposalStage.STAGE1_FAILED.value,
                        stage1_report={"passed": False, "violations": ["codex_execution_failed"]},
                    )
                await self._record_rejection(prop_key, [], ["codex_execution_failed"], "STAGE1_FAILED", cycle_id)
                return {
                    "ok": False,
                    "committed": False,
                    "stage": ProposalStage.STAGE1_FAILED.value,
                    "proposal_key": prop_key,
                    "violations": ["codex_execution_failed"],
                }

            if not wt_path.is_dir():
                violations = ["missing_self_edit_worktree"]
                if prop_id:
                    await self.evo_repo.update_proposal(
                        prop_id,
                        stage=ProposalStage.STAGE1_FAILED.value,
                        stage1_report={"passed": False, "violations": violations},
                    )
                await self._record_rejection(prop_key, [], violations, "STAGE1_FAILED", cycle_id)
                return {"ok": False, "committed": False, "stage": "STAGE1_FAILED", "proposal_key": prop_key, "violations": violations}

            changes = self._collect_worktree_changes(wt_path)
            changed_files: list[str] = changes["changed_files"]
            unified_diff: str = changes["unified_diff"]
            changed_lines: int = int(changes["changed_lines_count"])

            pre_violations: list[str] = []
            if not changed_files:
                pre_violations.append("no_file_changes_produced")
            if len(changed_files) > self.max_changed_files:
                pre_violations.append(f"too_many_changed_files ({len(changed_files)} > {self.max_changed_files})")
            if changed_lines > self.max_loc_delta:
                pre_violations.append(f"loc_delta_exceeded ({changed_lines} > {self.max_loc_delta})")
            ok_t, t_viol, _ = self.validate_target_files(changed_files)
            if not ok_t:
                pre_violations.extend(t_viol)

            prop = await self.proposal_manager.resolve_proposal(prop_id or prop_key)
            if prop is None:
                raise LookupError(f"Self-edit proposal '{prop_id or prop_key}' not found")

            delta_upd = dict(prop.get("genome_delta") or {})
            delta_upd["files"] = changed_files
            delta_upd["file_overrides"] = {
                k: v for k, v in (changes["file_overrides"] or {}).items() if isinstance(v, str) and v != "<binary>"
            }
            delta_upd["diff_preview"] = unified_diff[:4000]
            delta_upd["changed_lines_count"] = changed_lines
            await self.evo_repo.update_proposal(prop["id"], genome_delta=delta_upd)

            if pre_violations:
                await self.evo_repo.update_proposal(
                    prop["id"],
                    stage=ProposalStage.STAGE1_FAILED.value,
                    stage1_report={"passed": False, "violations": pre_violations},
                )
                await self._record_rejection(prop["proposal_key"], changed_files, pre_violations, "STAGE1_FAILED", cycle_id)
                return {
                    "ok": False,
                    "committed": False,
                    "stage": ProposalStage.STAGE1_FAILED.value,
                    "proposal_key": prop["proposal_key"],
                    "changed_files": changed_files,
                    "violations": pre_violations,
                }

            pytest_holder: dict[str, Any] = {}

            async def _guard_pytest_runner(cand_root: Path) -> tuple[bool, str]:
                ok_p, summary_p, details_p = await self.run_worktree_pytest(cand_root, changed_files, test_targets)
                pytest_holder["summary"] = summary_p
                pytest_holder["details"] = details_p
                return ok_p, summary_p

            screened = await self.proposal_manager.screen_proposal(
                prop["id"],
                candidate_root=wt_path,
                pytest_runner_fn=_guard_pytest_runner,
                emit_outbox_notification=False,
            )
            if screened["stage"] == ProposalStage.STAGE1_FAILED.value:
                violations = list((screened.get("stage1_report") or {}).get("violations") or [])
                await self._record_rejection(prop["proposal_key"], changed_files, violations, "STAGE1_FAILED", cycle_id)
                return {
                    "ok": False,
                    "committed": False,
                    "stage": ProposalStage.STAGE1_FAILED.value,
                    "proposal_key": prop["proposal_key"],
                    "changed_files": changed_files,
                    "violations": violations,
                }

            pytest_summary = str(pytest_holder.get("summary") or "pytest passed")
            if not self.auto_approve_enabled:
                return {
                    "ok": True,
                    "committed": False,
                    "stage": ProposalStage.STAGE2_PENDING_APPROVAL.value,
                    "proposal_key": prop["proposal_key"],
                    "changed_files": changed_files,
                    "pytest_summary": pytest_summary,
                }

            review_ok, review_summary, concerns, restart_hint = await self._run_brain_review(
                proposal_key=prop["proposal_key"],
                title=title,
                hypothesis=hypothesis,
                changed_files=changed_files,
                diff_preview=unified_diff,
                pytest_summary=pytest_summary,
            )
            if not review_ok:
                await self.proposal_manager.reject_proposal(
                    prop["id"],
                    approver="aoc:self_reviewer",
                    note="; ".join(concerns) or review_summary or "Rejected by Brain self-review",
                )
                await self._record_rejection(
                    prop["proposal_key"],
                    changed_files,
                    concerns or ["brain_self_review_rejected"],
                    "STAGE2_REJECTED",
                    cycle_id,
                )
                return {
                    "ok": False,
                    "committed": False,
                    "stage": ProposalStage.STAGE2_REJECTED.value,
                    "proposal_key": prop["proposal_key"],
                    "changed_files": changed_files,
                    "concerns": concerns,
                }

            # Self-Approve & Land Commit ONLY onto target_branch (e.g. evo/auto)
            await self.proposal_manager.approve_proposal(
                prop["id"],
                approver="aoc:self_approver",
                note=review_summary or f"Verified in worktree: {pytest_summary}",
            )

            self._git(wt_path, "add", "-A", "--", *changed_files, check=True)
            commit_msg = f"evo(auto): {title[:90]} [{prop['proposal_key']}]\n\nHypothesis: {hypothesis[:240]}\nTests: {pytest_summary}"
            self._git(
                wt_path,
                "-c",
                "user.name=AOC-001",
                "-c",
                "user.email=aoc@local",
                "commit",
                "-m",
                commit_msg,
                check=True,
            )
            new_commit = self._git(wt_path, "rev-parse", "HEAD", check=True).stdout.strip()
            parent_commit = base_commit or self._git(wt_path, "rev-parse", "HEAD~1", check=True).stdout.strip()
            rollback_tag = f"evo-rollback-{prop['proposal_key']}"

            self._git(self.repo_root, "branch", "-f", target_branch, new_commit, check=True)
            self._git(self.repo_root, "tag", "-f", rollback_tag, parent_commit, check=True)

            restart_required = _needs_container_restart(changed_files) or bool(restart_hint)
            final_delta = {
                **delta_upd,
                "commit_sha": new_commit,
                "parent_commit": parent_commit,
                "rollback_tag": rollback_tag,
                "target_branch": target_branch,
                "restart_required": restart_required,
                "pytest_summary": pytest_summary,
                "review_summary": review_summary,
            }
            await self.evo_repo.update_proposal(prop["id"], genome_delta=final_delta)

            if queue_key:
                await self.mark_self_edit_consumed(
                    str(queue_key),
                    status="completed",
                    commit_sha=new_commit,
                    proposal_key=prop["proposal_key"],
                )

            now = utcnow()
            today_str = now.strftime("%Y-%m-%d")
            prev_state = await self.get_state()
            commits_today = (
                int(prev_state.get("commits_today_count") or 0) + 1
                if prev_state.get("commits_day") == today_str
                else 1
            )
            pending_commits = [
                c
                for c in list(prev_state.get("pending_commits") or [])
                if isinstance(c, dict) and c.get("proposal_key") != prop["proposal_key"]
            ]
            if restart_required:
                pending_commits.append(
                    {
                        "proposal_key": prop["proposal_key"],
                        "title": title,
                        "commit_sha": new_commit[:12],
                        "rollback_tag": rollback_tag,
                        "changed_files": changed_files,
                        "restart_required": True,
                        "committed_at": now.isoformat(),
                    }
                )
            pending_commits = pending_commits[-10:]
            pending_restart = any(bool(c.get("restart_required")) for c in pending_commits)

            await self._update_state(
                {
                    "last_proposal_key": prop["proposal_key"],
                    "last_stage": "SELF_APPROVED_COMMITTED",
                    "last_commit_sha": new_commit,
                    "last_rollback_tag": rollback_tag,
                    "last_target_branch": target_branch,
                    "last_commit_at": now.isoformat(),
                    "last_changed_files": changed_files,
                    "last_pytest_summary": pytest_summary,
                    "last_violations": [],
                    "commits_day": today_str,
                    "commits_today_count": commits_today,
                    "total_self_approved": int(prev_state.get("total_self_approved") or 0) + 1,
                    "pending_restart": pending_restart,
                    "pending_commits": pending_commits,
                }
            )

            report_payload = {
                "kind": "self_edit_report",
                "proposal_id": str(prop["id"]),
                "proposal_key": prop["proposal_key"],
                "title": title,
                "summary": (review_summary or execution_summary or title)[:400],
                "changed_files": changed_files,
                "changed_lines": changed_lines,
                "pytest_summary": pytest_summary,
                "branch": target_branch,
                "commit_sha": new_commit[:12],
                "rollback_tag": rollback_tag,
                "restart_required": restart_required,
                "pending_restart_count": len(pending_commits),
                "rebuild_command": f"cd /home/ubuntu/aoc && git merge --no-ff {target_branch} && sudo docker compose up -d --build --no-deps aoc",
            }
            await self.state_repo.enqueue_outbox_event(
                event_type=OutboxEventType.NOTIFICATION_DISPATCH,
                idempotency_key=f"notify:self_edit:{prop['proposal_key']}",
                payload=report_payload,
                aggregate_type="evolution_proposal",
                aggregate_id=prop["id"],
            )
            await self.state_repo.log_event(
                "self_edit.committed",
                message=f"Self-approved & committed {prop['proposal_key']} onto {target_branch} ({new_commit[:12]})",
                payload=report_payload,
                cycle_id=cycle_id,
            )
            return {
                "ok": True,
                "committed": True,
                "stage": "SELF_APPROVED_COMMITTED",
                "proposal_id": str(prop["id"]),
                "proposal_key": prop["proposal_key"],
                "branch": target_branch,
                "commit_sha": new_commit,
                "rollback_tag": rollback_tag,
                "changed_files": changed_files,
                "changed_lines": changed_lines,
                "restart_required": restart_required,
                "pytest_summary": pytest_summary,
                "diff_preview": unified_diff[:2000],
            }
        finally:
            if wt_path and wt_path.exists():
                self.cleanup_worktree(wt_path)

    async def _record_rejection(
        self,
        proposal_key: str,
        changed_files: list[str],
        violations: list[str],
        stage: str,
        cycle_id: uuid.UUID | None,
    ) -> None:
        prev_state = await self.get_state()
        await self._update_state(
            {
                "last_proposal_key": proposal_key,
                "last_stage": stage,
                "last_changed_files": changed_files,
                "last_violations": violations,
                "total_rejected": int(prev_state.get("total_rejected") or 0) + 1,
            }
        )
        await self.state_repo.log_event(
            "self_edit.verification_failed",
            level="warning",
            message=f"Self-edit {proposal_key} blocked at {stage}: {', '.join(violations[:3])}",
            payload={"proposal_key": proposal_key, "stage": stage, "changed_files": changed_files, "violations": violations},
            cycle_id=cycle_id,
        )

    async def apply_direct_change_and_self_approve(
        self,
        *,
        title: str,
        reason: str = "",
        expected_benefit: str = "",
        rollback_plan: str = "",
        proposed_change: dict[str, Any] | None = None,
        desired_change: str = "",
        target_files: list[str] | None = None,
        test_plan: str = "",
        cycle_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Channel 1 entry: try deterministic patch in isolated worktree, or enqueue for Codex execution."""
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "self_edit_disabled"}

        can_go, gate_reason, _ = await self.can_start_self_edit()
        pc = proposed_change if isinstance(proposed_change, dict) else {}
        pc_target = _strip_rel_prefix(str(pc.get("target_file") or ""))
        eff_targets = list(target_files or [])
        if pc_target and pc_target not in eff_targets:
            eff_targets.insert(0, pc_target)

        if not pc_target:
            q_res = await self.enqueue_self_edit(
                title=title,
                target_files=eff_targets,
                problem_summary=reason,
                desired_change=desired_change or expected_benefit or reason or title,
                test_plan=test_plan,
                rollback_plan=rollback_plan,
                source="meta_reflection",
                priority=2,
            )
            return {"ok": True, "mode": "queued_for_codex", "committed": False, **q_res}

        ok_t, viols, _ = self.validate_target_files([pc_target])
        if not ok_t:
            await self.state_repo.log_event(
                "self_edit.policy_rejected",
                level="warning",
                message=f"Blocked Meta-Reflection self-edit on locked '{pc_target}': {viols}",
                payload={"target_file": pc_target, "violations": viols},
                cycle_id=cycle_id,
            )
            return {"ok": False, "committed": False, "stage": "CONSTITUTIONAL_REJECTED", "violations": viols}

        if not can_go:
            q_res = await self.enqueue_self_edit(
                title=title,
                target_files=eff_targets,
                problem_summary=reason,
                desired_change=desired_change or str(pc.get("hypothesis") or expected_benefit or title),
                test_plan=test_plan,
                rollback_plan=rollback_plan,
                source="meta_reflection",
                priority=2,
            )
            return {"ok": True, "mode": "queued_due_to_cooldown", "committed": False, "cooldown_reason": gate_reason, **q_res}

        meta = await self.prepare_codex_worktree(
            title=title,
            hypothesis=str(pc.get("hypothesis") or expected_benefit or reason or title),
            target_files=[pc_target],
            change_summary=str(pc.get("summary") or reason or title),
            rollback_plan=rollback_plan,
            trigger_type="META_REFLECTION_SELF_EDIT",
        )
        wt_path = Path(meta["worktree_path"])
        try:
            L2EvolutionBridge._apply_change_in_worktree(wt_path, pc_target, pc)
        except Exception as exc:
            self.cleanup_worktree(wt_path)
            await self.evo_repo.update_proposal(
                meta["proposal_id"],
                stage=ProposalStage.STAGE1_FAILED.value,
                stage1_report={"passed": False, "violations": [f"direct_patch_fallback:{exc}"]},
            )
            q_res = await self.enqueue_self_edit(
                title=title,
                target_files=eff_targets,
                problem_summary=f"{reason} (direct patch fallback: {exc})",
                desired_change=desired_change or str(pc.get("hypothesis") or json.dumps(pc, ensure_ascii=False)[:600]),
                test_plan=test_plan,
                rollback_plan=rollback_plan,
                source="meta_reflection",
                priority=2,
            )
            return {"ok": True, "mode": "queued_for_codex", "committed": False, "fallback_reason": str(exc), **q_res}

        res = await self.review_and_finalize_worktree(
            self_edit_meta=meta,
            execution_succeeded=True,
            execution_summary=str(pc.get("hypothesis") or title),
            cycle_id=cycle_id,
        )
        res["mode"] = "direct_self_approved" if res.get("committed") else "direct_verification_failed"
        return res

    async def revert_commit(self, proposal_key: str, *, operator: str = "joel") -> dict[str, Any]:
        """Post-hoc revert of a self-approved commit on target_branch (never touches main)."""
        clean_key = str(proposal_key or "").strip()
        if not clean_key:
            raise ValueError("proposal_key is required")

        prop = await self.proposal_manager.resolve_proposal(clean_key)
        delta = dict((prop or {}).get("genome_delta") or {})
        commit_sha = str(delta.get("commit_sha") or "").strip()
        rollback_tag = str(delta.get("rollback_tag") or f"evo-rollback-{clean_key}").strip()
        branch = str(delta.get("target_branch") or self.target_branch).strip() or self.target_branch

        if not commit_sha:
            state = await self.get_state()
            for item in list(state.get("pending_commits") or []):
                if isinstance(item, dict) and item.get("proposal_key") == clean_key:
                    commit_sha = str(item.get("commit_sha") or "").strip()
                    rollback_tag = str(item.get("rollback_tag") or rollback_tag).strip()
                    break

        branch_ref = f"refs/heads/{branch}"
        br_res = self._git(self.repo_root, "rev-parse", "--verify", branch_ref)
        if br_res.returncode != 0:
            raise LookupError(f"Evolution branch '{branch}' does not exist")
        branch_tip = br_res.stdout.strip()

        resolved_sha = ""
        if commit_sha:
            rev = self._git(self.repo_root, "rev-parse", "--verify", commit_sha)
            if rev.returncode == 0:
                resolved_sha = rev.stdout.strip()

        revert_mode = ""
        new_tip = branch_tip
        tag_check = self._git(self.repo_root, "rev-parse", "--verify", rollback_tag)
        if resolved_sha and branch_tip == resolved_sha and tag_check.returncode == 0:
            tag_sha = tag_check.stdout.strip()
            self._git(self.repo_root, "branch", "-f", branch, tag_sha, check=True)
            revert_mode = "fast_reset_to_rollback_tag"
            new_tip = tag_sha
        else:
            if not resolved_sha:
                raise LookupError(f"Cannot locate commit for proposal '{clean_key}'")
            wt_path = (self._worktrees_base_dir() / f"revert-{clean_key}-{uuid.uuid4().hex[:6]}").resolve()
            try:
                self._git(self.repo_root, "worktree", "add", "--detach", str(wt_path), branch_tip, check=True)
                self._git(
                    wt_path,
                    "-c",
                    "user.name=AOC-001",
                    "-c",
                    "user.email=aoc@local",
                    "revert",
                    "--no-edit",
                    resolved_sha,
                    check=True,
                )
                new_tip = self._git(wt_path, "rev-parse", "HEAD", check=True).stdout.strip()
                self._git(self.repo_root, "branch", "-f", branch, new_tip, check=True)
                revert_mode = "git_revert_commit"
            finally:
                self.cleanup_worktree(wt_path)

        prev_state = await self.get_state()
        remaining = [
            c
            for c in list(prev_state.get("pending_commits") or [])
            if isinstance(c, dict) and c.get("proposal_key") != clean_key
        ]
        await self._update_state(
            {
                "last_reverted_proposal": clean_key,
                "last_stage": "REVERTED",
                "pending_commits": remaining,
                "pending_restart": any(bool(c.get("restart_required")) for c in remaining),
            }
        )
        await self.state_repo.log_event(
            "self_edit.reverted",
            level="warning",
            message=f"Reverted self-edit {clean_key} on {branch} ({revert_mode}) by {operator}",
            payload={
                "proposal_key": clean_key,
                "branch": branch,
                "reverted_commit": resolved_sha or commit_sha,
                "new_tip": new_tip,
                "revert_mode": revert_mode,
                "operator": operator,
            },
        )
        return {
            "ok": True,
            "proposal_key": clean_key,
            "branch": branch,
            "revert_mode": revert_mode,
            "reverted_commit": (resolved_sha or commit_sha)[:12],
            "new_tip": new_tip[:12],
            "pending_restart": any(bool(c.get("restart_required")) for c in remaining),
        }

    async def get_diff_summary(self, proposal_key: str) -> str:
        clean_key = str(proposal_key or "").strip()
        prop = await self.proposal_manager.resolve_proposal(clean_key)
        delta = dict((prop or {}).get("genome_delta") or {})
        diff_prev = str(delta.get("diff_preview") or "").strip()
        commit_sha = str(delta.get("commit_sha") or "").strip()
        if diff_prev:
            header = f"[{clean_key}] {delta.get('target_branch', self.target_branch)} @ {commit_sha[:12]}\n"
            return (header + diff_prev)[:3200]
        if commit_sha and (self.repo_root / ".git").exists():
            res = self._git(self.repo_root, "show", "--stat", "--patch", commit_sha)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()[:3200]
        return f"未找到 {clean_key} 的改动 Diff 记录。"
