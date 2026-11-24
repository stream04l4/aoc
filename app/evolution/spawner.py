"""Blank-slate isolated offspring spawner with resource discipline & automatic rollback culling."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from app.core.config import EvolutionSettings
from app.evolution.blank_slate import BlankSlateVerifier
from app.evolution.constitution_guard import CONSTITUTIONAL_FILES, EVOLVABLE_ALLOWLIST
from app.evolution.gc import CullingGC
from app.evolution.interfaces import (
    BlankSlateReport,
    InstanceRole,
    InstanceStatus,
    SpawnRequest,
    SpawnResult,
    build_instance_schema_name,
    validate_instance_key,
)
from app.evolution.repository import EvolutionRepository
from app.observability.logging import get_logger
from app.storage.database import Database

logger = get_logger("aoc.evolution.spawner")


def _default_free_disk_ratio(path: Path) -> float:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return float(usage.free) / float(max(1, usage.total))


class InstanceSpawner:
    """Spawns isolated blank-slate V5.0 offspring sharing PostgreSQL/Letta/Docker image cleanly."""

    def __init__(
        self,
        *,
        settings: EvolutionSettings,
        repo: EvolutionRepository,
        gc: CullingGC,
        verifier: BlankSlateVerifier | None = None,
        pg_base_db_url: str = "",
        disk_free_ratio_fn: Callable[[Path], float] | None = None,
        git_worktree_fn: Callable[[Path, Path, str, str], Awaitable[str | None] | str | None] | None = None,
        schema_provisioner_fn: Callable[[str, str, Path], Awaitable[None] | None] | None = None,
        docker_launcher_fn: Callable[[list[str]], Awaitable[None] | None] | None = None,
        health_checker_fn: Callable[[int, float], Awaitable[bool] | bool] | None = None,
        blank_slate_verifier_fn: Callable[[str, str, str | None, str], Awaitable[BlankSlateReport]] | None = None,
        require_stage2_approval: bool = False,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.gc = gc
        self.verifier = verifier or BlankSlateVerifier()
        self.pg_base_db_url = pg_base_db_url or repo.db.url
        self.repo_root = Path(settings.repo_root).resolve()
        self.instances_root = Path(settings.instances_root).resolve()
        self._disk_free_ratio_fn = disk_free_ratio_fn or _default_free_disk_ratio
        self._git_worktree_fn = git_worktree_fn or self._default_git_worktree
        self._schema_provisioner_fn = schema_provisioner_fn or self._default_provision_schema
        self._docker_launcher_fn = docker_launcher_fn or self._default_docker_launch
        self._health_checker_fn = health_checker_fn or self._default_health_check
        self._blank_slate_verifier_fn = blank_slate_verifier_fn
        self.require_stage2_approval = require_stage2_approval

    async def _maybe_await(self, res: Any) -> Any:
        if hasattr(res, "__await__"):
            return await res
        return res

    async def _allocate_port(self, requested_port: int | None) -> int:
        if requested_port is not None:
            return requested_port
        live = await self.repo.list_instances(limit=100)
        used = {int(r["api_port"]) for r in live if r.get("api_port") and r.get("status") != InstanceStatus.CULLED.value}
        for port in range(self.settings.candidate_port_start, self.settings.candidate_port_end + 1):
            if port not in used:
                return port
        raise RuntimeError("No free candidate API ports available within configured range")

    def build_docker_run_command(
        self,
        *,
        instance_key: str,
        container_name: str,
        schema_name: str,
        letta_agent_name: str,
        worktree_dir: Path,
        workspace_dir: Path,
        api_port: int,
    ) -> list[str]:
        """Construct zero-image-bloat `docker run` command reusing parent image with RO worktree overlays."""
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--network",
            self.settings.docker_network,
            "--memory",
            self.settings.candidate_memory_limit,
            "-p",
            f"127.0.0.1:{api_port}:9200",
        ]
        env_file = self.repo_root / ".env"
        if env_file.exists():
            cmd.extend(["--env-file", str(env_file)])

        emb_cache = self.repo_root / ".cache" / "embeddings"
        cmd.extend(
            [
                "-v",
                f"{worktree_dir / 'app'}:/app/app:ro",
                "-v",
                f"{worktree_dir / 'config'}:/app/config:ro",
                "-v",
                f"{worktree_dir / 'migrations'}:/app/migrations:ro",
                "-v",
                f"{workspace_dir}:{workspace_dir}",
                "-v",
                "/run/aoc-runner:/run/aoc-runner",
            ]
        )
        if emb_cache.exists():
            cmd.extend(["-v", f"{emb_cache}:/app/.cache/embeddings:ro"])

        envs = {
            "AOC_ORGANISM__NAME": f"AOC-CAND-{instance_key}",
            "AOC_DATABASE__SCHEMA_NAME": schema_name,
            "AOC_MEMORY__AGENT_NAME": letta_agent_name,
            "AOC_GOVERNANCE__WORKSPACE_ROOT": str(workspace_dir),
            "AOC_RUNNER__INSTANCE_ID": instance_key,
            "AOC_RUNNER__SOCKET_PATH": "/run/aoc-runner/runner.sock",
            "AOC_TELEGRAM__ENABLED": "false",
            "AOC_TELEGRAM__POLLING_ENABLED": "false",
            "AOC_API__HOST": "0.0.0.0",
        }
        for k, v in envs.items():
            cmd.extend(["-e", f"{k}={v}"])
        cmd.append(self.settings.docker_image)
        return cmd

    async def _default_git_worktree(
        self, worktree_dir: Path, _repo_root: Path, branch_name: str, base_ref: str
    ) -> str | None:
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "add", "-b", branch_name, str(worktree_dir), base_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {(res.stderr or res.stdout).strip()}")
        rev = subprocess.run(
            ["git", "-C", str(worktree_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return rev.stdout.strip() if rev.returncode == 0 else None

    async def _default_provision_schema(self, db_url: str, schema_name: str, worktree_dir: Path) -> None:
        env = os.environ.copy()
        env["AOC_DATABASE__URL"] = db_url
        env["AOC_DATABASE__SCHEMA_NAME"] = schema_name
        alembic_bin = str(self.repo_root / ".venv" / "bin" / "alembic")
        cmd = [alembic_bin, "-c", str(worktree_dir / "alembic.ini"), "upgrade", "head"]
        if not Path(alembic_bin).exists():
            cmd = ["uv", "run", "--no-sync", "alembic", "-c", str(worktree_dir / "alembic.ini"), "upgrade", "head"]
        res = subprocess.run(cmd, cwd=str(worktree_dir), env=env, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"Schema migration failed for {schema_name}: {(res.stderr or res.stdout).strip()}")

    async def _default_docker_launch(self, cmd: list[str]) -> None:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"docker run failed: {(res.stderr or res.stdout).strip()}")

    async def _default_health_check(self, api_port: int, timeout_seconds: float) -> bool:
        import asyncio

        deadline = asyncio.get_running_loop().time() + max(2.0, timeout_seconds)
        async with httpx.AsyncClient(timeout=3.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    r = await client.get(f"http://127.0.0.1:{api_port}/health")
                    if r.status_code == 200:
                        return True
                except Exception:
                    pass
                await asyncio.sleep(1.0)
        return False

    async def spawn(self, req: SpawnRequest) -> SpawnResult:
        short_id = uuid.uuid4().hex[:8]
        instance_key = validate_instance_key(req.instance_key or f"gen{req.generation}-{short_id}")
        schema_suffix = req.instance_key.replace("-", "_") if req.instance_key else short_id
        schema_name = req.target_schema or build_instance_schema_name(schema_suffix)

        # Gate 0: Require explicit Stage 2 human approval when enforced by funnel
        if self.require_stage2_approval:
            if req.proposal_id is None:
                return SpawnResult(
                    success=False,
                    instance_key=instance_key,
                    generation=req.generation,
                    db_schema=schema_name,
                    error="Stage 2 human approval required: missing proposal_id.",
                )
            prop = await self.repo.get_proposal(req.proposal_id)
            if prop is None:
                return SpawnResult(
                    success=False,
                    instance_key=instance_key,
                    generation=req.generation,
                    db_schema=schema_name,
                    error=f"Stage 2 approval check failed: proposal {req.proposal_id} not found.",
                )
            if (
                prop.get("stage") not in {"STAGE2_APPROVED", "STAGE3_ARENA_RUNNING"}
                or not prop.get("approved_by")
                or not prop.get("approved_at")
            ):
                return SpawnResult(
                    success=False,
                    instance_key=instance_key,
                    generation=req.generation,
                    db_schema=schema_name,
                    error=(
                        f"Stage 2 human approval required before spawning "
                        f"(current stage={prop.get('stage')}, approved_by={prop.get('approved_by')!r})."
                    ),
                )

        # Gate 1: Hard host disk headroom check (>= 20% free required)
        free_ratio = float(self._disk_free_ratio_fn(self.instances_root))
        if free_ratio < self.settings.min_free_disk_ratio:
            return SpawnResult(
                success=False,
                instance_key=instance_key,
                generation=req.generation,
                db_schema=schema_name,
                error=(
                    f"Host free disk ratio {free_ratio:.1%} is below safety floor "
                    f"{self.settings.min_free_disk_ratio:.0%}; reproduction blocked."
                ),
            )

        # Gate 2: Global live instance concurrency cap (<= 2 including primary)
        live_count = await self.repo.count_live_instances()
        if live_count >= self.settings.max_concurrent_instances:
            return SpawnResult(
                success=False,
                instance_key=instance_key,
                generation=req.generation,
                db_schema=schema_name,
                error=(
                    f"Live instance cap reached ({live_count}/{self.settings.max_concurrent_instances}); "
                    "cannot incubate concurrent candidate."
                ),
            )

        inst_dir = self.instances_root / instance_key
        worktree_dir = inst_dir / "repo"
        workspace_dir = inst_dir / "workspace"
        branch_name = f"evo-cand-{instance_key}"
        container_name = f"aoc-cand-{instance_key}"
        letta_agent_name = f"aoc-inst-{instance_key}"
        api_port = await self._allocate_port(req.api_port)

        inst_row = await self.repo.create_instance(
            instance_key=instance_key,
            generation=req.generation,
            parent_instance_id=req.parent_instance_id,
            proposal_id=req.proposal_id,
            role=InstanceRole.CANDIDATE.value,
            status=InstanceStatus.INCUBATING.value,
            db_schema=schema_name,
            letta_agent_name=letta_agent_name,
            git_branch=branch_name,
            repo_worktree_path=str(worktree_dir),
            workspace_root=str(workspace_dir),
            container_name=container_name,
            api_port=api_port,
            blank_slate_verified=False,
        )
        inst_id = inst_row["id"]
        docker_cmd = self.build_docker_run_command(
            instance_key=instance_key,
            container_name=container_name,
            schema_name=schema_name,
            letta_agent_name=letta_agent_name,
            worktree_dir=worktree_dir,
            workspace_dir=workspace_dir,
            api_port=api_port,
        )

        try:
            workspace_dir.mkdir(parents=True, exist_ok=True)
            commit_sha = await self._maybe_await(
                self._git_worktree_fn(worktree_dir, self.repo_root, branch_name, req.base_ref)
            )
            if commit_sha:
                await self.repo.update_instance(inst_id, git_commit=commit_sha)

            if req.file_overrides and worktree_dir.exists():
                wt_resolved = worktree_dir.resolve()
                const_set = set(CONSTITUTIONAL_FILES)
                for rel, content in req.file_overrides.items():
                    clean_rel = rel.strip().lstrip("./").replace("\\", "/")
                    if clean_rel in const_set or clean_rel not in EVOLVABLE_ALLOWLIST:
                        raise PermissionError(f"Spawner blocked non-evolvable override: {clean_rel}")
                    dst = (wt_resolved / clean_rel).resolve()
                    if wt_resolved not in dst.parents:
                        raise PermissionError(f"Spawner blocked out-of-worktree override: {clean_rel}")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_text(content, encoding="utf-8")

            target_db_url = req.target_db_url or self.pg_base_db_url
            await self._maybe_await(self._schema_provisioner_fn(target_db_url, schema_name, worktree_dir))
            await self._maybe_await(self._docker_launcher_fn(docker_cmd))

            healthy = bool(await self._maybe_await(self._health_checker_fn(api_port, self.settings.health_timeout_seconds)))
            if not healthy:
                raise RuntimeError(f"Candidate container {container_name} failed health check on port {api_port}")

            if self._blank_slate_verifier_fn is not None:
                report = await self._blank_slate_verifier_fn(
                    target_db_url, schema_name, req.parent_letta_agent_id, instance_key
                )
            else:
                target_db = Database(target_db_url, schema_name=schema_name)
                try:
                    report = await self.verifier.verify(
                        target_db,
                        instance_key=instance_key,
                        parent_letta_agent_id=req.parent_letta_agent_id,
                        expected_schema=schema_name,
                    )
                finally:
                    await target_db.dispose()

            if not report.passed:
                failed = [c.name for c in report.checks if not c.passed]
                raise RuntimeError(f"Blank-slate birth verification failed: {failed}")

            await self.repo.update_instance(
                inst_id,
                status=InstanceStatus.TESTING.value,
                blank_slate_verified=True,
            )
            logger.info(
                "spawner.candidate_ready",
                instance_key=instance_key,
                schema=schema_name,
                port=api_port,
            )
            return SpawnResult(
                success=True,
                instance_id=inst_id,
                instance_key=instance_key,
                generation=req.generation,
                status=InstanceStatus.TESTING.value,
                db_schema=schema_name,
                letta_agent_name=letta_agent_name,
                workspace_root=str(workspace_dir),
                repo_worktree_path=str(worktree_dir),
                container_name=container_name,
                api_port=api_port,
                blank_slate_report=report,
                docker_command=docker_cmd,
            )
        except Exception as exc:
            err_msg = str(exc)
            logger.warning("spawner.failed_triggering_gc", instance_key=instance_key, error=err_msg)
            cull_rep = await self.gc.cull_instance(
                inst_id,
                reason=f"Spawn/BlankSlate failure: {err_msg}",
                proposal_id=req.proposal_id,
                hypothesis=req.hypothesis,
                anti_pattern_tags=["spawn_failure"],
                safety_passed=False,
            )
            return SpawnResult(
                success=False,
                instance_id=inst_id,
                instance_key=instance_key,
                generation=req.generation,
                status=cull_rep.status,
                db_schema=schema_name,
                letta_agent_name=letta_agent_name,
                workspace_root=str(workspace_dir),
                repo_worktree_path=str(worktree_dir),
                container_name=container_name,
                api_port=api_port,
                docker_command=docker_cmd,
                error=err_msg,
            )
