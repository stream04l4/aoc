"""External Self-Evolving Playbook Manager (V4.0 M3).

Strictly lives OUTSIDE the immutable kernel (`app/core/`, `app/drives/`, `app/affect/`).
Allows AOC to autonomously accumulate, version, verify, and refine external cognitive/task
playbooks per domain (e.g., `coding`, `curiosity_external`, `research`, `debugging`),
subject to strict safety invariants:
  - NEVER touches AOC Core source code, Governance policies, or L3 permissions.
  - Every playbook update passes deterministic Prompt-Injection & safety scanning.
  - Versioned markdown files stored outside kernel (`<workspace_root>/playbooks/<domain>/v<N>.md`)
    with SHA-256 checksums indexed in `digital_world_state(kind='external_playbook')` (0 new tables).
  - Tracks empirical post-adoption success rate & verified IG; automatically rolls back to
    `parent_version` if a newly evolved playbook degrades task performance!
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.perception.world.security import detect_prompt_injection
from app.storage.repository import StateRepository

logger = get_logger("aoc.skills.playbooks")

_FORBIDDEN_PLAYBOOK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("core_source_edit", re.compile(r"(modify|edit|patch|overwrite)\s+.{0,30}(app/core|app/governance|app/drives|loop\.py|policy\.py)", re.I)),
    ("governance_bypass", re.compile(r"(bypass|disable|ignore)\s+.{0,20}(governance|preflight|l3|approval|budget)", re.I)),
    ("danger_sandbox", re.compile(r"danger-full-access|sudo\s+rm|chmod\s+777", re.I)),
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan_playbook_safety(content: str) -> list[str]:
    """Deterministic safety + prompt-injection scanner for autonomous Playbook edits."""
    text = (content or "").strip()
    if not text:
        return ["empty_playbook"]
    violations: list[str] = []
    inj = detect_prompt_injection(text)
    for flag in inj:
        violations.append(f"prompt_injection:{flag}")
    for tag, pat in _FORBIDDEN_PLAYBOOK_PATTERNS:
        if pat.search(text):
            violations.append(f"forbidden_playbook_directive:{tag}")
    return list(dict.fromkeys(violations))


class PlaybookManager:
    """Versioned external strategy playbooks with SHA-256 verification and automatic degradation rollback."""

    def __init__(
        self,
        *,
        repo: StateRepository,
        storage_dir: str | Path,
        max_chars: int = 2000,
        max_versions_per_domain: int = 20,
        rollback_min_samples: int = 3,
        rollback_max_drop: float = 0.20,
    ) -> None:
        self.repo = repo
        self.storage_dir = Path(storage_dir).resolve()
        self.max_chars = max(200, int(max_chars))
        self.max_versions_per_domain = max(2, int(max_versions_per_domain))
        self.rollback_min_samples = max(2, int(rollback_min_samples))
        self.rollback_max_drop = max(0.05, float(rollback_max_drop))

    @staticmethod
    def _norm_domain(domain: str) -> str:
        slug = re.sub(r"[^a-z0-9_]+", "_", str(domain or "general").strip().lower()).strip("_")[:48]
        return slug or "general"

    async def _load_record(self, domain: str) -> dict[str, Any] | None:
        dom = self._norm_domain(domain)
        items = await self.repo.list_world_items(kind="external_playbook")
        return next((x for x in items if x.get("key") == dom), None)

    async def save_revision(
        self,
        *,
        domain: str,
        content: str,
        change_summary: str = "",
        source: str = "autonomous_evolution",
    ) -> dict[str, Any]:
        """Validate, persist, and activate a new versioned external Playbook revision."""
        dom = self._norm_domain(domain)
        clean_content = (content or "").strip()[: self.max_chars]
        violations = scan_playbook_safety(clean_content)
        if violations:
            return {"saved": False, "rejected_unsafe": True, "domain": dom, "violations": violations}

        existing = await self._load_record(dom)
        val = dict(existing.get("value") or {}) if existing and isinstance(existing.get("value"), dict) else {}
        versions: list[dict[str, Any]] = list(val.get("versions") or [])
        active_ver = int(val.get("active_version") or 0)

        if len(versions) >= self.max_versions_per_domain:
            return {
                "saved": False,
                "domain": dom,
                "reason": f"max_versions_reached ({len(versions)}/{self.max_versions_per_domain})",
            }

        # Avoid duplicate no-op revisions
        checksum = _sha256_text(clean_content)
        cur_entry = next((v for v in versions if int(v.get("version", 0)) == active_ver), None)
        if cur_entry is not None and cur_entry.get("checksum") == checksum:
            return {"saved": False, "domain": dom, "version": active_ver, "reason": "unchanged_content"}

        next_ver = (max((int(v.get("version", 0)) for v in versions), default=0)) + 1
        dom_dir = (self.storage_dir / dom).resolve()
        if self.storage_dir not in dom_dir.parents and dom_dir != self.storage_dir:
            raise PermissionError("Playbook path escapes storage_dir")
        dom_dir.mkdir(parents=True, exist_ok=True)
        file_path = dom_dir / f"v{next_ver}.md"
        file_path.write_text(clean_content + "\n", encoding="utf-8")

        now_iso = utcnow().isoformat()
        new_entry = {
            "version": next_ver,
            "parent_version": active_ver if active_ver > 0 else None,
            "file_path": str(file_path),
            "checksum": checksum,
            "change_summary": (change_summary or "Autonomous playbook refinement")[:300],
            "samples": 0,
            "successes": 0,
            "ig_sum": 0.0,
            "created_at": now_iso,
            "status": "active",
        }
        for v in versions:
            if v.get("status") == "active":
                v["status"] = "superseded"
        versions.append(new_entry)

        new_val = {
            "domain": dom,
            "active_version": next_ver,
            "versions": versions[-self.max_versions_per_domain :],
            "updated_at": now_iso,
        }
        await self.repo.upsert_world_item(
            kind="external_playbook",
            key=dom,
            value=new_val,
            status="active",
            source=source,
        )
        await self.repo.log_event(
            "playbook.revision_saved",
            message=f"Saved external playbook '{dom}' v{next_ver}: {new_entry['change_summary']}",
            payload={"domain": dom, "version": next_ver, "parent_version": new_entry["parent_version"]},
        )
        return {
            "saved": True,
            "domain": dom,
            "version": next_ver,
            "parent_version": new_entry["parent_version"],
            "file_path": str(file_path),
            "checksum": checksum,
        }

    async def get_active_playbook(self, domain: str) -> dict[str, Any] | None:
        """Load active playbook content after verifying SHA-256 integrity on disk."""
        dom = self._norm_domain(domain)
        rec = await self._load_record(dom)
        if rec is None or not isinstance(rec.get("value"), dict):
            return None
        val = rec["value"]
        active_ver = int(val.get("active_version") or 0)
        versions = list(val.get("versions") or [])
        entry = next((v for v in versions if int(v.get("version", 0)) == active_ver), None)
        if entry is None:
            return None
        fp = Path(str(entry.get("file_path") or ""))
        if not fp.is_file():
            return None
        text = fp.read_text(encoding="utf-8", errors="replace").strip()
        if _sha256_text(text) != entry.get("checksum"):
            logger.warning("playbook.integrity_mismatch", domain=dom, version=active_ver)
            return None
        samples = int(entry.get("samples") or 0)
        successes = int(entry.get("successes") or 0)
        return {
            "domain": dom,
            "version": active_ver,
            "parent_version": entry.get("parent_version"),
            "content": text,
            "change_summary": entry.get("change_summary", ""),
            "samples": samples,
            "success_rate": round(successes / samples, 4) if samples > 0 else None,
            "avg_ig": round(float(entry.get("ig_sum") or 0.0) / samples, 4) if samples > 0 else None,
            "updated_at": val.get("updated_at"),
        }

    async def list_playbooks_for_context(
        self, *, domains: list[str] | None = None, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Return compact integrity-verified active playbooks for CognitiveContext injection."""
        items = await self.repo.list_world_items(kind="external_playbook")
        wanted = {self._norm_domain(d) for d in (domains or []) if d}
        out: list[dict[str, Any]] = []
        # Prefer explicitly matching domains first, then general fallback
        ordered = sorted(
            items,
            key=lambda x: (0 if x.get("key") in wanted else 1, str(x.get("key") or "")),
        )
        for item in ordered:
            dom = str(item.get("key") or "")
            if wanted and dom not in wanted and dom != "general":
                continue
            pb = await self.get_active_playbook(dom)
            if pb is None:
                continue
            out.append(
                {
                    "domain": pb["domain"],
                    "version": pb["version"],
                    "content": pb["content"][:800],
                    "samples": pb["samples"],
                    "success_rate": pb["success_rate"],
                }
            )
            if len(out) >= limit:
                break
        return out

    async def record_outcome_and_check_rollback(
        self,
        *,
        domain: str,
        succeeded: bool,
        verified_ig: float = 0.0,
    ) -> dict[str, Any]:
        """Record empirical task outcome against active playbook and auto-rollback if degraded."""
        dom = self._norm_domain(domain)
        rec = await self._load_record(dom)
        if rec is None or not isinstance(rec.get("value"), dict):
            return {"tracked": False, "domain": dom}

        val = dict(rec["value"])
        active_ver = int(val.get("active_version") or 0)
        versions: list[dict[str, Any]] = [dict(v) for v in (val.get("versions") or [])]
        cur = next((v for v in versions if int(v.get("version", 0)) == active_ver), None)
        if cur is None:
            return {"tracked": False, "domain": dom}

        cur["samples"] = int(cur.get("samples") or 0) + 1
        if succeeded:
            cur["successes"] = int(cur.get("successes") or 0) + 1
        cur["ig_sum"] = round(float(cur.get("ig_sum") or 0.0) + max(0.0, float(verified_ig or 0.0)), 4)

        rolled_back = False
        restored_version: int | None = None
        cur_samples = int(cur["samples"])
        cur_rate = int(cur["successes"]) / max(1, cur_samples)
        parent_ver = cur.get("parent_version")

        if cur_samples >= self.rollback_min_samples and parent_ver:
            parent = next((v for v in versions if int(v.get("version", 0)) == int(parent_ver)), None)
            if parent is not None:
                p_samples = int(parent.get("samples") or 0)
                p_rate = (int(parent.get("successes") or 0) / p_samples) if p_samples > 0 else 0.70
                degraded = (cur_rate < p_rate - self.rollback_max_drop) or (cur_rate < 0.50 and p_rate >= 0.70)
                if degraded:
                    # Verify parent artifact integrity before restoring
                    pfp = Path(str(parent.get("file_path") or ""))
                    if pfp.is_file() and _sha256_text(pfp.read_text(encoding="utf-8", errors="replace").strip()) == parent.get("checksum"):
                        cur["status"] = "rolled_back"
                        cur["rolled_back_at"] = utcnow().isoformat()
                        cur["rollback_reason"] = f"success_rate_degraded:{cur_rate:.2f}<parent:{p_rate:.2f}"
                        parent["status"] = "active"
                        val["active_version"] = int(parent["version"])
                        rolled_back = True
                        restored_version = int(parent["version"])
                        await self.repo.log_event(
                            "playbook.auto_rolled_back",
                            message=f"Auto-rolled back playbook '{dom}' v{active_ver} -> v{restored_version} (success {cur_rate:.2f} vs parent {p_rate:.2f})",
                            payload={"domain": dom, "from_version": active_ver, "to_version": restored_version},
                            level="warning",
                        )

        val["versions"] = versions
        val["updated_at"] = utcnow().isoformat()
        await self.repo.upsert_world_item(
            kind="external_playbook",
            key=dom,
            value=val,
            status="active",
            source=rec.get("source") or "autonomous_evolution",
        )
        return {
            "tracked": True,
            "domain": dom,
            "version": active_ver,
            "samples": cur_samples,
            "success_rate": round(cur_rate, 4),
            "rolled_back": rolled_back,
            "restored_version": restored_version,
        }

    async def rollback_to_parent(
        self,
        domain: str,
        *,
        reason: str = "meta_reflection_regression",
    ) -> dict[str, Any]:
        """Explicitly roll back an active Playbook domain to its SHA-256 verified parent_version (V5.3 M3)."""
        dom = self._norm_domain(domain)
        rec = await self._load_record(dom)
        if rec is None or not isinstance(rec.get("value"), dict):
            return {"rolled_back": False, "domain": dom, "reason": "playbook_not_found"}

        val = dict(rec["value"])
        active_ver = int(val.get("active_version") or 0)
        versions: list[dict[str, Any]] = [dict(v) for v in (val.get("versions") or [])]
        cur = next((v for v in versions if int(v.get("version", 0)) == active_ver), None)
        if cur is None:
            return {"rolled_back": False, "domain": dom, "reason": "active_version_missing"}

        parent_ver = cur.get("parent_version")
        if not parent_ver:
            return {"rolled_back": False, "domain": dom, "reason": "no_parent_version"}

        parent = next((v for v in versions if int(v.get("version", 0)) == int(parent_ver)), None)
        if parent is None:
            return {"rolled_back": False, "domain": dom, "reason": "parent_entry_not_found"}

        pfp = Path(str(parent.get("file_path") or ""))
        if not pfp.is_file():
            return {"rolled_back": False, "domain": dom, "reason": "parent_file_missing"}
        if _sha256_text(pfp.read_text(encoding="utf-8", errors="replace").strip()) != parent.get("checksum"):
            return {"rolled_back": False, "domain": dom, "reason": "parent_checksum_mismatch"}

        cur["status"] = "rolled_back"
        cur["rolled_back_at"] = utcnow().isoformat()
        cur["rollback_reason"] = str(reason or "meta_reflection_regression")[:200]
        parent["status"] = "active"
        val["active_version"] = int(parent["version"])
        val["versions"] = versions
        val["updated_at"] = utcnow().isoformat()
        await self.repo.upsert_world_item(
            kind="external_playbook",
            key=dom,
            value=val,
            status="active",
            source=rec.get("source") or "autonomous_evolution",
        )
        await self.repo.log_event(
            "playbook.rolled_back",
            level="warning",
            message=f"Rolled back playbook '{dom}' v{active_ver} -> v{parent['version']}: {reason}",
            payload={
                "domain": dom,
                "from_version": active_ver,
                "to_version": int(parent["version"]),
                "reason": str(reason or "")[:200],
            },
        )
        return {
            "rolled_back": True,
            "domain": dom,
            "from_version": active_ver,
            "to_version": int(parent["version"]),
            "reason": str(reason or "")[:200],
        }
