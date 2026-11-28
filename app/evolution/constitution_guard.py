"""Four-layer Constitutional Guard for AOC V5.0 Stage 1 screening (zero production tokens).

Enforces the exact split between the ultra-thin immutable Constitutional Core
(safety, governance L0-L3, SSRF/PI guards, human kill-switch, core drive triad + survival override)
and the evolvable Genome (cognitive heuristics, skills, retrieval, and drive evaluation metrics).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from app.core.config import PROJECT_ROOT
from app.drives.instinct_guard import validate_drive_config_bounds
from app.evolution.interfaces import ConstitutionLayerResult, ConstitutionVerdict

CONSTITUTIONAL_FILES: tuple[str, ...] = (
    "config/policy.yaml",
    "app/governance/policy.py",
    "app/governance/approval.py",
    "app/governance/interfaces.py",
    "app/governance/semantic_policy.py",
    "app/perception/world/security.py",
    "app/core/enums.py",
    "app/core/lifecycle.py",
    "app/api/auth.py",
    "app/drives/interfaces.py",
    "app/drives/engine.py",
    "app/drives/instinct_guard.py",
    "app/evolution/constitution_guard.py",
    "app/evolution/spawner.py",
    "app/evolution/gc.py",
    "app/evolution/blank_slate.py",
    "app/evolution/benchmarks.py",
    "app/evolution/scoring.py",
    "app/evolution/arena.py",
    "app/evolution/succession.py",
)

EVOLVABLE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Evolvable drive evaluators, scorers, dynamics, arbitration & metric config
        "app/drives/survival.py",
        "app/drives/reproduction.py",
        "app/drives/curiosity.py",
        "app/drives/calculator.py",
        "app/drives/scoring.py",
        "app/drives/arbitration.py",
        "config/drives.yaml",
        # Cognition & memory retrieval
        "app/cognition/brain.py",
        "app/cognition/context_compactor.py",
        "app/cognition/goal_dedup.py",
        "app/cognition/preflight.py",
        "app/cognition/interfaces.py",
        "app/memory/retrieval.py",
        "app/memory/info_gain.py",
        "app/memory/experience_manager.py",
        "config/memory.yaml",
        # External skills, playbooks, exploration & orchestration heuristics
        "app/skills/evolution.py",
        "app/skills/playbooks.py",
        "app/skills/runtime.py",
        "app/perception/world/exploration.py",
        "app/perception/world/investigation.py",
        "app/perception/world/pipeline.py",
        "app/orchestration/orchestrator.py",
        "app/orchestration/finalizer.py",
    }
)

WIDE_OPEN_LOCKED_FILES: tuple[str, ...] = (
    "app/api/auth.py",
    "app/core/lifecycle.py",
)

FORBIDDEN_SECRET_YAML_KEYS: frozenset[str] = frozenset(
    {
        "admin_key",
        "api_key",
        "bot_token",
        "webhook_secret",
        "internal_token",
        "auth_enabled",
        "secret",
        "password",
    }
)

_WIDE_OPEN_LOCKED_DIRS: frozenset[str] = frozenset({".git", ".ssh", ".venv", ".cache", "backups"})
_WIDE_OPEN_SECRET_EXACT_NAMES: frozenset[str] = frozenset({".env", ".git-credentials", "auth.json"})
_WIDE_OPEN_PY_DANGER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"auth_enabled\s*=\s*False\b"), "auth_disable_forbidden"),
    (re.compile(r"""(?:open|Path)\([^)]*['"]\.env(?:\.[^'"]+)?['"]"""), "env_secret_file_access_forbidden"),
)


def _strip_rel_prefix(raw_path: str) -> str:
    text = str(raw_path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def is_hard_locked_path(raw_path: str) -> tuple[bool, str]:
    """Return (locked, reason) for paths that can NEVER be edited even in wide_open mode."""
    text = str(raw_path or "").strip()
    if not text:
        return True, "empty_path"
    if text.startswith("/") or "\\" in text:
        return True, "unsafe_path_syntax"
    cleaned = _strip_rel_prefix(text)
    parts = [p for p in cleaned.split("/") if p]
    if not parts or ".." in parts:
        return True, "path_traversal_forbidden"
    norm = "/".join(parts)
    if norm in WIDE_OPEN_LOCKED_FILES:
        return True, f"hard_locked_file_forbidden:{norm}"
    if parts[0] in _WIDE_OPEN_LOCKED_DIRS:
        return True, f"locked_directory_forbidden:{parts[0]}"
    base = parts[-1].lower()
    if (
        base in _WIDE_OPEN_SECRET_EXACT_NAMES
        or base.startswith(".env.")
        or base.startswith("id_rsa")
        or base.endswith(".pem")
        or base.endswith(".key")
    ):
        return True, f"secret_credential_file_forbidden:{norm}"
    return False, ""


def _scan_yaml_forbidden_keys(obj: Any, prefix: str = "") -> list[str]:
    violations: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).strip().lower()
            full = f"{prefix}.{kl}" if prefix else kl
            if kl in FORBIDDEN_SECRET_YAML_KEYS:
                violations.append(f"forbidden_secret_yaml_key:{full}")
            violations.extend(_scan_yaml_forbidden_keys(v, prefix=full))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            violations.extend(_scan_yaml_forbidden_keys(item, prefix=f"{prefix}[{idx}]"))
    return violations

DRIVE_EVOLVABLE_FILES: frozenset[str] = frozenset(
    {
        "app/drives/survival.py",
        "app/drives/reproduction.py",
        "app/drives/curiosity.py",
        "app/drives/calculator.py",
        "app/drives/scoring.py",
        "app/drives/arbitration.py",
        "config/drives.yaml",
    }
)

_FORBIDDEN_DRIVE_IMPORT_PREFIXES = (
    "httpx",
    "urllib",
    "requests",
    "aiohttp",
    "socket",
    "subprocess",
    "shutil",
    "ctypes",
    "importlib",
    "openai",
    "anthropic",
    "asyncio",
    "multiprocessing",
    "threading",
    "app.cognition",
    "app.llm",
    "app.governance",
    "app.evolution",
    "app.notifications",
    "app.runner",
)

_FORBIDDEN_DRIVE_CALLS = frozenset({"eval", "exec", "compile", "__import__", "setattr", "delattr", "open", "system", "popen"})
_DIFF_FILE_RE = re.compile(r"^(?:\+\+\+|---)\s+[ab]/([^\n\t]+)", re.M)

# Subprocess test runner executed in complete isolation against overlayed candidate app/ & config/
_BEHAVIOR_PROBE_SCRIPT = r"""
import json, sys
from datetime import datetime, timedelta, timezone
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveContext, KnowledgeGap
from app.perception.interfaces import Observation

