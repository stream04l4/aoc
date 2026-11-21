"""RSS / Atom DigitalWorldSensor with ETag/Last-Modified checkpoints and SSRF/XXE protection (V2.0 P1-5)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Sequence

import httpx

from app.perception.interfaces import utcnow
from app.perception.world.interfaces import DigitalWorldSensor, SensorPollResult, WorldEvent, WorldSourceConfig
from app.perception.world.security import ResolverFn, safe_http_fetch, sanitize_external_text

_XXE_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)\b", re.I)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _parse_date(raw: str | None) -> datetime:
    if not raw:
        return utcnow()
    txt = raw.strip()
    try:
        dt = parsedate_to_datetime(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return utcnow()


def parse_rss_or_atom(xml_bytes: bytes, source: WorldSourceConfig, max_items: int = 20) -> list[WorldEvent]:
    text = xml_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    if _XXE_RE.search(text):
        raise ValueError("Rejected XML feed containing DOCTYPE/ENTITY declarations")

    root = ET.fromstring(text)
    root_name = _strip_ns(root.tag)
    events: list[WorldEvent] = []

    if root_name == "rss" or any(_strip_ns(c.tag) == "channel" for c in root):
        channels = [c for c in root if _strip_ns(c.tag) == "channel"] or [root]
        for ch in channels:
            for item in ch:
                if _strip_ns(item.tag) != "item":
                    continue
                fields: dict[str, str] = {}
                for child in item:
                    cname = _strip_ns(child.tag)
                    if cname not in fields and child.text:
                        fields[cname] = child.text.strip()
                title = sanitize_external_text(fields.get("title", ""), max_chars=400)
                body = sanitize_external_text(fields.get("description") or fields.get("encoded") or title, max_chars=3000)
                link = fields.get("link", "")
                guid = fields.get("guid") or link or title
                if not (title or body):
                    continue
                ev = WorldEvent(
                    source_id=source.source_id,
                    event_type="rss_item",
                    title=title or "(untitled)",
                    content=body or title,
                    source_reliability=source.reliability_score,
                    observed_at=_parse_date(fields.get("pubdate") or fields.get("date")),
                    metadata={"link": link, "guid": guid, "feed_format": "rss"},
                )
                ev.ensure_hash()
                events.append(ev)
                if len(events) >= max_items:
                    break
    elif root_name == "feed":
        for entry in root:
            if _strip_ns(entry.tag) != "entry":
                continue
            title = ""
            body = ""
            link = ""
            entry_id = ""
            pub = None
            for child in entry:
                cname = _strip_ns(child.tag)
                if cname == "title" and child.text:
                    title = sanitize_external_text(child.text, max_chars=400)
                elif cname in {"summary", "content"} and child.text and not body:
                    body = sanitize_external_text(child.text, max_chars=3000)
                elif cname == "link":
                    href = child.attrib.get("href") or (child.text or "").strip()
                    if href and not link:
                        link = href
                elif cname == "id" and child.text:
                    entry_id = child.text.strip()
                elif cname in {"updated", "published"} and child.text and not pub:
                    pub = child.text.strip()
            if not (title or body):
                continue
            ev = WorldEvent(
                source_id=source.source_id,
                event_type="rss_item",
                title=title or "(untitled)",
                content=body or title,
                source_reliability=source.reliability_score,
                observed_at=_parse_date(pub),
                metadata={"link": link, "guid": entry_id or link or title, "feed_format": "atom"},
            )
            ev.ensure_hash()
            events.append(ev)
            if len(events) >= max_items:
                break
    else:
        raise ValueError(f"Unsupported feed root element: <{root.tag}>")

    return events


class RSSWorldSensor(DigitalWorldSensor):
    sensor_type = "rss"

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
            return SensorPollResult(source_id=source.source_id, new_cursor=cursor, error="missing rss url")

        headers: dict[str, str] = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
        if cursor.get("etag"):
            headers["If-None-Match"] = str(cursor["etag"])
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = str(cursor["last_modified"])

        status, resp_headers, body, _final_url = await safe_http_fetch(
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
            raise RuntimeError(f"RSS feed returned HTTP {status} for {url}")

        max_items = int(source.config.get("max_items", 20))
        all_events = parse_rss_or_atom(body, source, max_items=max_items)
        seen_hashes = set(cursor.get("seen_hashes") or [])
        fresh = [ev for ev in all_events if ev.ensure_hash() not in seen_hashes]
        combined_seen = list(seen_hashes | {ev.content_hash for ev in all_events})[-200:]
        new_cursor["seen_hashes"] = combined_seen
        return SensorPollResult(source_id=source.source_id, events=fresh, new_cursor=new_cursor)
