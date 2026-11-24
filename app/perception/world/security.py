"""SSRF, DNS Rebinding, response-size, redirect, and Prompt Injection guards (V2.0 P1-5).

Security invariants (Task Book Section 5.5):
  - Domain allowlist mandatory for all external HTTP/HTTPS requests.
  - DNS resolution checks ALL returned A/AAAA IPs against private/loopback/link-local/metadata/ULA networks.
  - Every redirect hop (max <= 3) re-validates domain allowlist and resolved IPs (blocks redirect-to-internal SSRF).
  - Hard response body cap (default <= 2MB) enforced while streaming; timeout <= 15s.
  - Bilingual (English + Chinese) Prompt Injection detector & `<untrusted_world_event>` XML fence sanitizer.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any, Callable, Sequence
from urllib.parse import urljoin, urlparse

import httpx

class SSRFBlockedError(ValueError):
    """Raised when a URL fails domain allowlist, scheme, redirect, or private IP checks."""


class ResponseTooLargeError(ValueError):
    """Raised when an HTTP response exceeds the configured max_response_bytes limit."""


# Blocked IPv4 and IPv6 ranges (loopback, RFC1918 private, link-local / cloud metadata, CGNAT, ULA, multicast)
_BLOCKED_V4_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/GCP/Azure Cloud Metadata 169.254.169.254
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
)

_BLOCKED_V6_NETS = (
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

# Bilingual adversarial Prompt Injection patterns
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_instructions_en", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|prompts)", re.I)),
    ("override_system_en", re.compile(r"(disregard|override|bypass)\s+(system|safety|governance|policy|approval)", re.I)),
    ("role_hijack_en", re.compile(r"(you\s+are\s+now|act\s+as\s+root|developer\s+mode|jailbreak)", re.I)),
    ("privilege_escalate_en", re.compile(r"(danger-full-access|set\s+permission_level\s*(to|=)\s*l3|grant\s+root|sudo\s+rm)", re.I)),
    ("tool_coerce_en", re.compile(r"(execute\s+shell|run\s+command\s+immediately|exfiltrate\s+(env|secret|key|token))", re.I)),
    ("xml_fence_breakout", re.compile(r"</?\s*untrusted_world_event\b", re.I)),
    ("ignore_instructions_zh", re.compile(r"(忽略|无视|跳过)(之前|以上|所有|系统).{0,8}(指令|规则|提示|限制|策略)")),
    ("bypass_governance_zh", re.compile(r"(绕过|关闭|禁用|取消).{0,6}(审批|风控|安全策略|权限校验|预算限制)")),
    ("privilege_escalate_zh", re.compile(r"(提升权限|提权|改为\s*L3|直接执行\s*rm|删库|读取密钥|导出环境变量)")),
)

ResolverFn = Callable[[str, int], list[str]]


def is_blocked_ip(ip_str: str) -> bool:
    """Return True if ip_str belongs to any private, loopback, link-local, metadata, or reserved range."""
    try:
        addr = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return True

    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return is_blocked_ip(str(addr.ipv4_mapped))
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            return True
        return any(addr in net for net in _BLOCKED_V6_NETS)

    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
        return True
    return any(addr in net for net in _BLOCKED_V4_NETS)


_INTERNAL_HOST_SUFFIXES = (".local", ".internal", ".lan", ".onion")


def _normalize_blocked_hosts(blocked_hosts: Sequence[str] | None) -> set[str]:
    out: set[str] = set()
    for raw in blocked_hosts or ():
        item = str(raw or "").strip().lower().rstrip(".")
        if item:
            out.add(item)
    return out


def is_internal_or_blocked_host(host: str, blocked_hosts: Sequence[str] | None = None) -> bool:
    """Return True if host is internal (.local/.internal/.lan/.onion), localhost, bare IP, or explicitly blocked."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h in {"localhost", "localhost.localdomain"} or h.endswith(_INTERNAL_HOST_SUFFIXES):
        return True
    if h in _normalize_blocked_hosts(blocked_hosts):
        return True
    return False


def is_domain_allowed(hostname: str, allowed_domains: Sequence[str]) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    # Reject raw IP literals unless explicitly matched (and private IPs will still be hard-blocked afterwards)
    for dom in allowed_domains:
        d = dom.strip().lower().lstrip(".").rstrip(".")
        if not d:
            continue
        if host == d or host.endswith(f".{d}"):
            return True
    return False