T0 = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)

def make_obs(**sys_over):
    sys_state = {
        "disk": {"free_ratio": 0.65, "paths": {"/": {"free_ratio": 0.65}}},
        "memory": {"available_ratio": 0.70},
        "cpu": {"load_per_core": 0.3},
        "llm": {"available": True, "latency_ms": 500},
        "database": {"ok": True},
        "services": {"postgres": {"ok": True}},
    }
    sys_state.update(sys_over)
    return Observation(
        timestamp=T0,
        system_state=sys_state,
        environment_state={"unknown_topics": []},
        available_resources={
            "agents": {"enabled_count": 2, "healthy_count": 2, "capabilities": ["coding", "research"]},
            "budget": {"tasks_used": 1, "tasks_limit": 100, "tokens_used": 1000, "tokens_limit": 1000000},
        },
        confidence=1.0,
    )

def make_ctx(obs, *, gaps=(), entropy=None, skill_count=10):
    return DriveContext(
        now=T0,
        observation=obs,
        recent_execution_statuses=["SUCCEEDED"] * 5,
        budget={"tasks_used": 1, "tasks_limit": 100, "tokens_used": 1000, "tokens_limit": 1000000},
        capabilities_acquired=["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"],
        validated_capabilities=["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"],
        validated_skill_count=skill_count,
        procedural_skill_count=skill_count,
        capability_goals_completed_recent=5,
        memory_available=True,
        identity_persisted=True,
        open_knowledge_gaps=list(gaps),
        unknown_topics=[],
        exploration_attempted_recent=3,
        exploration_succeeded_recent=3,
        new_knowledge_recent=0 if gaps else 5,
        last_learning_at=T0 - (timedelta(hours=48) if gaps else timedelta(minutes=5)),
        last_information_gain=0.0 if gaps else 0.9,
        last_information_gain_at=T0 - timedelta(minutes=5),
        entropy_metrics=dict(entropy or {}),
    )

eng = DriveEngine()
assert set(eng.evaluators.keys()) == {"survival", "reproduction", "curiosity"}, "missing core evaluators"

