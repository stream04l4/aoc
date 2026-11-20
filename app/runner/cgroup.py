"""cgroup v2 subtree delegation manager with automatic non-systemd fallback (V2.0 P1-3).

When running under systemd with ``Delegate=yes`` (and ``User=ubuntu``):
  - Detects the writable delegated cgroup v2 directory from ``/proc/self/cgroup``.
  - Moves the Runner main process into a leaf ``runner-main`` child cgroup if needed so
    cgroup v2's no-internal-processes rule allows enabling ``+memory +pids`` on ``cgroup.subtree_control``.
  - Creates a dedicated ``exec-<execution_id>`` child cgroup per task with ``memory.max`` and ``pids.max``.
  - Uses ``cgroup.kill`` (Linux 5.14+) or kills every PID listed in ``cgroup.procs`` on cancellation/timeout.

When running in non-systemd / CI / unit test environments:
  - Automatically degrades to ``fallback_pgroup`` mode (``start_new_session=True`` + ``os.killpg``)
    without requiring root privileges or Docker socket access.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from app.observability.logging import get_logger

logger = get_logger("aoc.runner.cgroup")

CGROUP_FS_ROOT = Path("/sys/fs/cgroup")


def detect_self_cgroup_dir(
    proc_self_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_fs_root: Path = CGROUP_FS_ROOT,
) -> Path | None:
    try:
        raw = proc_self_cgroup.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.strip()
        # Unified cgroup v2 entry has hierarchy ID 0 and empty controllers: "0::/system.slice/..."
        if line.startswith("0::"):
            rel = line[3:].lstrip("/")
            return cgroup_fs_root / rel if rel else cgroup_fs_root
    return None


class CgroupV2Manager:
    def __init__(
        self,
        *,
        memory_max: str = "2G",
        pids_max: int = 128,
        cgroup_fs_root: Path = CGROUP_FS_ROOT,
        proc_self_cgroup: Path = Path("/proc/self/cgroup"),
        allow_cgroup: bool = True,
    ) -> None:
        self.memory_max = str(memory_max)
        self.pids_max = int(pids_max)
        self._cgroup_fs_root = cgroup_fs_root
        self._proc_self_cgroup = proc_self_cgroup
        self.delegated: bool = False
        self.root_dir: Path | None = None
        self.mode: str = "fallback_pgroup"
        if allow_cgroup:
            self._probe_and_prepare()

    def _probe_and_prepare(self) -> None:
        self_dir = detect_self_cgroup_dir(self._proc_self_cgroup, self._cgroup_fs_root)
        if self_dir is None or not self_dir.exists():
            return
        # If runner was already moved into runner-main on a prior init, step up to delegated parent
        if self_dir.name == "runner-main" and self_dir.parent.exists() and os.access(self_dir.parent, os.W_OK):
            self_dir = self_dir.parent

        if not os.access(self_dir, os.W_OK):
            return

        # Test whether we can actually create a child cgroup (verifies true systemd Delegate=yes)
        probe_dir = self_dir / f".aoc-probe-{os.getpid()}"
        try:
            probe_dir.mkdir(exist_ok=True)
            probe_dir.rmdir()
        except OSError:
            return

        # Satisfy cgroup v2 no-internal-processes rule before enabling subtree controllers
        runner_leaf = self_dir / "runner-main"
        try:
            runner_leaf.mkdir(exist_ok=True)
            (runner_leaf / "cgroup.procs").write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError as exc:
            logger.debug("cgroup.move_runner_skipped", path=str(runner_leaf), error=str(exc))

        # Best-effort enable memory and pids controllers in subtree_control
        subtree_ctrl = self_dir / "cgroup.subtree_control"
        avail_ctrl = self_dir / "cgroup.controllers"
        if subtree_ctrl.exists() and avail_ctrl.exists():
            try:
                available = set(avail_ctrl.read_text(encoding="utf-8").split())
                to_enable = [f"+{c}" for c in ("memory", "pids") if c in available]
                if to_enable:
                    subtree_ctrl.write_text(" ".join(to_enable) + "\n", encoding="utf-8")
            except OSError as exc:
                logger.debug("cgroup.subtree_control_partial", path=str(subtree_ctrl), error=str(exc))

        self.delegated = True
        self.root_dir = self_dir
        self.mode = "cgroup_v2"
        logger.info("cgroup.delegated_ready", root=str(self_dir), memory_max=self.memory_max, pids_max=self.pids_max)

    def create_execution_cgroup(self, execution_id: str) -> Path | None:
        if not self.delegated or self.root_dir is None:
            return None
        safe_id = "".join(ch for ch in execution_id if ch.isalnum() or ch in {"-", "_"})[:48]
        cg_dir = self.root_dir / f"exec-{safe_id}"
        try:
            cg_dir.mkdir(exist_ok=True)
        except OSError as exc:
            logger.warning("cgroup.create_failed", path=str(cg_dir), error=str(exc))
            return None

        # Apply memory.max and pids.max best-effort if controllers are active
        mem_file = cg_dir / "memory.max"
        if mem_file.exists() and self.memory_max:
            try:
                mem_file.write_text(f"{self.memory_max}\n", encoding="utf-8")
            except OSError:
                pass
        pids_file = cg_dir / "pids.max"
        if pids_file.exists() and self.pids_max > 0:
            try:
                pids_file.write_text(f"{self.pids_max}\n", encoding="utf-8")
            except OSError:
                pass
        return cg_dir

    def attach_pid(self, cg_dir: Path | None, pid: int) -> bool:
        if cg_dir is None or pid <= 0:
            return False
        procs_file = cg_dir / "cgroup.procs"
        try:
            procs_file.write_text(f"{pid}\n", encoding="utf-8")
            return True
        except OSError as exc:
            logger.warning("cgroup.attach_failed", path=str(cg_dir), pid=pid, error=str(exc))
            return False

    def kill_execution(self, cg_path: str | None, pgid: int | None = None, pid: int | None = None, grace_seconds: float = 2.0) -> None:
        """Terminate every process belonging to the execution cleanly without leaving orphans."""
        # 1. Polite SIGTERM to process group first
        if pgid and pgid > 0:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                pass
        elif pid and pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        # Wait briefly for graceful exit
        deadline = time.monotonic() + max(0.05, grace_seconds)
        while time.monotonic() < deadline:
            if not self._any_alive(cg_path, pid):
                break
            time.sleep(0.05)

        # 2. Hard SIGKILL via cgroup.kill (Linux 5.14+) or all cgroup.procs + killpg
        if cg_path:
            cg_dir = Path(cg_path)
            kill_file = cg_dir / "cgroup.kill"
            if kill_file.exists():
                try:
                    kill_file.write_text("1\n", encoding="utf-8")
                except OSError:
                    pass
            procs_file = cg_dir / "cgroup.procs"
            if procs_file.exists():
                try:
                    for line in procs_file.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line.isdigit():
                            try:
                                os.kill(int(line), signal.SIGKILL)
                            except OSError:
                                pass
                except OSError:
                    pass

        if pgid and pgid > 0:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        if pid and pid > 0:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def cleanup_cgroup(self, cg_path: str | None) -> None:
        if not cg_path:
            return
        cg_dir = Path(cg_path)
        if not cg_dir.exists():
            return
        try:
            cg_dir.rmdir()
        except OSError:
            pass

    def _any_alive(self, cg_path: str | None, pid: int | None) -> bool:
        if cg_path:
            procs_file = Path(cg_path) / "cgroup.procs"
            if procs_file.exists():
                try:
                    if any(line.strip().isdigit() for line in procs_file.read_text(encoding="utf-8").splitlines()):
                        return True
                except OSError:
                    pass
        if pid and pid > 0:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
        return False
