"""Shared enumerations used across module interfaces."""

from __future__ import annotations

from enum import StrEnum


class LifecycleState(StrEnum):
    INITIALIZING = "INITIALIZING"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    SLEEPING = "SLEEPING"
    THROTTLED = "THROTTLED"
    CRITICAL = "CRITICAL"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class QuotaStatus(StrEnum):
    QUOTA_OK = "QUOTA_OK"
    QUOTA_EMERGENCY_ONLY = "QUOTA_EMERGENCY_ONLY"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


class DriveType(StrEnum):
    SURVIVAL = "survival"
    REPRODUCTION = "reproduction"
    CURIOSITY = "curiosity"


NO_DRIVE = "none"


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHING = "DISPATCHING"
    RUNNING = "RUNNING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMEOUT,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.ORPHANED,
            ExecutionStatus.RECOVERY_REQUIRED,
        }


class TaskStatus(StrEnum):
    # Durable Task State Machine (V2.0 P1-2) + V1.0 backward-compatible aliases
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"  # V1.0 alias for AWAITING_APPROVAL
    QUEUED = "QUEUED"
    PENDING = "PENDING"  # V1.0 legacy initial status
    DISPATCHING = "DISPATCHING"
    DISPATCHED = "DISPATCHED"  # V1.0 legacy alias for DISPATCHING
    RUNNING = "RUNNING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    VERIFYING = "VERIFYING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    ABANDONED = "ABANDONED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    ORPHANED = "ORPHANED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    BUDGET_DEFERRED = "BUDGET_DEFERRED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
            TaskStatus.ABANDONED,
            TaskStatus.CANCELLED,
            TaskStatus.RECOVERY_REQUIRED,
            TaskStatus.BUDGET_DEFERRED,
        }

    @property
    def is_awaiting_approval(self) -> bool:
        return self in {TaskStatus.REQUIRES_APPROVAL, TaskStatus.AWAITING_APPROVAL}


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