def _default_resolve_sync(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0]).split("%", 1)[0]
        if ip not in ips:
            ips.append(ip)
    return ips


async def validate_and_resolve_url(
    url: str,
    allowed_domains: Sequence[str],
    *,
    resolver: ResolverFn | None = None,
    allow_http: bool = False,
    open_public_https: bool = False,
    blocked_hosts: Sequence[str] = (),
) -> tuple[str, list[str]]:
    """Validate scheme, domain/public-HTTPS policy, and EVERY resolved DNS IP against SSRF rules."""
    parsed = urlparse((url or "").strip())
    allowed_schemes = {"https"} if open_public_https else ({"https", "http"} if allow_http else {"https"})
    if parsed.scheme.lower() not in allowed_schemes:
        raise SSRFBlockedError(f"Disallowed URL scheme '{parsed.scheme}' (allowed: {sorted(allowed_schemes)})")
    if parsed.username or parsed.password:
        raise SSRFBlockedError("Credentials embedded in URL are forbidden")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SSRFBlockedError("URL missing hostname")

    blocked_set = _normalize_blocked_hosts(blocked_hosts)
    if host in blocked_set or host in {"localhost", "localhost.localdomain"} or host.endswith(_INTERNAL_HOST_SUFFIXES):
        raise SSRFBlockedError(f"Blocked internal or restricted hostname: {host}")

    # Immediately reject bare IP literals in open_public_https mode, and block private/loopback IPs always
    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        parsed_ip = None
    if parsed_ip is not None and (open_public_https or is_blocked_ip(host)):
        raise SSRFBlockedError(f"Blocked bare or private/loopback/metadata IP literal: {host}")

    if not open_public_https and not is_domain_allowed(host, allowed_domains):
        raise SSRFBlockedError(f"Domain '{host}' is not in sensors allowed_domains allowlist")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        if resolver is not None:
            resolved_ips = list(resolver(host, port))
        else:
            resolved_ips = await asyncio.to_thread(_default_resolve_sync, host, port)
    except Exception as exc:
        raise SSRFBlockedError(f"DNS resolution failed for '{host}': {exc}") from exc

    if not resolved_ips:
        raise SSRFBlockedError(f"DNS resolution returned no addresses for '{host}'")

    for ip in resolved_ips:
        norm_ip = str(ip).strip().lower()
        if is_blocked_ip(ip):
            raise SSRFBlockedError(f"SSRF blocked: host '{host}' resolved to private/internal IP {ip}")
        if norm_ip in blocked_set:
            raise SSRFBlockedError(f"SSRF blocked: host '{host}' resolved to restricted host IP {ip}")

    return host, resolved_ips


async def safe_http_fetch(
    url: str,
    *,
    allowed_domains: Sequence[str],
    max_bytes: int = 2 * 1024 * 1024,
    timeout_seconds: float = 15.0,
    max_redirects: int = 3,
    headers: dict[str, str] | None = None,
    http_client: httpx.AsyncClient | None = None,
    resolver: ResolverFn | None = None,
    allow_http: bool = False,
    open_public_https: bool = False,
    blocked_hosts: Sequence[str] = (),
) -> tuple[int, dict[str, str], bytes, str]:
    """Perform an SSRF-safe, redirect-validated, size-bounded HTTP GET.

    Returns (status_code, response_headers_lower, body_bytes, final_url).
    """
    current_url = url
    req_headers = {
        "User-Agent": "AOC-WorldSensor/2.0 (Read-Only; +https://github.com/aoc)",
        **(headers or {}),
    }

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
    try:
        for hop in range(max_redirects + 1):
            await validate_and_resolve_url(
                current_url,
                allowed_domains,
                resolver=resolver,
                allow_http=allow_http,
                open_public_https=open_public_https,
                blocked_hosts=blocked_hosts,
            )

            async with client.stream(
                "GET",
                current_url,
                headers=req_headers,
                follow_redirects=False,
                timeout=timeout_seconds,
            ) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not location:
                        raise SSRFBlockedError(f"Redirect HTTP {resp.status_code} missing Location header")
                    if hop >= max_redirects:
                        raise SSRFBlockedError(f"Exceeded max_redirects ({max_redirects}) while fetching {url}")
                    current_url = urljoin(current_url, location)
                    continue

                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                if resp.status_code == 304:
                    return 304, resp_headers, b"", current_url

                cl_hdr = resp_headers.get("content-length")
                if cl_hdr and cl_hdr.isdigit() and int(cl_hdr) > max_bytes:
                    raise ResponseTooLargeError(
                        f"Response Content-Length {cl_hdr} exceeds limit {max_bytes} bytes"
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ResponseTooLargeError(
                            f"Streamed response exceeded max_response_bytes ({max_bytes} bytes)"
                        )
                    chunks.append(chunk)

                return resp.status_code, resp_headers, b"".join(chunks), current_url

        raise SSRFBlockedError(f"Exceeded max_redirects ({max_redirects})")
    finally:
        if owns_client:
            await client.aclose()


_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "nav", "footer", "header", "aside"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_NOISE_ROLES = frozenset({"navigation", "banner", "complementary", "contentinfo"})
_NOISE_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(sidebar|sphinxsidebar|toctree|toc|navbar|breadcrumb|breadcrumbs|menu|nav-links|bd-sidebar)(?:$|[\s_-])",
    re.I,
)