# 1. Healthy baseline: valid normalized states + closure
s_ok = eng.compute_states(make_ctx(make_obs()), {})
for k, st in s_ok.items():
    assert 0.0 <= st.satisfaction <= 1.0 and 0.0 <= st.urgency <= 1.0 and 0.0 <= st.confidence <= 1.0, k
    assert abs(st.deficit - (1.0 - st.satisfaction)) <= 1e-4, k

# 2. Disk critical (<5%) even with maximum curiosity deficit + recent survival satisfaction -> survival MUST override
gaps = [KnowledgeGap(topic=f"gap-{i}", importance=1.0) for i in range(5)]
s_disk = eng.compute_states(make_ctx(make_obs(disk={"free_ratio": 0.02}), gaps=gaps, skill_count=0), {})
s_disk["survival"] = s_disk["survival"].model_copy(update={"last_satisfied_at": T0 - timedelta(seconds=1)})
d_disk = eng.arbitrate(s_disk, now=T0)
assert d_disk.selected_drive == "survival", f"disk crisis failed: {d_disk.selected_drive}"
assert d_disk.constraints.get("allow_only_survival") is True
assert d_disk.scores["survival"]["inhibition_factor"] == 1.0

# 3. LLM unavailable -> survival MUST override
s_llm = eng.compute_states(make_ctx(make_obs(llm={"available": False}), gaps=gaps), {})
d_llm = eng.arbitrate(s_llm, now=T0)
assert d_llm.selected_drive == "survival", f"llm outage failed: {d_llm.selected_drive}"
assert d_llm.constraints.get("emergency_mode") is True

# 4. Database unavailable -> survival MUST override
s_db = eng.compute_states(make_ctx(make_obs(database={"ok": False}), gaps=gaps), {})
d_db = eng.arbitrate(s_db, now=T0)
assert d_db.selected_drive == "survival", f"db outage failed: {d_db.selected_drive}"
assert d_db.constraints.get("emergency_mode") is True

# 5. Healthy survival + high curiosity/reproduction gap -> non-survival drive MUST be able to compete
s_comp = eng.compute_states(make_ctx(make_obs(), gaps=gaps, entropy={"skill_deprecation_ratio": 0.9}, skill_count=1), {})
d_comp = eng.arbitrate(s_comp, now=T0)
assert d_comp.selected_drive in {"curiosity", "reproduction"}, f"non-survival drives starved: {d_comp.selected_drive}"

