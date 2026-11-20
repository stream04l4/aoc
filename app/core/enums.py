"""Shared enumerations used across module interfaces."""

from __future__ import annotations

from enum import StrEnum


class LifecycleState(StrEnum):
    INITIALIZING = "INITIALIZING"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    SLEEPING = "SLEEPING"
    CRITICAL = "CRITICAL"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class DriveType(StrEnum):
    SURVIVAL = "survival"
    REPRODUCTION = "reproduction"
    CURIOSITY = "curiosity"


NO_DRIVE = "none"


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMEOUT,
            ExecutionStatus.CANCELLED,
        }


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    REJECTED = "REJECTED"
    ABANDONED = "ABANDONED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
            TaskStatus.ABANDONED,
        }


class GoalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    ACHIEVED = "ACHIEVED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    ABANDONED = "ABANDONED"


class PermissionLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"

    @property
    def rank(self) -> int:
        return int(self.value[1])

    @classmethod
    def max(cls, *levels: "PermissionLevel") -> "PermissionLevel":
        return max(levels, key=lambda lv: lv.rank)


class MemoryType(StrEnum):
    IDENTITY = "identity"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    SELF_MODEL = "self_model"
