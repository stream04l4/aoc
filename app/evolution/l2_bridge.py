"""Bounded L2 Evolution Bridge (V5.3 M4).

Connects Meta-Reflection L2 upgrade cards approved by Joel (`HumanBridge`) to AOC V5.0's
governed evolution pipeline:
  1. Strict pre-check rejecting any `CONSTITUTIONAL_FILES`, files outside `EVOLVABLE_ALLOWLIST`,
     or sensitive YAML namespaces (`api.*`, `governance.*`, `blocked_hosts`, etc.).
  2. Creates `STAGE0_DRAFT` proposal in `public.evolution_proposals` via `ProposalManager`.
  3. Materializes the proposed edit in an isolated temporary worktree (deleted cleanly in `finally`),
     runs zero-token 4-layer `ConstitutionGuard` screening + optional pytest suite (`screen_proposal`).
  4. Emits a Stage 2 confirmation card (`ask_human` with diff preview + rollback plan + 3 choices).
  5. On Stage 2 approval: backs up `.bak` + applies allowlisted YAML safely (when repo is writable) or
     writes a signed patch artifact under `<worktrees_root>/approved/` (when container mount is `:ro`),
     updating `world_states(kind='l2_evolution_bridge_state', key='primary')`.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from app.evolution.constitution_guard import CONSTITUTIONAL_FILES, ConstitutionGuard, EVOLVABLE_ALLOWLIST
from app.evolution.interfaces import ProposalStage
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.repository import StateRepository

logger = get_logger("aoc.evolution.l2_bridge")

_CONSTITUTIONAL_SET = frozenset(CONSTITUTIONAL_FILES)
_WRITABLE_YAML_ALLOWLIST = frozenset({"config/drives.yaml", "config/memory.yaml"})
_FORBIDDEN_YAML_KEYS = frozenset(
    {
        "api",
        "governance",
        "blocked_hosts",
        "admin_key",
        "api_key",
        "bot_token",
        "webhook_secret",
        "database",
        "auth_enabled",
        "auto_approve_max_level",
        "internal_token",
        "secret",
    }
)


def _deep_merge_dict(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge_dict(merged[k], v)
        else:
            merged[k] = v
    return merged


def _scan_forbidden_yaml_keys(obj: Any, prefix: str = "") -> list[str]:
    bad: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).strip().lower()
            full = f"{prefix}.{kl}" if prefix else kl
            if kl in _FORBIDDEN_YAML_KEYS:
                bad.append(f"forbidden_yaml_key:{full}")
            bad.extend(_scan_forbidden_yaml_keys(v, prefix=full))
    return bad


class L2EvolutionBridge:
    """Bridges Meta-Reflection L2 outreach decisions to V5.0 ProposalManager + ConstitutionGuard."""

    def __init__(
        self,
        *,
        evo_repo: EvolutionRepository,
        state_repo: StateRepository,
        proposal_manager: ProposalManager,
        constitution_guard: ConstitutionGuard,
        human_bridge: Any = None,
        repo_root: str | Path,
        instances_root: str | Path = "/home/ubuntu/aoc-instances",
        workspace_root: str | Path = "/home/ubuntu/aoc-workspaces",
        enabled: bool = False,
        pytest_runner_fn: Callable[[Path], Awaitable[tuple[bool, str]] | tuple[bool, str]] | None = None,
    ) -> None:
        self.evo_repo = evo_repo
        self.state_repo = state_repo
        self.proposal_manager = proposal_manager
        self.constitution_guard = constitution_guard
        self.human_bridge = human_bridge
        self.repo_root = Path(repo_root).resolve()
        self.instances_root = Path(instances_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.enabled = bool(enabled)
        self.pytest_runner_fn = pytest_runner_fn

    def validate_proposed_change(self, proposed_change: dict[str, Any] | None) -> tuple[bool, list[str], str]:
        """Strict constitutional & allowlist pre-check before touching any filesystem or worktree."""
        if not isinstance(proposed_change, dict) or not proposed_change:
            return False, ["missing_proposed_change"], ""

        raw_target = str(proposed_change.get("target_file") or "").strip()
        if not raw_target:
            return False, ["missing_target_file"], ""
        if raw_target.startswith("/") or ".." in Path(raw_target).parts or "\\" in raw_target:
            return False, [f"invalid_target_path:{raw_target}"], ""

        rel = Path(raw_target.lstrip("./")).as_posix()
        if rel in _CONSTITUTIONAL_SET:
            return False, [f"constitutional_file_forbidden:{rel}"], rel
        if rel not in EVOLVABLE_ALLOWLIST:
            return False, [f"outside_evolvable_allowlist:{rel}"], rel

        violations: list[str] = []
        if rel.endswith((".yaml", ".yml")):
            yaml_updates = proposed_change.get("yaml_updates")
            file_content = proposed_change.get("file_content")
            if isinstance(yaml_updates, dict) and yaml_updates:
                violations.extend(_scan_forbidden_yaml_keys(yaml_updates))
            elif isinstance(file_content, str) and file_content.strip():
                try:
                    parsed = yaml.safe_load(file_content)
                    if not isinstance(parsed, dict):
                        violations.append("yaml_file_content_must_be_mapping")
                    else:
                        violations.extend(_scan_forbidden_yaml_keys(parsed))
                except Exception as exc:
                    violations.append(f"invalid_yaml_content:{exc}")
            else:
                violations.append("yaml_change_requires_yaml_updates_or_file_content")
        elif rel.endswith(".py"):
            reps = proposed_change.get("replacements")
            file_content = proposed_change.get("file_content")
            has_reps = isinstance(reps, list) and any(
                isinstance(r, dict) and str(r.get("old") or "") for r in reps
            )
            has_content = isinstance(file_content, str) and bool(file_content.strip())
            if not has_reps and not has_content:
                violations.append("python_change_requires_replacements_or_file_content")
        else:
            violations.append(f"unsupported_file_type:{rel}")

        return len(violations) == 0, violations, rel

    def _resolve_worktrees_root(self) -> Path:
        for base in (self.instances_root, self.workspace_root):
            cand = base / "l2-worktrees"
            try:
                cand.mkdir(parents=True, exist_ok=True)
                return cand
            except OSError:
                continue
        raise PermissionError("No writable root available for l2-worktrees")

    def _prepare_isolated_worktree(self, proposal_key: str) -> tuple[Path, bool]:
        wt_root = self._resolve_worktrees_root()
        wt_path = (wt_root / proposal_key).resolve()
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

        git_dir = self.repo_root / ".git"
        if git_dir.exists() and os.access(git_dir, os.W_OK):
            res = subprocess.run(
                ["git", "-C", str(self.repo_root), "worktree", "add", "--detach", str(wt_path), "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if res.returncode == 0 and wt_path.is_dir():
                return wt_path, True

        wt_path.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", ".pytest_cache")
        for sub in ("app", "config"):
            src = self.repo_root / sub
            if src.is_dir():
                shutil.copytree(src, wt_path / sub, ignore=ignore, dirs_exist_ok=True)
        return wt_path, False

    def _cleanup_worktree(self, wt_path: Path, used_git_worktree: bool) -> None:
        if used_git_worktree:
            try:
                subprocess.run(
                    ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(wt_path)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except Exception:
                pass
        shutil.rmtree(wt_path, ignore_errors=True)

    @staticmethod
    def _apply_change_in_worktree(
        wt_path: Path, target_file: str, proposed_change: dict[str, Any]
    ) -> tuple[str, str, str]:
        target = (wt_path / target_file).resolve()
        if wt_path.resolve() not in target.parents:
            raise PermissionError(f"Path escape denied: {target_file}")
        if not target.is_file():
            raise FileNotFoundError(f"Target file not found in worktree: {target_file}")

        old_text = target.read_text(encoding="utf-8")
        diff_old_text = old_text
        if target_file.endswith((".yaml", ".yml")):
            yaml_updates = proposed_change.get("yaml_updates")
            if isinstance(yaml_updates, dict) and yaml_updates:
                cur_doc = yaml.safe_load(old_text) or {}
                if not isinstance(cur_doc, dict):
                    raise ValueError(f"Existing YAML root in {target_file} is not a mapping")
                diff_old_text = yaml.safe_dump(cur_doc, sort_keys=False, allow_unicode=True)
                merged_doc = _deep_merge_dict(cur_doc, yaml_updates)
                new_text = yaml.safe_dump(merged_doc, sort_keys=False, allow_unicode=True)
            else:
                new_text = str(proposed_change.get("file_content") or "")
        else:
            reps = proposed_change.get("replacements")
            if isinstance(reps, list) and reps:
                new_text = old_text
                for rep in reps:
                    old_seg = str((rep or {}).get("old") or "")
                    new_seg = str((rep or {}).get("new") or "")
                    if not old_seg or old_seg not in new_text:
                        raise ValueError(f"Replacement snippet not found in {target_file}")
                    new_text = new_text.replace(old_seg, new_seg, 1)
            else:
                new_text = str(proposed_change.get("file_content") or "")
            ast.parse(new_text, filename=target_file)

        target.write_text(new_text, encoding="utf-8")
        diff_lines = list(
            difflib.unified_diff(
                diff_old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{target_file}",
                tofile=f"b/{target_file}",
                n=2,
            )
        )
        return old_text, new_text, "".join(diff_lines)

    async def _update_bridge_state(self, updates: dict[str, Any]) -> None:
        payload = {**updates, "updated_at": utcnow().isoformat()}
        await self.state_repo.upsert_world_item(
            kind="l2_evolution_bridge_state",
            key="primary",
            value=payload,
            status="active",
            source="l2_evolution_bridge",
        )

    async def on_proposal_adopted(
        self,
        *,
        card_id: str,
        card_val: dict[str, Any],
        sender: str = "joel",
    ) -> dict[str, Any]:
        """Stage 1 screening trigger when Joel adopts a Meta-Reflection L2 card."""
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "l2_evolution_bridge_disabled"}

        meta = card_val.get("meta") if isinstance(card_val.get("meta"), dict) else {}
        proposed_change = meta.get("proposed_change") if isinstance(meta.get("proposed_change"), dict) else {}
        ok_pre, pre_violations, target_file = self.validate_proposed_change(proposed_change)
        if not ok_pre:
            await self.state_repo.log_event(
                "l2_bridge.constitutional_rejected",
                level="warning",
                message=f"Rejected L2 proposed_change on '{target_file or 'unknown'}': {', '.join(pre_violations)}",
                payload={"card_id": card_id, "target_file": target_file, "violations": pre_violations, "sender": sender},
            )
            await self._update_bridge_state(
                {
                    "last_stage": "CONSTITUTIONAL_REJECTED",
                    "last_target_file": target_file or None,
                    "last_violations": pre_violations,
                    "last_source_card_id": card_id,
                }
            )
            return {
                "ok": False,
                "stage": "CONSTITUTIONAL_REJECTED",
                "target_file": target_file,
                "violations": pre_violations,
            }

        parent = await self.evo_repo.ensure_primary_instance(
            workspace_root=str(self.workspace_root),
        )
        hypothesis = str(
            proposed_change.get("hypothesis")
            or meta.get("hypothesis")
            or card_val.get("reason")
            or card_val.get("title")
            or f"L2 evolution on {target_file}"
        )[:500]
        trigger_reason = str(card_val.get("reason") or card_val.get("title") or "Meta-reflection L2 adoption")[:500]

        prop = await self.proposal_manager.create_proposal(
            parent_instance_id=parent["id"],
            target_generation=int(parent.get("generation", 0)) + 1,
            trigger_type="META_REFLECTION_L2",
            trigger_reason=trigger_reason,
            hypothesis=hypothesis,
            genome_delta={
                "files": [target_file],
                "summary": str(proposed_change.get("summary") or card_val.get("title") or "")[:300],
                "card_id": card_id,
                "rollback_plan": str(meta.get("rollback_plan") or "还原备份文件或回滚提交")[:300],
            },
            cooldown_hours=0.0,
        )

        wt_path: Path | None = None
        used_git = False
        diff_text = ""
        screened: dict[str, Any] = prop
        try:
            wt_path, used_git = self._prepare_isolated_worktree(prop["proposal_key"])
            _, new_text, diff_text = self._apply_change_in_worktree(wt_path, target_file, proposed_change)
            delta_upd = dict(prop.get("genome_delta") or {})
            delta_upd["file_overrides"] = {target_file: new_text}
            delta_upd["diff_preview"] = diff_text[:4000]
            await self.evo_repo.update_proposal(prop["id"], genome_delta=delta_upd)
            screened = await self.proposal_manager.screen_proposal(
                prop["id"],
                candidate_root=wt_path,
                pytest_runner_fn=self.pytest_runner_fn,
                emit_outbox_notification=False,
            )
        except Exception as exc:
            await self.evo_repo.update_proposal(
                prop["id"],
                stage=ProposalStage.STAGE1_FAILED.value,
                stage1_report={"passed": False, "violations": [f"worktree_apply_error:{exc}"]},
            )
            await self.state_repo.log_event(
                "l2_bridge.stage1_failed",
                level="warning",
                message=f"L2 Stage 1 preparation failed for '{target_file}': {exc}",
                payload={"proposal_key": prop["proposal_key"], "target_file": target_file, "error": str(exc)},
            )
            await self._update_bridge_state(
                {
                    "last_proposal_key": prop["proposal_key"],
                    "last_stage": ProposalStage.STAGE1_FAILED.value,
                    "last_target_file": target_file,
                    "last_violations": [f"worktree_apply_error:{exc}"],
                }
            )
            return {
                "ok": False,
                "stage": ProposalStage.STAGE1_FAILED.value,
                "proposal_id": str(prop["id"]),
                "proposal_key": prop["proposal_key"],
                "violations": [f"worktree_apply_error:{exc}"],
            }
        finally:
            if wt_path is not None:
                self._cleanup_worktree(wt_path, used_git)

        if screened["stage"] == ProposalStage.STAGE1_FAILED.value:
            violations = list((screened.get("stage1_report") or {}).get("violations") or [])
            await self.state_repo.log_event(
                "l2_bridge.stage1_failed",
                level="warning",
                message=f"L2 proposal {prop['proposal_key']} failed Stage 1 constitutional guard",
                payload={"proposal_key": prop["proposal_key"], "target_file": target_file, "violations": violations},
            )
            await self._update_bridge_state(
                {
                    "last_proposal_key": prop["proposal_key"],
                    "last_stage": ProposalStage.STAGE1_FAILED.value,
                    "last_target_file": target_file,
                    "last_violations": violations,
                }
            )
            return {
                "ok": False,
                "stage": ProposalStage.STAGE1_FAILED.value,
                "proposal_id": str(prop["id"]),
                "proposal_key": prop["proposal_key"],
                "violations": violations,
            }

        # Stage 1 PASSED -> Emit Stage 2 Confirmation Card via HumanBridge
        stage2_card_id: str | None = None
        diff_snippet = (diff_text.strip() or "(no textual diff)")[:700]
        rb_plan = str(meta.get("rollback_plan") or "保留 .bak 备份文件，可一键恢复原配置")[:200]
        if self.human_bridge is not None and getattr(self.human_bridge, "enabled", False):
            msg = (
                f"已完成隔离工作区修改与四层宪法校验（通过），准备变更 `{target_file}`：\n\n"
                f"```diff\n{diff_snippet}\n```\n\n"
                f"回滚方案：{rb_plan}"
            )
            res = await self.human_bridge.maybe_send_outreach(
                kind="ask_human",
                topic_key=f"l2_stage2:{prop['proposal_key']}",
                title=f"L2 变更二次确认：{target_file}"[:160],
                message=msg[:1450],
                reason=f"提案 {prop['proposal_key']} 已通过宪法 4 层隔离验证",
                expected_benefit=str(card_val.get("expected_benefit") or hypothesis)[:300],
                options=[
                    {"label": "批准生效(推荐)", "instruction": f"批准 {prop['proposal_key']} 变更生效"},
                    {"label": "仅留记录暂不应用", "instruction": f"暂不应用 {prop['proposal_key']}，仅保留审阅"},
                    {"label": "作废关闭", "instruction": f"拒绝并关闭 L2 提案 {prop['proposal_key']}"},
                ],
                force=True,
                extra_metadata={
                    "bridge_stage": "stage2_confirm",
                    "proposal_id": str(prop["id"]),
                    "proposal_key": prop["proposal_key"],
                    "target_file": target_file,
                    "source_card_id": card_id,
                },
            )
            if isinstance(res, dict) and res.get("sent"):
                stage2_card_id = res.get("card_id")

        await self.state_repo.log_event(
            "l2_bridge.stage1_passed",
            level="info",
            message=f"L2 proposal {prop['proposal_key']} passed Stage 1 and awaits Stage 2 confirmation",
            payload={
                "proposal_id": str(prop["id"]),
                "proposal_key": prop["proposal_key"],
                "target_file": target_file,
                "stage2_card_id": stage2_card_id,
            },
        )
        await self._update_bridge_state(
            {
                "last_proposal_id": str(prop["id"]),
                "last_proposal_key": prop["proposal_key"],
                "last_stage": ProposalStage.STAGE2_PENDING_APPROVAL.value,
                "last_target_file": target_file,
                "last_stage2_card_id": stage2_card_id,
                "last_violations": [],
            }
        )
        return {
            "ok": True,
            "stage": ProposalStage.STAGE2_PENDING_APPROVAL.value,
            "proposal_id": str(prop["id"]),
            "proposal_key": prop["proposal_key"],
            "target_file": target_file,
            "diff_preview": diff_text[:4000],
            "stage2_card_id": stage2_card_id,
        }

    async def on_stage2_decision(
        self,
        *,
        card_id: str,
        option_index: int,
        card_val: dict[str, Any],
        sender: str = "joel",
    ) -> dict[str, Any]:
        """Handle Joel's decision on a Stage 2 confirmation card (approve / retain / reject)."""
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "l2_evolution_bridge_disabled"}

        meta = card_val.get("meta") if isinstance(card_val.get("meta"), dict) else {}
        prop_ref = str(meta.get("proposal_id") or meta.get("proposal_key") or "").strip()
        target_file = str(meta.get("target_file") or "").strip().lstrip("./")
        if not prop_ref:
            return {"ok": False, "reason": "missing_proposal_ref"}

        if option_index == 0:
            approved = await self.proposal_manager.approve_proposal(
                prop_ref,
                approver=sender or "joel",
                note=f"Stage 2 approved via HumanBridge card {card_id}",
            )
            delta = approved.get("genome_delta") if isinstance(approved.get("genome_delta"), dict) else {}
            overrides = delta.get("file_overrides") if isinstance(delta.get("file_overrides"), dict) else {}
            override_text = overrides.get(target_file)
            diff_preview = str(delta.get("diff_preview") or "")
            apply_mode = "metadata_only"
            artifact_path: str | None = None
            backup_path: str | None = None

            live_target = (self.repo_root / target_file).resolve() if target_file else None
            if (
                target_file in _WRITABLE_YAML_ALLOWLIST
                and isinstance(override_text, str)
                and live_target is not None
                and self.repo_root in live_target.parents
                and live_target.is_file()
                and os.access(live_target, os.W_OK)
                and os.access(live_target.parent, os.W_OK)
            ):
                bak = live_target.with_name(f"{live_target.name}.bak.{approved['proposal_key']}")
                shutil.copy2(live_target, bak)
                live_target.write_text(override_text, encoding="utf-8")
                apply_mode = "applied_with_backup"
                backup_path = str(bak)
                artifact_path = str(live_target)
            elif target_file:
                approved_dir = self._resolve_worktrees_root() / "approved"
                approved_dir.mkdir(parents=True, exist_ok=True)
                patch_file = approved_dir / f"{approved['proposal_key']}.patch"
                patch_file.write_text(diff_preview or f"# target: {target_file}\n", encoding="utf-8")
                apply_mode = "patch_artifact_ready"
                artifact_path = str(patch_file)

            await self.state_repo.log_event(
                "l2_bridge.stage2_approved",
                level="info",
                message=f"L2 proposal {approved['proposal_key']} approved ({apply_mode})",
                payload={
                    "proposal_key": approved["proposal_key"],
                    "target_file": target_file,
                    "apply_mode": apply_mode,
                    "artifact_path": artifact_path,
                    "backup_path": backup_path,
                    "approver": sender,
                },
            )
            await self._update_bridge_state(
                {
                    "last_proposal_key": approved["proposal_key"],
                    "last_stage": ProposalStage.STAGE2_APPROVED.value,
                    "last_target_file": target_file,
                    "last_apply_mode": apply_mode,
                    "last_artifact_path": artifact_path,
                }
            )
            return {
                "ok": True,
                "stage": ProposalStage.STAGE2_APPROVED.value,
                "proposal_key": approved["proposal_key"],
                "apply_mode": apply_mode,
                "artifact_path": artifact_path,
                "backup_path": backup_path,
            }

        if option_index == 1:
            await self.state_repo.log_event(
                "l2_bridge.stage2_retained",
                level="info",
                message=f"L2 proposal {prop_ref} retained for manual review",
                payload={"proposal_ref": prop_ref, "target_file": target_file, "sender": sender},
            )
            await self._update_bridge_state(
                {
                    "last_proposal_key": meta.get("proposal_key") or prop_ref,
                    "last_stage": "RETAINED_FOR_REVIEW",
                    "last_target_file": target_file,
                }
            )
            return {"ok": True, "stage": "RETAINED_FOR_REVIEW", "proposal_id": prop_ref}

        rejected = await self.proposal_manager.reject_proposal(
            prop_ref,
            approver=sender or "joel",
            note=f"Stage 2 rejected via HumanBridge card {card_id}",
        )
        await self.state_repo.log_event(
            "l2_bridge.stage2_rejected",
            level="info",
            message=f"L2 proposal {rejected['proposal_key']} rejected by {sender}",
            payload={"proposal_key": rejected["proposal_key"], "target_file": target_file, "approver": sender},
        )
        await self._update_bridge_state(
            {
                "last_proposal_key": rejected["proposal_key"],
                "last_stage": ProposalStage.STAGE2_REJECTED.value,
                "last_target_file": target_file,
            }
        )
        return {"ok": True, "stage": ProposalStage.STAGE2_REJECTED.value, "proposal_key": rejected["proposal_key"]}
