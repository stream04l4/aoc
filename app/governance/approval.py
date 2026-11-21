"""Bound Human-in-the-Loop L2 Approval Service (V2.0 P1-8, Task Book Section 5.6.2).

Enforces:
  - Cryptographically unique single-use nonce + expiry TTL bound to (task_id, plan_hash, params_digest, permission_level).
  - Atomic notification enqueue via PostgreSQL Outbox (notification.dispatch).
  - Unified approval/rejection entry point shared by REST API (/v2/approvals/*) and Telegram Webhook,
    always delegating final re-validation & dispatch to AgentOrchestrator.approve_and_dispatch().
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.orchestration.orchestrator import (
    AgentOrchestrator,
    ApprovalConflictError,
    ApprovalDeniedError,
    ApprovalNotFoundError,
    compute_params_digest,
)
from app.perception.interfaces import utcnow
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class ApprovalService:
    def __init__(
        self,
        repo: StateRepository,
        orchestrator: AgentOrchestrator,
        *,
        default_ttl_hours: float = 24.0,
    ) -> None:
        self.repo = repo
        self.orchestrator = orchestrator
        self.default_ttl_hours = max(0.01, float(default_ttl_hours))

    async def create_approval_request(
        self,
        *,
        task_id: uuid.UUID,
        plan_id: uuid.UUID | None,
        plan_hash: str,
        params_digest: str,
        permission_level: str,
        objective: str = "",
        assigned_agent: str = "",
        ttl_hours: float | None = None,
        cycle_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Create a bound single-use approval record and enqueue an async Telegram/operator notification."""
        nonce = secrets.token_hex(16)
        ttl = self.default_ttl_hours if ttl_hours is None else max(0.01, float(ttl_hours))
        expires_at = utcnow() + timedelta(hours=ttl)
        record = await self.repo.create_approval_record(
            task_id=task_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            params_digest=params_digest,
            permission_level=permission_level,
            nonce=nonce,
            expires_at=expires_at,
        )
        await self.repo.enqueue_outbox_event(
            event_type=OutboxEventType.NOTIFICATION_DISPATCH,
            idempotency_key=f"notify:approval:{record['approval_id']}",
            payload={
                "kind": "approval_requested",
                "approval_id": record["approval_id"],
                "task_id": str(task_id),
                "permission_level": permission_level,
                "assigned_agent": assigned_agent,
                "objective": objective[:300],
                "nonce": nonce,
                "expires_at": expires_at.isoformat(),
                "cycle_id": str(cycle_id) if cycle_id else None,
            },
            aggregate_type="approval",
            aggregate_id=uuid.UUID(record["approval_id"]),
        )
        return record

    async def list_approvals(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return await self.repo.list_approval_records(status=status, limit=limit)

    async def get_by_nonce(self, nonce: str) -> dict[str, Any] | None:
        return await self.repo.get_approval_by_nonce(nonce)

    async def _validate_record_preconditions(
        self,
        approval: dict[str, Any] | None,
        *,
        expected_nonce: str | None = None,
        check_params_digest: bool = True,
    ) -> dict[str, Any]:
        if approval is None:
            raise ApprovalNotFoundError("Approval record not found")
        if approval["status"] != "PENDING":
            raise ApprovalConflictError(
                f"Approval {approval['approval_id']} is not PENDING (status={approval['status']})"
            )
        if expected_nonce is not None and not secrets.compare_digest(expected_nonce, approval["nonce"]):
            raise ApprovalDeniedError("Approval nonce verification failed")
        now = utcnow()
        if _ensure_utc(approval["expires_at"]) <= now:
            await self.repo.update_approval_record(
                uuid.UUID(approval["approval_id"]),
                status="EXPIRED",
                decision_note="Approval expired before decision",
                decided_at=now,
            )
            raise ApprovalConflictError(f"Approval {approval['approval_id']} has expired")

        if check_params_digest:
            task = await self.repo.get_task(uuid.UUID(approval["task_id"]))
            if task is None:
                raise ApprovalNotFoundError(f"Task {approval['task_id']} not found")
            current_digest = compute_params_digest(task)
            if approval["params_digest"] and approval["params_digest"] != current_digest:
                raise ApprovalConflictError(
                    "Task execution parameters changed since approval request was created (params_digest mismatch)"
                )
        return approval

    async def approve_by_id(
        self,
        approval_id: uuid.UUID,
        *,
        approver: str,
        reason: str = "operator approved",
        channel: str = "api",
        expected_nonce: str | None = None,
    ) -> dict[str, Any]:
        approval = await self.repo.get_approval_record(approval_id)
        approval = await self._validate_record_preconditions(
            approval, expected_nonce=expected_nonce, check_params_digest=True
        )
        res = await self.orchestrator.approve_and_dispatch(
            task_id=uuid.UUID(approval["task_id"]),
            approver=approver,
            reason=reason,
            channel=channel,
        )
        res["approval_id"] = approval["approval_id"]
        return res

    async def reject_by_id(
        self,
        approval_id: uuid.UUID,
        *,
        approver: str,
        reason: str = "operator rejected",
        channel: str = "api",
        expected_nonce: str | None = None,
    ) -> dict[str, Any]:
        approval = await self.repo.get_approval_record(approval_id)
        approval = await self._validate_record_preconditions(
            approval, expected_nonce=expected_nonce, check_params_digest=False
        )
        res = await self.orchestrator.reject_approval(
            task_id=uuid.UUID(approval["task_id"]),
            approver=approver,
            reason=reason,
            channel=channel,
        )
        res["approval_id"] = approval["approval_id"]
        return res

    async def decide_by_nonce(
        self,
        nonce: str,
        *,
        action: str,
        approver: str,
        reason: str = "",
        channel: str = "telegram",
    ) -> dict[str, Any]:
        approval = await self.repo.get_approval_by_nonce(nonce)
        if approval is None:
            raise ApprovalNotFoundError("Approval nonce not found")
        aid = uuid.UUID(approval["approval_id"])
        if action == "approve":
            return await self.approve_by_id(
                aid,
                approver=approver,
                reason=reason or f"approved via {channel}",
                channel=channel,
                expected_nonce=nonce,
            )
        if action == "reject":
            return await self.reject_by_id(
                aid,
                approver=approver,
                reason=reason or f"rejected via {channel}",
                channel=channel,
                expected_nonce=nonce,
            )
        raise ApprovalDeniedError(f"Unsupported approval action '{action}'")
