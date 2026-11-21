"""Three real, guarded stdio JSON-RPC 2.0 MCP Tool Servers (V2.0 P1-6, Task Book Section 5.7.1).

Built-in MCP servers (each runnable over stdio JSON-RPC 2.0 via `python -m app.tools.builtin_servers <server>`):
  1. aoc-mcp-web     -> `web_fetch_readonly`   (L0 read-only, domain allowlist + SSRF/metadata block, <=2MB fetch, <=16KB out, <=15s)
  2. aoc-mcp-fs      -> `workspace_filesystem` (L0 read / L1 write, root-locked workspace, blocks `..`, symlinks, `.aoc_*`, <=2MB file)
  3. aoc-mcp-gitdocs -> `git_docs_search`      (L0 read-only, allowlisted repos only, git log/grep/read_doc, forbids mutations/hooks, <=50 matches)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from app.perception.world.security import (
    ResponseTooLargeError,
    SSRFBlockedError,
    html_to_clean_text,
    safe_http_fetch,
    sanitize_external_text,
)

MAX_FS_FILE_BYTES = 2 * 1024 * 1024
MAX_WEB_OUTPUT_CHARS = 14000
MAX_GIT_RESULTS = 50


def _validate_workspace_path(workspace_root: Path, workspace: str, rel_path: str) -> Path:
    """Enforce strict workspace containment, blocking `..` traversal, symlinks, and `.aoc_*` files."""
    ws_root = workspace_root.resolve()
    ws_dir = Path(workspace).resolve()
    if ws_dir != ws_root and ws_root not in ws_dir.parents:
        raise PermissionError(f"Workspace '{ws_dir}' is outside allowed workspace_root '{ws_root}'")
    ws_dir.mkdir(parents=True, exist_ok=True)

    rel_clean = (rel_path or ".").strip()
    if os.path.isabs(rel_clean):
        raise PermissionError("Absolute paths are forbidden in workspace_filesystem")

    # Reject any `.aoc_*` metadata component
    parts = Path(rel_clean).parts
    for part in parts:
        if part == "..":
            raise PermissionError("Parent traversal '..' is forbidden in workspace_filesystem")
        if part.startswith(".aoc_"):
            raise PermissionError(f"Access to internal metadata '{part}' is forbidden")

    # Walk each path component to catch symlink escapes even before target exists
    cursor = ws_dir
    for part in parts:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            resolved_sym = cursor.resolve()
            if resolved_sym != ws_dir and ws_dir not in resolved_sym.parents:
                raise PermissionError(f"Symlink escape blocked: '{part}' points outside workspace")

    final_target = (ws_dir / rel_clean).resolve()
    if final_target != ws_dir and ws_dir not in final_target.parents:
        raise PermissionError(f"Path escape blocked: '{rel_clean}' resolves outside workspace")
    return final_target


async def execute_web_fetch_readonly(
    arguments: dict[str, Any],
    *,
    allowed_domains: Sequence[str],
    max_fetch_bytes: int = 2 * 1024 * 1024,
    max_output_chars: int = MAX_WEB_OUTPUT_CHARS,
    timeout_seconds: float = 15.0,
    http_client: Any = None,
    resolver: Any = None,
) -> dict[str, Any]:
    url = str(arguments.get("url") or "").strip()
    if not url:
        raise ValueError("Missing required argument: url")

    status, headers, body, final_url = await safe_http_fetch(
        url,
        allowed_domains=allowed_domains,
        max_bytes=max_fetch_bytes,
        timeout_seconds=min(timeout_seconds, 15.0),
        max_redirects=3,
        http_client=http_client,
        resolver=resolver,
    )
    if status >= 400:
        raise RuntimeError(f"web_fetch_readonly received HTTP {status} for {final_url}")

    raw_text = body.decode("utf-8", errors="replace")
    ctype = headers.get("content-type", "")
    clean = html_to_clean_text(raw_text) if ("html" in ctype.lower() or "<html" in raw_text[:500].lower()) else raw_text
    clean = sanitize_external_text(clean, max_chars=max_output_chars + 64)
    truncated = len(clean) > max_output_chars
    if truncated:
        clean = clean[:max_output_chars]

    return {
        "url": final_url,
        "status_code": status,
        "content_type": ctype,
        "markdown_excerpt": clean,
        "truncated": truncated,
        "bytes_fetched": len(body),
    }


async def execute_workspace_filesystem(
    arguments: dict[str, Any],
    *,
    workspace_root: Path,
    workspace: str,
) -> dict[str, Any]:
    action = str(arguments.get("action") or "").strip().lower()
    rel_path = str(arguments.get("path") or ".")

    if action not in {"list_dir", "read_file", "write_file"}:
        raise ValueError(f"Unsupported workspace_filesystem action '{action}' (allowed: list_dir, read_file, write_file)")

    target = _validate_workspace_path(workspace_root, workspace, rel_path)

    if action == "list_dir":
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"Directory not found: {rel_path}")
        entries = []
        for entry in sorted(target.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".aoc_"):
                continue
            entries.append({"name": entry.name, "is_dir": entry.is_dir(), "size": entry.stat().st_size if entry.is_file() else 0})
        return {"action": "list_dir", "path": rel_path, "entries": entries[:200]}

    if action == "read_file":
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"File not found: {rel_path}")
        size = target.stat().st_size
        if size > MAX_FS_FILE_BYTES:
            raise ValueError(f"File size {size} exceeds 2MB read limit")
        content = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > 16384
        return {
            "action": "read_file",
            "path": rel_path,
            "size": size,
            "content": content[:16384],
            "truncated": truncated,
        }

    # write_file (L1)
    content = str(arguments.get("content") or "")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FS_FILE_BYTES:
        raise ValueError(f"Write payload size {len(encoded)} exceeds 2MB limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    return {"action": "write_file", "path": rel_path, "bytes_written": len(encoded)}


async def execute_git_docs_search(
    arguments: dict[str, Any],
    *,
    allowed_git_roots: Sequence[str],
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    action = str(arguments.get("action") or "grep").strip().lower()
    if action not in {"git_log", "grep", "read_doc"}:
        raise ValueError(f"Unsupported git_docs_search action '{action}' (allowed: git_log, grep, read_doc)")

    repo_raw = str(arguments.get("repo_path") or "").strip()
    if not repo_raw:
        if allowed_git_roots:
            repo_raw = str(allowed_git_roots[0])
        else:
            raise ValueError("Missing repo_path")

    repo = Path(repo_raw).resolve()
    roots = [Path(r).resolve() for r in allowed_git_roots]
    if not any(repo == r or r in repo.parents for r in roots):
        raise PermissionError(f"Repository path '{repo}' is outside allowed_git_roots")
    if not repo.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo}")

    git_bin = shutil.which("git")
    if not git_bin:
        raise FileNotFoundError("git binary not found")

    if action == "git_log":
        limit = max(1, min(int(arguments.get("limit", 10)), MAX_GIT_RESULTS))
        proc = await asyncio.create_subprocess_exec(
            git_bin, "-c", "core.hooksPath=/dev/null", "-C", str(repo),
            "log", f"-n{limit}", "--format=%H|%cI|%an|%s",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        if proc.returncode != 0:
            raise RuntimeError(err_b.decode(errors="replace").strip())
        commits = []
        for line in out_b.decode("utf-8", errors="replace").splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({"sha": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]})
        return {"action": "git_log", "repo_path": str(repo), "commits": commits[:MAX_GIT_RESULTS]}

    if action == "grep":
        pattern = str(arguments.get("query") or "").strip()
        if not pattern:
            raise ValueError("Missing required argument 'query' for grep")
        proc = await asyncio.create_subprocess_exec(
            git_bin, "-c", "core.hooksPath=/dev/null", "-C", str(repo),
            "grep", "-n", "-I", "-F", "--", pattern,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        if proc.returncode not in {0, 1}:
            raise RuntimeError(err_b.decode(errors="replace").strip())
        matches = [ln[:240] for ln in out_b.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        return {
            "action": "grep",
            "repo_path": str(repo),
            "query": pattern,
            "matches": matches[:MAX_GIT_RESULTS],
            "truncated": len(matches) > MAX_GIT_RESULTS,
        }

    # read_doc (strictly inside repo, blocks .env / secrets / symlink escape)
    rel_doc = str(arguments.get("doc_path") or "README.md").strip()
    if os.path.isabs(rel_doc) or ".." in Path(rel_doc).parts:
        raise PermissionError("Invalid doc_path traversal")
    if re.search(r"(\.env|\.ssh|id_rsa|\.pem|credentials|secret)", rel_doc, re.I):
        raise PermissionError(f"Reading sensitive path '{rel_doc}' is forbidden")
    doc_target = (repo / rel_doc).resolve()
    if repo not in doc_target.parents:
        raise PermissionError("doc_path escapes allowed repository root")
    if not doc_target.exists() or not doc_target.is_file():
        raise FileNotFoundError(f"Document not found: {rel_doc}")
    txt = doc_target.read_text(encoding="utf-8", errors="replace")
    return {
        "action": "read_doc",
        "repo_path": str(repo),
        "doc_path": rel_doc,
        "content": txt[:14000],
        "truncated": len(txt) > 14000,
    }


async def _serve_stdio_mcp(server_name: str) -> None:
    """Minimal MCP JSON-RPC 2.0 stdio server loop supporting initialize, tools/list, and tools/call."""
    allowed_domains = json.loads(os.environ.get("AOC_MCP_ALLOWED_DOMAINS", "[]"))
    allowed_git_roots = json.loads(os.environ.get("AOC_MCP_ALLOWED_GIT_ROOTS", "[]"))
    workspace_root = Path(os.environ.get("AOC_MCP_WORKSPACE_ROOT", "/home/ubuntu/aoc-workspaces")).resolve()

    tool_catalog: dict[str, dict[str, Any]] = {
        "aoc-mcp-web": {"name": "web_fetch_readonly", "description": "Read-only allowlisted web fetch (L0)"},
        "aoc-mcp-fs": {"name": "workspace_filesystem", "description": "Workspace-locked filesystem operations (L0/L1)"},
        "aoc-mcp-gitdocs": {"name": "git_docs_search", "description": "Read-only Git log/grep/docs search (L0)"},
    }

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        req_id: Any = None
        try:
            msg = json.loads(line)
            req_id = msg.get("id")
            method = msg.get("method", "")
            params = msg.get("params") or {}

            if method == "initialize":
                result: Any = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": server_name, "version": "2.0.0"},
                    "capabilities": {"tools": {}},
                }
            elif method == "tools/list":
                info = tool_catalog.get(server_name)
                result = {"tools": [info] if info else []}
            elif method == "tools/call":
                tname = str(params.get("name") or "")
                args = dict(params.get("arguments") or {})
                ws = str(params.get("workspace") or workspace_root)
                if server_name == "aoc-mcp-web" and tname == "web_fetch_readonly":
                    data = await execute_web_fetch_readonly(args, allowed_domains=allowed_domains)
                elif server_name == "aoc-mcp-fs" and tname == "workspace_filesystem":
                    data = await execute_workspace_filesystem(args, workspace_root=workspace_root, workspace=ws)
                elif server_name == "aoc-mcp-gitdocs" and tname == "git_docs_search":
                    data = await execute_git_docs_search(args, allowed_git_roots=allowed_git_roots)
                else:
                    raise ValueError(f"Tool '{tname}' not served by '{server_name}'")
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "structured": data}
            else:
                raise ValueError(f"Unsupported MCP method '{method}'")

            resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except (SSRFBlockedError, ResponseTooLargeError, PermissionError, ValueError, FileNotFoundError) as exc:
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": str(exc)}}
        except Exception as exc:
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(exc)}}

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    srv = sys.argv[1] if len(sys.argv) > 1 else "aoc-mcp-fs"
    asyncio.run(_serve_stdio_mcp(srv))
