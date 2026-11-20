"""OS-level ProcessIdentity capture and verification resistant to PID reuse (V2.0 P1-3).

Verification requires ALL of:
  1. Current /proc/sys/kernel/random/boot_id == identity.host_boot_id
  2. /proc/<pid>/stat exists, process state is NOT 'Z'/'X' (zombie/dead),
     and field 22 (starttime in clock ticks since boot) == identity.process_start_ticks
  3. If identity.cgroup_path is recorded and present on disk, pid is in <cgroup_path>/cgroup.procs
"""

from __future__ import annotations

import os
from pathlib import Path

from app.perception.interfaces import utcnow
from app.runner.protocol import ProcessIdentity

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def read_host_boot_id(boot_id_path: Path = BOOT_ID_PATH) -> str:
    try:
        return boot_id_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown-boot-id"


def parse_proc_stat(stat_text: str) -> tuple[str, int] | None:
    """Parse /proc/<pid>/stat safely even when comm contains spaces or parentheses.

    Returns (state_char, starttime_ticks) or None if malformed.
    In /proc/<pid>/stat:
      field 1: pid
      field 2: (comm)
      field 3: state  -> index 0 after the final ')'
      ...
      field 22: starttime -> index 19 after the final ')'
    """
    rparen = stat_text.rfind(")")
    if rparen < 0:
        return None
    rest = stat_text[rparen + 1 :].strip().split()
    if len(rest) <= 19:
        return None
    state = rest[0]
    try:
        starttime = int(rest[19])
    except ValueError:
        return None
    return state, starttime


def read_process_stat(pid: int, proc_root: Path = Path("/proc")) -> tuple[str, int] | None:
    if pid <= 0:
        return None
    stat_file = proc_root / str(pid) / "stat"
    try:
        raw = stat_file.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_proc_stat(raw)


def read_process_start_ticks(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    parsed = read_process_stat(pid, proc_root=proc_root)
    if parsed is None:
        return None
    return parsed[1]


def capture_process_identity(
    *,
    execution_id: str,
    idempotency_key: str,
    pid: int,
    runner_id: str,
    shim_pid: int | None = None,
    cgroup_path: str | None = None,
    systemd_unit: str | None = None,
    proc_root: Path = Path("/proc"),
    boot_id_path: Path = BOOT_ID_PATH,
) -> ProcessIdentity:
    start_ticks = read_process_start_ticks(pid, proc_root=proc_root) or 0
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    return ProcessIdentity(
        execution_id=execution_id,
        idempotency_key=idempotency_key,
        pid=pid,
        process_group_id=pgid,
        shim_pid=shim_pid,
        host_boot_id=read_host_boot_id(boot_id_path),
        process_start_ticks=start_ticks,
        cgroup_path=cgroup_path,
        systemd_unit=systemd_unit,
        runner_id=runner_id,
        started_at=utcnow(),
    )


def verify_process_identity(
    identity: ProcessIdentity,
    *,
    proc_root: Path = Path("/proc"),
    boot_id_path: Path = BOOT_ID_PATH,
) -> tuple[bool, str]:
    """Verify whether the exact process described by ``identity`` is still alive."""
    current_boot = read_host_boot_id(boot_id_path)
    if identity.host_boot_id and current_boot != identity.host_boot_id:
        return False, "host_boot_id_mismatch"

    if identity.pid <= 0:
        return False, "invalid_pid"

    parsed = read_process_stat(identity.pid, proc_root=proc_root)
    if parsed is None:
        return False, "process_not_found"

    state, observed_ticks = parsed
    if state in {"Z", "X", "x"}:
        return False, f"process_zombie_or_dead:{state}"

    if identity.process_start_ticks <= 0 or observed_ticks != identity.process_start_ticks:
        return False, f"start_ticks_mismatch:expected={identity.process_start_ticks},observed={observed_ticks}"

    if identity.cgroup_path:
        procs_file = Path(identity.cgroup_path) / "cgroup.procs"
        if procs_file.exists():
            try:
                pids = {line.strip() for line in procs_file.read_text(encoding="utf-8").splitlines() if line.strip()}
            except OSError:
                return False, "cgroup_unreadable"
            if str(identity.pid) not in pids:
                return False, "pid_not_in_expected_cgroup"

    return True, "alive_verified"
