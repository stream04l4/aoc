"""Read-only Web Change DigitalWorldSensor with content-hash diffing and SSRF protection (V2.0 P1-5)."""

from __future__ import annotations

import difflib
import hashlib
import re
from typing import Any, Sequence

import httpx

from app.perception.interfaces import utcnow
from app.perception.world.interfaces import DigitalWorldSensor, SensorPollResult, WorldEvent, WorldSourceConfig
from app.perception.world.security import ResolverFn, html_to_clean_text, safe_http_fetch, sanitize_external_text

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class WebChangeWorldSensor(DigitalWorldSensor):
    sensor_type = "web"

    def __init__(
        self,
        *,
        allowed_domains: Sequence[str],
        max_response_bytes: int = 2 * 1024 * 1024,
        timeout_seconds: float = 15.0,
        max_redirects: int = 3,
        http_client: httpx.AsyncClient | None = None,
        resolver: ResolverFn | None = None,
        allow_http: bool = False,
    ) -> None:
        self.allowed_domains = list(allowed_domains)
        self.max_response_bytes = max_response_bytes
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self._http_client = http_client
        self._resolver = resolver
        self._allow_http = allow_http

    async def poll(self, source: WorldSourceConfig, cursor: dict[str, Any]) -> SensorPollResult:
        url = str(source.config.get("url") or "").strip()
        if not url:
            return SensorPollResult(source_id=source.source_id, new_cursor=cursor, error="missing web url")

        headers: dict[str, str] = {"Accept": "text/html, text/plain, application/xhtml+xml"}
        if cursor.get("etag"):
            headers["If-None-Match"] = str(cursor["etag"])
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = str(cursor["last_modified"])

        status, resp_headers, body, final_url = await safe_http_fetch(
            url,
            allowed_domains=self.allowed_domains,
            max_bytes=self.max_response_bytes,
            timeout_seconds=self.timeout_seconds,
            max_redirects=self.max_redirects,
            headers=headers,
            http_client=self._http_client,
            resolver=self._resolver,
            allow_http=self._allow_http,
        )

        new_cursor = dict(cursor)
        if resp_headers.get("etag"):
            new_cursor["etag"] = resp_headers["etag"]
        if resp_headers.get("last-modified"):
            new_cursor["last_modified"] = resp_headers["last-modified"]

        if status == 304:
            return SensorPollResult(source_id=source.source_id, events=[], new_cursor=new_cursor, not_modified=True)
        if status >= 400:
            raise RuntimeError(f"Web source returned HTTP {status} for {url}")

        raw_text = body.decode("utf-8", errors="replace")
        title_m = _TITLE_RE.search(raw_text)
        page_title = sanitize_external_text(title_m.group(1), max_chars=250) if title_m else (source.name or final_url)
        clean_text = sanitize_external_text(html_to_clean_text(raw_text), max_chars=8000)
        content_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()

        prev_hash = str(cursor.get("last_content_hash") or "")
        if prev_hash == content_hash:
            return SensorPollResult(source_id=source.source_id, events=[], new_cursor=new_cursor, not_modified=True)

        prev_excerpt = str(cursor.get("last_excerpt") or "")
        if prev_excerpt:
            diff_lines = list(
                difflib.unified_diff(
                    prev_excerpt.split(". "),
                    clean_text[:2000].split(". "),
                    lineterm="",
                    n=1,
                )
            )
            diff_summary = " | ".join(line for line in diff_lines if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))[:1200]
        else:
            diff_summary = clean_text[:1200]

        new_cursor["last_content_hash"] = content_hash
        new_cursor["last_excerpt"] = clean_text[:2000]

        ev = WorldEvent(
            source_id=source.source_id,
            event_type="web_change",
            title=f"Page updated: {page_title}",
            content=diff_summary or clean_text[:1200],
            content_hash=content_hash,
            source_reliability=source.reliability_score,
            observed_at=utcnow(),
            metadata={"url": final_url, "page_title": page_title, "initial_capture": not bool(prev_hash)},
        )
        return SensorPollResult(source_id=source.source_id, events=[ev], new_cursor=new_cursor)