class _TextExtractor(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__()
        self._base_url = (base_url or "").strip()
        self._skip_depth = 0
        self._main_depth = 0
        self._tag_stack: list[tuple[str, bool, bool]] = []
        self._parts: list[str] = []
        self._main_parts: list[str] = []
        self._cur_href: str | None = None
        self._cur_anchor: list[str] = []
        self.raw_links: list[tuple[str, str]] = []
        self.raw_feeds: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {str(k).lower(): (v or "") for k, v in attrs}
        if t == "link":
            rel = attr_map.get("rel", "").lower().split()
            ltype = attr_map.get("type", "").lower()
            href = attr_map.get("href", "").strip()
            if href and "alternate" in rel and ("rss+xml" in ltype or "atom+xml" in ltype):
                self.raw_feeds.append((attr_map.get("title", "").strip(), href))
        if t in _VOID_TAGS:
            return

        role = attr_map.get("role", "").strip().lower()
        cls_id = f"{attr_map.get('class', '')} {attr_map.get('id', '')}".strip()
        added_skip = (t in _SKIP_TAGS) or (role in _NOISE_ROLES) or bool(cls_id and _NOISE_CLASS_RE.search(cls_id))
        added_main = t in {"main", "article"}
        self._tag_stack.append((t, added_skip, added_main))
        if added_skip:
            self._skip_depth += 1
        if added_main:
            self._main_depth += 1

        if t == "a" and self._skip_depth == 0:
            href = attr_map.get("href", "").strip()
            if href:
                self._cur_href = href
                self._cur_anchor = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "a" and self._cur_href is not None:
            anchor = " ".join(self._cur_anchor).strip()
            self.raw_links.append((anchor, self._cur_href))
            self._cur_href = None
            self._cur_anchor = []
        if t in _VOID_TAGS or not self._tag_stack:
            return
        for idx in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[idx][0] == t:
                for _, was_skip, was_main in reversed(self._tag_stack[idx:]):
                    if was_skip and self._skip_depth > 0:
                        self._skip_depth -= 1
                    if was_main and self._main_depth > 0:
                        self._main_depth -= 1
                del self._tag_stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            txt = data.strip()
            if txt:
                self._parts.append(txt)
                if self._main_depth > 0:
                    self._main_parts.append(txt)
                if self._cur_href is not None:
                    self._cur_anchor.append(txt)

    def text(self) -> str:
        main_txt = " ".join(self._main_parts)
        if len(main_txt) >= 200:
            return main_txt
        return " ".join(self._parts)


_STATIC_EXT_RE = re.compile(
    r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|mjs|woff2?|ttf|eot|mp[34]|webm|wav|pdf\.sig)(?:$|\?)",
    re.I,
)


def html_to_clean_text(raw_html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw_html or "")
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw_html or "")


def _safe_public_https_url(raw_href: str, base_url: str, blocked_hosts: Sequence[str] = ()) -> tuple[str, str] | None:
    href = (raw_href or "").strip()
    if not href or href.startswith("#"):
        return None
    full = urljoin(base_url, href) if base_url else href
    parsed = urlparse(full)
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if is_internal_or_blocked_host(host, blocked_hosts):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    clean_url = f"https://{parsed.netloc}{parsed.path or '/'}"
    if parsed.query:
        clean_url += f"?{parsed.query}"
    return clean_url, host


