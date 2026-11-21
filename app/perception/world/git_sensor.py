"""Read-only Git Repository DigitalWorldSensor with Commit SHA checkpointing (V2.0 P1-5)."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.perception.interfaces import utcnow
from app.perception.world.interfaces import DigitalWorldSensor, SensorPollResult, WorldEvent, WorldSourceConfig
from app.perception.world.security import sanitize_external_text


class GitWorldSensor(DigitalWorldSensor):
    sensor_type = "git"

    def __init__(self, *, allowed_git_roots: Sequence[str], timeout_seconds: float = 10.0) -> None:
        self.allowed_git_roots = [Path(p).resolve() for p in allowed_git_roots]
        self.timeout_seconds = timeout_seconds

    def _validate_repo_path(self, repo_path: str) -> Path:
        candidate = Path(repo_path).resolve()
        if not candidate.exists():
            raise FileNotFoundError(f"Git repo path does not exist: {candidate}")
        allowed = any(candidate == root or root in candidate.parents for root in self.allowed_git_roots)
        if not allowed:
            raise PermissionError(f"Git repo path '{candidate}' is outside allowed_git_roots")
        return candidate

    async def _run_git(self, repo: Path, *args: str) -> str:
        git_bin = shutil.which("git")
        if not git_bin:
            raise FileNotFoundError("git binary not found")
        # Strictly read-only invocation with hooks disabled
        proc = await asyncio.create_subprocess_exec(
            git_bin,
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {err_b.decode(errors='replace').strip()}")
        return out_b.decode("utf-8", errors="replace").strip()

    async def poll(self, source: WorldSourceConfig, cursor: dict[str, Any]) -> SensorPollResult:
        repo_raw = str(source.config.get("repo_path") or "").strip()
        if not repo_raw:
            return SensorPollResult(source_id=source.source_id, new_cursor=cursor, error="missing repo_path")
        repo = self._validate_repo_path(repo_raw)
        ref = str(source.config.get("branch") or "HEAD")
        max_commits = max(1, min(int(source.config.get("max_commits", 5)), 30))

        head_sha = await self._run_git(repo, "rev-parse", ref)
        last_sha = str(cursor.get("last_commit_sha") or "")
        new_cursor = dict(cursor)

        if last_sha == head_sha:
            return SensorPollResult(source_id=source.source_id, events=[], new_cursor=new_cursor, not_modified=True)

        rev_range = f"{last_sha}..{head_sha}" if last_sha else head_sha
        fmt = "%H%x1f%cI%x1f%an%x1f%s"
        try:
            log_out = await self._run_git(repo, "log", rev_range, f"-n{max_commits}", f"--format={fmt}")
        except RuntimeError:
            # Handle history rewrite / shallow clone gracefully by falling back to HEAD
            log_out = await self._run_git(repo, "log", head_sha, f"-n{max_commits}", f"--format={fmt}")

        events: list[WorldEvent] = []
        for line in log_out.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            sha, iso_dt, author, subject = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
            files_out = ""
            try:
                files_out = await self._run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
            except Exception:
                pass
            changed_files = [f.strip() for f in files_out.splitlines() if f.strip()][:20]
            try:
                obs_dt = datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
                if obs_dt.tzinfo is None:
                    obs_dt = obs_dt.replace(tzinfo=timezone.utc)
            except Exception:
                obs_dt = utcnow()

            content = sanitize_external_text(
                f"Commit {sha[:10]} by {author}: {subject}. Changed files: {', '.join(changed_files) or 'none'}",
                max_chars=2500,
            )
            ev = WorldEvent(
                source_id=source.source_id,
                event_type="git_commit",
                title=sanitize_external_text(f"[{source.name or repo.name}] {subject}", max_chars=400),
                content=content,
                source_reliability=source.reliability_score,
                observed_at=obs_dt,
                metadata={
                    "commit_sha": sha,
                    "author": author,
                    "repo_path": str(repo),
                    "changed_files": changed_files,
                },
            )
            ev.ensure_hash()
            events.append(ev)

        new_cursor["last_commit_sha"] = head_sha
        return SensorPollResult(source_id=source.source_id, events=events, new_cursor=new_cursor)
