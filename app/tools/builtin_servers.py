"""Three real, guarded stdio JSON-RPC 2.0 MCP Tool Servers (V2.0 P1-6, Task Book Section 5.7.1).

Built-in MCP servers (each runnable over stdio JSON-RPC 2.0 via `python -m app.tools.builtin_servers <server>`):
  1. aoc-mcp-web     -> `web_fetch_readonly`   (L0 read-only, domain allowlist + SSRF/metadata block, <=2MB fetch, <=16KB out, <=15s)
  2. aoc-mcp-fs      -> `workspace_filesystem` (L0 read / L1 write, root-locked workspace, blocks `..`, symlinks, `.aoc_*`, <=2MB file)
  3. aoc-mcp-gitdocs -> `git_docs_search`      (L0 read-only, allowlisted repos only, git log/grep/read_doc, forbids mutations/hooks, <=50 matches)
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote_plus, urlparse

import httpx

from app.perception.world.security import (
    ResponseTooLargeError,
    SSRFBlockedError,
    _safe_public_https_url,
    detect_prompt_injection,
    html_to_clean_text,
    html_to_clean_text_and_links,
    is_blocked_ip,
    is_internal_or_blocked_host,
    safe_http_fetch,
    sanitize_external_text,
)

MAX_FS_FILE_BYTES = 2 * 1024 * 1024
MAX_WEB_OUTPUT_CHARS = 14000
MAX_GIT_RESULTS = 50
_XXE_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)\b", re.I)


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


def _parse_rss_or_atom_feed(
    raw_xml: str,
    base_url: str,
    *,
    blocked_hosts: Sequence[str] = (),
    max_entries: int = 12,
) -> tuple[str, list[dict[str, str]]] | None:
    """Parse Atom or RSS 2.0 XML into a compact multi-entry summary and safe outbound links."""
    if _XXE_RE.search(raw_xml):
        raise ValueError("Rejected XML feed containing DOCTYPE/ENTITY declarations")

    root = ET.fromstring(raw_xml)
    atom_ns = "{http://www.w3.org/2005/Atom}"
    feed_title = "Feed"
    raw_entries: list[tuple[str, str, str, str]] = []

    atom_entries = root.findall(f"{atom_ns}entry") or root.findall("entry")
    if atom_entries:
        ns = atom_ns if root.find(f"{atom_ns}entry") is not None else ""
        feed_title = sanitize_external_text(root.findtext(f"{ns}title") or "Atom Feed", max_chars=120) or "Atom Feed"
        for entry in atom_entries[: max(1, int(max_entries))]:
            etitle = entry.findtext(f"{ns}title") or ""
            edate = (entry.findtext(f"{ns}updated") or entry.findtext(f"{ns}published") or "").strip()[:32]
            elink = ""
            for lnk in entry.findall(f"{ns}link"):
                href = (lnk.attrib.get("href") or "").strip()
                rel = (lnk.attrib.get("rel") or "alternate").strip().lower()
                if href and rel in {"alternate", ""}:
                    elink = href
                    break
            if not elink:
                elink = (entry.findtext(f"{ns}link") or entry.findtext(f"{ns}id") or "").strip()
            esum = entry.findtext(f"{ns}summary") or entry.findtext(f"{ns}content") or ""
            raw_entries.append((etitle, edate, elink, esum))
    else:
        channel = root.find("channel")
        rss_items = (channel.findall("item") if channel is not None else []) or root.findall("item")
        if not rss_items:
            return None
        ft_raw = (channel.findtext("title") if channel is not None else None) or root.findtext("title") or "RSS Feed"
        feed_title = sanitize_external_text(ft_raw, max_chars=120) or "RSS Feed"
        for item in rss_items[: max(1, int(max_entries))]:
            ititle = item.findtext("title") or ""
            idate = (item.findtext("pubDate") or "").strip()[:32]
            ilink = (item.findtext("link") or "").strip()
            isum = item.findtext("description") or ""
            raw_entries.append((ititle, idate, ilink, isum))

    lines: list[str] = []
    outbound_links: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for idx, (r_title, r_date, r_link, r_sum) in enumerate(raw_entries, start=1):
        etitle = sanitize_external_text(r_title, max_chars=140)
        esum = sanitize_external_text(r_sum, max_chars=220)
        if detect_prompt_injection(etitle) or detect_prompt_injection(esum) or detect_prompt_injection(r_link):
            continue
        safe_url = ""
        if r_link:
            norm = _safe_public_https_url(r_link, base_url, blocked_hosts)
            if norm:
                safe_url, domain = norm
                if len(outbound_links) < 10 and safe_url not in seen_urls:
                    seen_urls.add(safe_url)
                    outbound_links.append({"text": etitle or safe_url, "url": safe_url, "domain": domain})
        lines.append(f"{idx}. {etitle or '(untitled)'} | date={r_date or 'unknown'} | url={safe_url or 'none'} | {esum}")

    if not lines:
        return None
    header = f"Feed: {feed_title} (showing {len(lines)} entries)"
    return header + "\n" + "\n".join(lines), outbound_links


async def execute_web_fetch_readonly(
    arguments: dict[str, Any],
    *,
    allowed_domains: Sequence[str],
    max_fetch_bytes: int = 2 * 1024 * 1024,
    max_output_chars: int = MAX_WEB_OUTPUT_CHARS,
    timeout_seconds: float = 20.0,
    http_client: Any = None,
    resolver: Any = None,
    open_public_https: bool = False,
    blocked_hosts: Sequence[str] = (),
) -> dict[str, Any]:
    url = str(arguments.get("url") or "").strip()
    if not url:
        raise ValueError("Missing required argument: url")
    extract_links = bool(arguments.get("extract_links", True))

    status, headers, body, final_url = await safe_http_fetch(
        url,
        allowed_domains=allowed_domains,
        max_bytes=max_fetch_bytes,
        timeout_seconds=min(float(timeout_seconds), 20.0),
        max_redirects=3,
        http_client=http_client,
        resolver=resolver,
        open_public_https=open_public_https,
        blocked_hosts=blocked_hosts,
    )
    if status >= 400:
        raise RuntimeError(f"web_fetch_readonly received HTTP {status} for {final_url}")

    raw_text = body.decode("utf-8", errors="replace")
    ctype = headers.get("content-type", "")
    head_snip = raw_text.lstrip()[:200].lower()
    is_xml_feed = (
        "xml" in ctype.lower()
        or "atom" in ctype.lower()
        or "rss" in ctype.lower()
        or head_snip.startswith(("<?xml", "<feed", "<rss"))
    )
    is_html = (not is_xml_feed) and ("html" in ctype.lower() or "<html" in raw_text[:500].lower())
    outbound_links: list[dict[str, str]] = []
    discovered_feeds: list[dict[str, str]] = []

    if is_xml_feed:
        try:
            parsed_feed = _parse_rss_or_atom_feed(
                raw_text,
                final_url,
                blocked_hosts=blocked_hosts,
                max_entries=12,
            )
        except ET.ParseError:
            parsed_feed = None
        if parsed_feed is not None:
            clean, feed_links = parsed_feed
            if extract_links:
                outbound_links = feed_links
        else:
            clean = raw_text
    elif is_html and extract_links:
        clean, outbound_links, discovered_feeds = html_to_clean_text_and_links(
            raw_text,
            base_url=final_url,
            max_links=10,
            max_feeds=3,
            blocked_hosts=blocked_hosts,
        )
    elif is_html:
        clean = html_to_clean_text(raw_text)
    else:
        clean = raw_text

    clean = sanitize_external_text(clean, max_chars=max_output_chars + 64)
    truncated = len(clean) > max_output_chars
    if truncated:
        clean = clean[:max_output_chars]

    return {
        "url": final_url,
        "status_code": status,
        "content_type": ctype,
        "markdown_excerpt": clean,
        "outbound_links": outbound_links,
        "discovered_feeds": discovered_feeds,
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


async def execute_arxiv_search(
    arguments: dict[str, Any],
    *,
    allowed_domains: Sequence[str] = ("export.arxiv.org",),
    timeout_seconds: float = 15.0,
    http_client: Any = None,
    resolver: Any = None,
) -> dict[str, Any]:
    """Read-only arXiv Atom API paper search returning structured, sanitized paper metadata (V4.1 L0)."""
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("Missing required argument: query")
    try:
        max_results = max(1, min(int(arguments.get("max_results", 5) or 5), 10))
    except (TypeError, ValueError):
        max_results = 5
    sort_by = str(arguments.get("sort_by") or "relevance").strip()
    if sort_by not in {"relevance", "lastUpdatedDate", "submittedDate"}:
        sort_by = "relevance"

    url = (
        f"https://export.arxiv.org/api/query?search_query=all:{quote_plus(query)}"
        f"&start=0&max_results={max_results}&sortBy={sort_by}&sortOrder=descending"
    )
    eff_domains = list(dict.fromkeys([*(str(d) for d in (allowed_domains or []) if d), "export.arxiv.org"]))
    status, _headers, body, _final_url = await safe_http_fetch(
        url,
        allowed_domains=eff_domains,
        max_bytes=2 * 1024 * 1024,
        timeout_seconds=min(float(timeout_seconds), 15.0),
        max_redirects=3,
        http_client=http_client,
        resolver=resolver,
    )
    if status >= 400:
        raise RuntimeError(f"arxiv_search received HTTP {status}")

    raw_xml = body.decode("utf-8", errors="replace").strip()
    if not raw_xml:
        return {"query": query, "sort_by": sort_by, "count": 0, "papers": []}
    if _XXE_RE.search(raw_xml):
        raise ValueError("Rejected arXiv XML containing DOCTYPE/ENTITY declarations")

    root = ET.fromstring(raw_xml)
    atom_ns = "{http://www.w3.org/2005/Atom}"
    papers: list[dict[str, Any]] = []
    for entry in root.findall(f"{atom_ns}entry")[:max_results]:
        id_raw = (entry.findtext(f"{atom_ns}id") or "").strip()
        arxiv_id = id_raw.rstrip("/").split("/")[-1] if id_raw else ""
        title = sanitize_external_text(entry.findtext(f"{atom_ns}title") or "", max_chars=240)
        summary = sanitize_external_text(entry.findtext(f"{atom_ns}summary") or "", max_chars=600)
        published = (entry.findtext(f"{atom_ns}published") or "").strip()[:32]
        authors: list[str] = []
        for auth in entry.findall(f"{atom_ns}author")[:4]:
            aname = sanitize_external_text(auth.findtext(f"{atom_ns}name") or "", max_chars=60)
            if aname:
                authors.append(aname)
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": " ".join(title.split()),
                "authors": authors,
                "published": published,
                "url": id_raw,
                "summary": " ".join(summary.split()),
            }
        )
    return {
        "query": query,
        "sort_by": sort_by,
        "count": len(papers),
        "papers": papers,
    }


async def execute_web_search(
    arguments: dict[str, Any],
    *,
    searxng_url: str,
    blocked_hosts: Sequence[str] = (),
    timeout_seconds: float = 20.0,
    http_client: Any = None,
) -> dict[str, Any]:
    """Read-only general web search via configured internal SearXNG backend (V4.2 L0)."""
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("Missing required argument: query")
    base = (searxng_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("web_search backend is not configured (AOC_KERNEL_V3__SEARXNG_URL)")

    try:
        max_results = max(1, min(int(arguments.get("max_results", 5) or 5), 8))
    except (TypeError, ValueError):
        max_results = 5
    categories = str(arguments.get("categories") or "general").strip().lower()
    if categories not in {"general", "it", "science"}:
        categories = "general"
    time_range = str(arguments.get("time_range") or "").strip().lower()
    if time_range not in {"", "day", "month", "year"}:
        time_range = ""

    params: dict[str, str] = {"q": query, "format": "json", "categories": categories}
    if time_range:
        params["time_range"] = time_range

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=min(float(timeout_seconds), 20.0), follow_redirects=False)
    try:
        resp = await client.get(
            f"{base}/search",
            params=params,
            headers={"Accept": "application/json", "User-Agent": "AOC-WebSearch/4.2"},
            timeout=min(float(timeout_seconds), 20.0),
        )
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code >= 400:
        raise RuntimeError(f"web_search received HTTP {resp.status_code} from SearXNG")
    payload = resp.json()
    raw_items = payload.get("results") or []

    results: list[dict[str, Any]] = []
    filtered_injections = 0
    seen_urls: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        r_url = str(item.get("url") or "").strip()
        parsed = urlparse(r_url)
        if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
            continue
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if is_internal_or_blocked_host(host, blocked_hosts):
            continue
        try:
            ipaddress.ip_address(host)
            if is_blocked_ip(host):
                continue
            continue  # reject bare IPs from web search results
        except ValueError:
            pass
        if r_url in seen_urls:
            continue

        title = sanitize_external_text(str(item.get("title") or host), max_chars=200)
        snippet = sanitize_external_text(str(item.get("content") or item.get("snippet") or ""), max_chars=420)
        if detect_prompt_injection(f"{title}\n{snippet}\n{r_url}"):
            filtered_injections += 1
            continue

        seen_urls.add(r_url)
        engines = [str(e) for e in (item.get("engines") or [])[:4] if e]
        results.append(
            {
                "title": title or host,
                "url": r_url,
                "domain": host,
                "snippet": snippet,
                "engines": engines,
            }
        )
        if len(results) >= max_results:
            break

    return {
        "query": query,
        "categories": categories,
        "count": len(results),
        "filtered_injections": filtered_injections,
        "results": results,
    }


async def _serve_stdio_mcp(server_name: str) -> None:
    """Minimal MCP JSON-RPC 2.0 stdio server loop supporting initialize, tools/list, and tools/call."""
    allowed_domains = json.loads(os.environ.get("AOC_MCP_ALLOWED_DOMAINS", "[]"))
    allowed_git_roots = json.loads(os.environ.get("AOC_MCP_ALLOWED_GIT_ROOTS", "[]"))
    workspace_root = Path(os.environ.get("AOC_MCP_WORKSPACE_ROOT", "/home/ubuntu/aoc-workspaces")).resolve()
    open_public_https = os.environ.get("AOC_MCP_OPEN_PUBLIC_HTTPS", "false").lower() in {"1", "true", "yes"}
    blocked_hosts = json.loads(os.environ.get("AOC_MCP_BLOCKED_HOSTS", "[]"))
    searxng_url = os.environ.get("AOC_MCP_SEARXNG_URL", "")

    tool_catalog: dict[str, dict[str, Any]] = {
        "aoc-mcp-web": {"name": "web_fetch_readonly", "description": "Read-only public web fetch with outbound link extraction (L0)"},
        "aoc-mcp-search": {"name": "web_search", "description": "Read-only general web search via SearXNG (L0)"},
        "aoc-mcp-fs": {"name": "workspace_filesystem", "description": "Workspace-locked filesystem operations (L0/L1)"},
        "aoc-mcp-gitdocs": {"name": "git_docs_search", "description": "Read-only Git log/grep/docs search (L0)"},
        "aoc-mcp-arxiv": {"name": "arxiv_search", "description": "Read-only arXiv paper search (L0)"},
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
                    data = await execute_web_fetch_readonly(
                        args,
                        allowed_domains=allowed_domains,
                        open_public_https=open_public_https,
                        blocked_hosts=blocked_hosts,
                    )
                elif server_name == "aoc-mcp-search" and tname == "web_search":
                    data = await execute_web_search(
                        args,
                        searxng_url=searxng_url,
                        blocked_hosts=blocked_hosts,
                    )
                elif server_name == "aoc-mcp-fs" and tname == "workspace_filesystem":
                    data = await execute_workspace_filesystem(args, workspace_root=workspace_root, workspace=ws)
                elif server_name == "aoc-mcp-gitdocs" and tname == "git_docs_search":
                    data = await execute_git_docs_search(args, allowed_git_roots=allowed_git_roots)
                elif server_name == "aoc-mcp-arxiv" and tname == "arxiv_search":
                    data = await execute_arxiv_search(args, allowed_domains=allowed_domains)
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