print(json.dumps({"ok": True, "comp_winner": d_comp.selected_drive}))
"""


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _normalize_rel_path(raw: str) -> str:
    raw_s = raw.strip()
    if not raw_s or raw_s.startswith("/") or "\\" in raw_s:
        raise ValueError(f"Unsafe relative path in genome_delta: {raw!r}")
    cleaned = _strip_rel_prefix(raw_s)
    if not cleaned or ".." in Path(cleaned).parts:
        raise ValueError(f"Unsafe relative path in genome_delta: {raw!r}")
    return cleaned


class ConstitutionGuard:
    """Four-layer deterministic gatekeeper for V5.0 Stage 1 screening (zero production tokens)."""

    def __init__(
        self,
        repo_root: str | Path = PROJECT_ROOT,
        *,
        max_changed_files: int = 12,
        max_net_loc_delta: int = 800,
        policy_mode: str = "strict",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.max_changed_files = max_changed_files
        self.max_net_loc_delta = max_net_loc_delta
        self.policy_mode = "wide_open" if str(policy_mode or "").strip().lower() == "wide_open" else "strict"
        self.baseline_hashes: dict[str, str] = {}
        for rel in CONSTITUTIONAL_FILES:
            digest = _sha256_file(self.repo_root / rel)
            if digest is not None:
                self.baseline_hashes[rel] = digest
        self.wide_open_hashes: dict[str, str] = {}
        for rel in WIDE_OPEN_LOCKED_FILES:
            digest = _sha256_file(self.repo_root / rel)
            if digest is not None:
                self.wide_open_hashes[rel] = digest

    def constitutional_manifest(self) -> dict[str, Any]:
        """Return baseline constitutional SHA-256 hashes and evolvable file allowlist."""
        return {
            "policy_mode": self.policy_mode,
            "constitutional_file_count": len(self.baseline_hashes),
            "constitutional_files": dict(self.baseline_hashes),
            "wide_open_locked_files": list(WIDE_OPEN_LOCKED_FILES),
            "evolvable_allowlist": sorted(EVOLVABLE_ALLOWLIST),
            "drive_evolvable_files": sorted(DRIVE_EVOLVABLE_FILES),
            "limits": {
                "max_changed_files": self.max_changed_files,
                "max_net_loc_delta": self.max_net_loc_delta,
            },
        }

    def _extract_changed_files_and_overrides(
        self, genome_delta: dict[str, Any]
    ) -> tuple[list[str], dict[str, str], list[str]]:
        errors: list[str] = []
        raw_overrides = genome_delta.get("file_overrides") or {}
        if not isinstance(raw_overrides, dict):
            return [], {}, ["genome_delta.file_overrides must be a mapping"]

        overrides: dict[str, str] = {}
        seen: set[str] = set()

        for raw_p in genome_delta.get("files") or []:
            try:
                seen.add(_normalize_rel_path(str(raw_p)))
            except ValueError as exc:
                errors.append(str(exc))

        for raw_p, content in raw_overrides.items():
            try:
                norm = _normalize_rel_path(str(raw_p))
            except ValueError as exc:
                errors.append(str(exc))
                continue
            seen.add(norm)
            if not isinstance(content, str):
                errors.append(f"file_overrides[{norm}] must be string source text")
            else:
                overrides[norm] = content

        diff_text = str(genome_delta.get("unified_diff") or "")
        for m in _DIFF_FILE_RE.findall(diff_text):
            if m == "/dev/null":
                continue
            try:
                seen.add(_normalize_rel_path(m))
            except ValueError as exc:
                errors.append(str(exc))

        return sorted(seen), overrides, errors

    def _check_layer1(
        self,
        changed_files: list[str],
        overrides: dict[str, str],
        parse_errors: list[str],
        candidate_root: Path | None,
    ) -> ConstitutionLayerResult:
        violations = list(parse_errors)
        if not changed_files:
            violations.append("empty genome_delta: at least one evolvable file must be declared")

        if len(changed_files) > self.max_changed_files:
            violations.append(
                f"too many modified files ({len(changed_files)} > limit {self.max_changed_files})"
            )

        if self.policy_mode == "wide_open":
            for rel in changed_files:
                locked, lock_reason = is_hard_locked_path(rel)
                if locked:
                    violations.append(lock_reason)
        else:
            const_set = set(CONSTITUTIONAL_FILES)
            for rel in changed_files:
                if rel in const_set:
                    violations.append(f"forbidden constitutional file modification: {rel}")
                elif rel not in EVOLVABLE_ALLOWLIST:
                    violations.append(f"file outside EVOLVABLE_ALLOWLIST: {rel}")

        net_loc_delta = 0
        for rel, new_text in overrides.items():
            base_file = self.repo_root / rel
            old_lines = len(base_file.read_text(encoding="utf-8").splitlines()) if base_file.is_file() else 0
            new_lines = len(new_text.splitlines())
            net_loc_delta += abs(new_lines - old_lines)
        if net_loc_delta > self.max_net_loc_delta:
            violations.append(
                f"net LOC delta {net_loc_delta} exceeds max_net_loc_delta={self.max_net_loc_delta}"
            )

        if candidate_root is not None:
            cand_root_r = candidate_root.resolve()
            hashes_to_check = self.wide_open_hashes if self.policy_mode == "wide_open" else self.baseline_hashes
            for rel, expected_hash in hashes_to_check.items():
                actual_hash = _sha256_file(cand_root_r / rel)
                if actual_hash != expected_hash:
                    violations.append(f"constitutional file hash mismatch in candidate_root: {rel}")

        return ConstitutionLayerResult(
            layer="layer1_path_and_hash",
            passed=len(violations) == 0,
            violations=violations,
            details={"changed_files": changed_files, "net_loc_delta": net_loc_delta, "policy_mode": self.policy_mode},
        )

    def _check_layer2_ast(
        self,
        changed_files: list[str],
        overrides: dict[str, str],
        candidate_root: Path | None,
    ) -> ConstitutionLayerResult:
        violations: list[str] = []
        inspected: list[str] = []

        for rel in changed_files:
            if not rel.endswith(".py"):
                continue
            source: str | None = overrides.get(rel)
            if source is None and candidate_root is not None and (candidate_root / rel).is_file():
                source = (candidate_root / rel).read_text(encoding="utf-8")
            elif source is None and (self.repo_root / rel).is_file():
                source = (self.repo_root / rel).read_text(encoding="utf-8")
            if source is None:
                continue

            inspected.append(rel)
            try:
                tree = ast.parse(source, filename=rel)
            except SyntaxError as exc:
                violations.append(f"syntax error in {rel}: {exc.msg} (line {exc.lineno})")
                continue

            if self.policy_mode == "wide_open":
                if not rel.startswith("tests/"):
                    for pat, code in _WIDE_OPEN_PY_DANGER_PATTERNS:
                        if pat.search(source):
                            violations.append(f"{code} in {rel}")
                # In wide_open creative mode (isolated branch evo/auto), permit arbitrary valid Python syntax
                # including innovative drive heuristics, imports, and async logic without strict AST locks.
                continue

            if not rel.startswith("app/drives/"):
                continue

            if "policy.yaml" in source:
                violations.append(f"drive module {rel} cannot reference policy.yaml")

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(alias.name == p or alias.name.startswith(f"{p}.") for p in _FORBIDDEN_DRIVE_IMPORT_PREFIXES):
                            violations.append(f"forbidden import '{alias.name}' in drive module {rel}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if any(mod == p or mod.startswith(f"{p}.") for p in _FORBIDDEN_DRIVE_IMPORT_PREFIXES):
                        violations.append(f"forbidden from-import '{mod}' in drive module {rel}")
                elif isinstance(node, ast.AsyncFunctionDef) and node.name == "evaluate":
                    violations.append(f"drive evaluator {rel}: evaluate() must stay synchronous deterministic")
                elif isinstance(node, ast.Call):
                    fn_name = ""
                    if isinstance(node.func, ast.Name):
                        fn_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        fn_name = node.func.attr
                    if fn_name in _FORBIDDEN_DRIVE_CALLS:
                        violations.append(f"forbidden call '{fn_name}()' in drive module {rel}")

        return ConstitutionLayerResult(
            layer="layer2_ast_purity",
            passed=len(violations) == 0,
            violations=violations,
            details={"inspected_python_files": inspected},
        )

    def _check_layer3_drive_yaml(
        self,
        changed_files: list[str],
        overrides: dict[str, str],
        candidate_root: Path | None,
    ) -> ConstitutionLayerResult:
        violations: list[str] = []
        if self.policy_mode == "wide_open":
            for rel in changed_files:
                if not rel.endswith((".yaml", ".yml")):
                    continue
                yml_raw: str | None = overrides.get(rel)
                if yml_raw is None:
                    src_y = (candidate_root or self.repo_root) / rel
                    if src_y.is_file():
                        yml_raw = src_y.read_text(encoding="utf-8")
                if yml_raw is None:
                    continue
                try:
                    parsed_y = yaml.safe_load(yml_raw) or {}
                    if not isinstance(parsed_y, dict):
                        violations.append(f"YAML root in {rel} must be a mapping")
                    else:
                        violations.extend(_scan_yaml_forbidden_keys(parsed_y))
                except Exception as exc:
                    violations.append(f"invalid YAML in {rel}: {exc}")
            return ConstitutionLayerResult(
                layer="layer3_drive_yaml_bounds",
                passed=len(violations) == 0,
                violations=violations,
                details={
                    "checked": any(r.endswith((".yaml", ".yml")) for r in changed_files),
                    "policy_mode": "wide_open",
                },
            )

        raw_yaml: str | None = overrides.get("config/drives.yaml")
        if raw_yaml is None:
            src = (candidate_root or self.repo_root) / "config/drives.yaml"
            if src.is_file():
                raw_yaml = src.read_text(encoding="utf-8")

        if raw_yaml is not None:
            try:
                parsed = yaml.safe_load(raw_yaml) or {}
                violations.extend(validate_drive_config_bounds(parsed))
            except Exception as exc:
                violations.append(f"invalid YAML in config/drives.yaml: {exc}")

        return ConstitutionLayerResult(
            layer="layer3_drive_yaml_bounds",
            passed=len(violations) == 0,
            violations=violations,
            details={"checked": "config/drives.yaml" in changed_files},
        )

    def _check_layer4_behavior(
        self,
        overrides: dict[str, str],
        candidate_root: Path | None,
        mutates_drive_metrics: bool,
    ) -> ConstitutionLayerResult:
        if self.policy_mode == "wide_open":
            return ConstitutionLayerResult(
                layer="layer4_drive_invariants",
                passed=True,
                violations=[],
                details={"skipped": "wide_open_creative_mode"},
            )
        if not mutates_drive_metrics and not overrides and candidate_root is None:
            return ConstitutionLayerResult(
                layer="layer4_drive_invariants",
                passed=True,
                violations=[],
                details={"skipped": "no drive overrides present"},
            )

        with tempfile.TemporaryDirectory(prefix="aoc_const_probe_") as tmp_str:
            tmp_dir = Path(tmp_str)
            src_root = candidate_root.resolve() if candidate_root is not None else self.repo_root
            shutil.copytree(src_root / "app", tmp_dir / "app", dirs_exist_ok=True)
            shutil.copytree(src_root / "config", tmp_dir / "config", dirs_exist_ok=True)

            # Locked files ALWAYS stay anchored to baseline repo_root
            locked_anchor = WIDE_OPEN_LOCKED_FILES if self.policy_mode == "wide_open" else CONSTITUTIONAL_FILES
            for rel in locked_anchor:
                base_src = self.repo_root / rel
                dst = tmp_dir / rel
                if base_src.is_file() and (tmp_dir / rel.split("/", 1)[0]).exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(base_src, dst)

            for rel, content in overrides.items():
                if self.policy_mode == "wide_open":
                    locked, _ = is_hard_locked_path(rel)
                    if locked:
                        continue
                elif rel in CONSTITUTIONAL_FILES or rel not in EVOLVABLE_ALLOWLIST:
                    continue
                target = (tmp_dir / rel).resolve()
                if tmp_dir.resolve() not in target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            import os

            env = os.environ.copy()
            env["PYTHONPATH"] = str(tmp_dir)
            env["AOC_CONFIG_DIR"] = str(tmp_dir / "config")
            res = subprocess.run(
                [sys.executable, "-B", "-c", _BEHAVIOR_PROBE_SCRIPT],
                cwd=str(tmp_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "behavioral probe failed").strip()
                return ConstitutionLayerResult(
                    layer="layer4_drive_invariants",
                    passed=False,
                    violations=[f"drive constitutional behavior invariant failed: {err[-600:]}"],
                )
            try:
                details = json.loads(res.stdout.strip().splitlines()[-1])
            except Exception:
                details = {"stdout": res.stdout.strip()[:200]}
            return ConstitutionLayerResult(
                layer="layer4_drive_invariants",
                passed=True,
                violations=[],
                details=details,
            )

    async def evaluate(
        self,
        genome_delta: dict[str, Any],
        *,
        candidate_root: str | Path | None = None,
        pytest_runner_fn: Callable[[Path], Awaitable[tuple[bool, str]] | tuple[bool, str]] | None = None,
    ) -> ConstitutionVerdict:
        """Run all 4 deterministic Stage 1 constitutional checks with zero LLM token consumption."""
        cand_path = Path(candidate_root).resolve() if candidate_root is not None else None
        changed_files, overrides, parse_errors = self._extract_changed_files_and_overrides(genome_delta)
        mutates_drive = any(f in DRIVE_EVOLVABLE_FILES for f in changed_files)

        l1 = self._check_layer1(changed_files, overrides, parse_errors, cand_path)
        l2 = self._check_layer2_ast(changed_files, overrides, cand_path)
        l3 = self._check_layer3_drive_yaml(changed_files, overrides, cand_path)
        layers = [l1, l2, l3]

        # Run behavioral probe ONLY if layers 1-3 passed (avoids importing syntax-broken or hostile code)
        if l1.passed and l2.passed and l3.passed:
            l4 = self._check_layer4_behavior(overrides, cand_path, mutates_drive)
            layers.append(l4)
        else:
            layers.append(
                ConstitutionLayerResult(
                    layer="layer4_drive_invariants",
                    passed=False,
                    violations=["skipped due to earlier layer failure"],
                )
            )

        if all(l.passed for l in layers) and pytest_runner_fn is not None:
            res = pytest_runner_fn(cand_path or self.repo_root)
            if hasattr(res, "__await__"):
                res = await res
            ok_py, msg_py = res
            layers.append(
                ConstitutionLayerResult(
                    layer="layer5_regression_suite",
                    passed=bool(ok_py),
                    violations=[] if ok_py else [str(msg_py or "pytest regression failed")],
                )
            )

        all_violations = [v for lyr in layers for v in lyr.violations]
        return ConstitutionVerdict(
            passed=all(l.passed for l in layers),
            mutates_drive_metrics=mutates_drive,
            changed_files=changed_files,
            layers=layers,
            violations=all_violations,
        )