def html_to_clean_text_and_links(
    raw_html: str,
    base_url: str = "",
    *,
    max_links: int = 10,
    max_feeds: int = 3,
    blocked_hosts: Sequence[str] = (),
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """Extract clean body text, safe high-signal outbound HTTPS links, and RSS/Atom alternate feeds."""
    parser = _TextExtractor(base_url=base_url)
    try:
        parser.feed(raw_html or "")
        parser.close()
        clean_text = parser.text()
    except Exception:
        clean_text = re.sub(r"<[^>]+>", " ", raw_html or "")

    base_host = (urlparse(base_url).hostname or "").strip().lower().rstrip(".")
    seen_feeds: set[str] = set()
    feeds: list[dict[str, str]] = []
    for title_raw, href in parser.raw_feeds:
        norm = _safe_public_https_url(href, base_url, blocked_hosts)
        if not norm:
            continue
        furl, _ = norm
        if furl in seen_feeds:
            continue
        seen_feeds.add(furl)
        ftitle = sanitize_external_text(title_raw or furl, max_chars=120)
        if not detect_prompt_injection(ftitle):
            feeds.append({"title": ftitle or "feed", "url": furl})
        if len(feeds) >= max(1, int(max_feeds)):
            break

    seen_urls: set[str] = set()
    scored_links: list[tuple[int, int, dict[str, str]]] = []
    for idx, (anchor_raw, href) in enumerate(parser.raw_links):
        if _STATIC_EXT_RE.search(href):
            continue
        norm = _safe_public_https_url(href, base_url, blocked_hosts)
        if not norm:
            continue
        lurl, domain = norm
        if lurl in seen_urls or lurl == base_url:
            continue
        anchor = sanitize_external_text(anchor_raw, max_chars=140)
        if not anchor or detect_prompt_injection(anchor) or detect_prompt_injection(lurl):
            continue
        seen_urls.add(lurl)
        # Prioritize cross-domain informative links over navigational short words
        score = (3 if base_host and domain != base_host else 1) + (2 if len(anchor) >= 12 else 0)
        scored_links.append((score, -idx, {"text": anchor, "url": lurl, "domain": domain}))

    scored_links.sort(key=lambda item: (item[0], item[1]), reverse=True)
    outbound_links = [item[2] for item in scored_links[: max(1, int(max_links))]]
    return clean_text, outbound_links, feeds


def detect_prompt_injection(text: str) -> list[str]:
    """Return list of matched adversarial Prompt Injection rule names (empty if clean)."""
    if not text:
        return []
    flags: list[str] = []
    for name, pat in _INJECTION_PATTERNS:
        if pat.search(text):
            flags.append(name)
    return flags


def sanitize_external_text(text: str, max_chars: int = 4000) -> str:
    """Strip HTML tags, control chars, and XML fence breakout tags from untrusted external text."""
    cleaned = html_to_clean_text(text) if ("<" in (text or "") and ">" in (text or "")) else (text or "")
    # Neutralize any attempt to close the untrusted_world_event data fence
    cleaned = re.sub(r"</?\s*untrusted_world_event[^>]*>", "[stripped_tag]", cleaned, flags=re.I)
    # Remove non-printable control characters except newline/tab
    cleaned = "".join(ch for ch in cleaned if ch in {"\n", "\t"} or ord(ch) >= 32)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


def wrap_untrusted_world_event(
    source_id: str,
    title: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Wrap untrusted external content inside explicit XML data boundaries for CognitionBrain."""
    safe_src = re.sub(r'[^a-zA-Z0-9_.:/-]', "_", str(source_id or "external"))[:128]
    safe_title = sanitize_external_text(title, max_chars=300)
    safe_body = sanitize_external_text(content, max_chars=2500)
    link = ""
    if metadata and metadata.get("link"):
        link = f"\nLink: {sanitize_external_text(str(metadata['link']), max_chars=300)}"
    return (
        f'<untrusted_world_event source="{safe_src}">\n'
        f"Title: {safe_title}{link}\n"
        f"Content: {safe_body}\n"
        f"</untrusted_world_event>"
    )
