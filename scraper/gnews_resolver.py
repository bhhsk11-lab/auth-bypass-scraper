"""Robust Google News URL resolver.

Resolves Google News article redirects to the publisher URL without ever
accepting arbitrary URLs (for example googleusercontent image URLs) found in
Google's HTML.

Resolution order:
  1. Direct URL query parameter, when present and valid.
  2. Embedded/legacy URL decoding from the article token.
  3. Google's current batchexecute/garturlreq flow using data-n-a-sg and
     data-n-a-ts from the article page.
  4. googlenewsdecoder package fallback, when installed.

Returns: (resolved_url | None, method_or_error)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from config import settings

logger = logging.getLogger("gnews-resolver")

BATCHEXECUTE_URL = (
    "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"
)

CACHE_MAX = 2000
_CACHE: OrderedDict[str, str] = OrderedDict()
_LOCK = asyncio.Lock()
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.80

GOOGLE_HOSTS = {
    "google.com",
    "www.google.com",
    "news.google.com",
    "googleusercontent.com",
    "www.googleusercontent.com",
    "gstatic.com",
    "www.gstatic.com",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


def _proxy() -> str | None:
    return getattr(settings, "proxy_url", None) or None


def _proxies() -> dict[str, str] | None:
    p = _proxy()
    return {"http": p, "https": p} if p else None


def is_google_news_url(url: str) -> bool:
    try:
        p = urlparse(str(url).strip())
        host = (p.hostname or "").lower().rstrip(".")
        return host == "news.google.com" and any(
            p.path.rstrip("/").startswith(prefix)
            for prefix in ("/rss/articles/", "/articles/", "/read/")
        )
    except Exception:
        return False


def _cache_get(url: str) -> str | None:
    value = _CACHE.get(url)
    if value:
        _CACHE.move_to_end(url)
    return value


def _cache_put(url: str, resolved: str) -> None:
    _CACHE[url] = resolved
    _CACHE.move_to_end(url)
    while len(_CACHE) > CACHE_MAX:
        _CACHE.popitem(last=False)


def _article_id(url: str) -> str | None:
    try:
        p = urlparse(url)
        parts = [unquote(x) for x in p.path.split("/") if x]
        for marker in ("rss", "articles", "read"):
            if marker in parts:
                i = parts.index(marker)
                if i + 1 < len(parts):
                    token = parts[i + 1].strip()
                    if token:
                        return token
        return None
    except Exception:
        return None


def _looks_like_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip().strip("\"'")
    try:
        p = urlparse(value)
        return p.scheme in {"http", "https"} and bool(p.hostname)
    except Exception:
        return False


def _is_google_internal_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return True
    return (
        host == "news.google.com"
        or host.endswith(".google.com")
        or host == "google.com"
        or host.endswith(".googleusercontent.com")
        or host.endswith(".gstatic.com")
    )


def _valid_publisher_url(value: Any, original: str) -> str | None:
    """Accept only a real HTTP(S) publisher URL; reject Google internals."""
    if not _looks_like_http_url(value):
        return None

    candidate = str(value).strip()
    if candidate == original:
        return None
    if _is_google_internal_url(candidate):
        return None

    try:
        p = urlparse(candidate)
        if not p.hostname:
            return None
        # Never accept a URL that is just a Google redirect host.
        return candidate
    except Exception:
        return None


def _decode_offline(token: str) -> str | None:
    """Best-effort decoding for older Google News tokens.

    This is intentionally conservative: decoded text is returned only when it
    is an actual HTTP(S) URL. It is never used as a generic HTML URL scraper.
    """
    raw_token = token.split("?", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(raw_token + "=" * (-len(raw_token) % 4))
    except (ValueError, binascii.Error):
        return None

    # Search decoded bytes for a complete URL. Older tokens often contain the
    # source URL as a length-delimited protobuf field. Search is limited to
    # ASCII URL bytes and then validated strictly.
    for match in re.finditer(rb"https?://[^\x00\"'<>\s]+", raw):
        try:
            candidate = match.group(0).decode("utf-8", "strict")
        except UnicodeDecodeError:
            continue
        candidate = candidate.rstrip(".,);]")
        if _looks_like_http_url(candidate) and not _is_google_internal_url(candidate):
            return candidate

    return None


def _get_page(url: str) -> tuple[str, dict[str, str], int]:
    response = cffi_requests.get(
        url,
        impersonate="chrome124",
        timeout=20,
        allow_redirects=True,
        proxies=_proxies(),
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": UA,
            "Cache-Control": "no-cache",
        },
    )
    cookies = {str(k): str(v) for k, v in response.cookies.items()}
    return response.text, cookies, response.status_code


def _extract_decoding_params(html: str, article_id: str) -> tuple[str | None, str | None]:
    """Extract sig/timestamp only from Google's article metadata."""
    soup = BeautifulSoup(html, "lxml")

    candidates = []
    for node in soup.find_all(attrs={"data-n-a-sg": True}):
        candidates.append(node)
    for node in soup.find_all(attrs={"data-n-a-ts": True}):
        if node not in candidates:
            candidates.append(node)

    # Prefer the node whose data-n-a-id equals our exact article token.
    exact = [n for n in candidates if n.get("data-n-a-id") == article_id]
    ordered = exact + [n for n in candidates if n not in exact]

    for node in ordered:
        sig = node.get("data-n-a-sg")
        ts = node.get("data-n-a-ts")
        if sig and ts and str(ts).isdigit():
            return str(sig), str(ts)

    # Fallback to the common c-wiz > div structure used by Google's page.
    node = soup.select_one("c-wiz > div[data-n-a-sg][data-n-a-ts]")
    if node is not None:
        sig = node.get("data-n-a-sg")
        ts = node.get("data-n-a-ts")
        if sig and ts and str(ts).isdigit():
            return str(sig), str(ts)

    # Last resort: attribute regex, still requiring both attributes in the
    # same HTML neighborhood rather than extracting arbitrary href/src URLs.
    m = re.search(
        r'data-n-a-sg=["\']([^"\']+)["\'][^>]{0,1200}?'
        r'data-n-a-ts=["\'](\d+)["\']',
        html,
        re.I | re.S,
    )
    if m:
        return m.group(1), m.group(2)

    m = re.search(
        r'data-n-a-ts=["\'](\d+)["\'][^>]{0,1200}?'
        r'data-n-a-sg=["\']([^"\']+)["\']',
        html,
        re.I | re.S,
    )
    if m:
        return m.group(2), m.group(1)

    return None, None


def _build_freq(article_id: str, timestamp: str, signature: str) -> str:
    """Build Google's Fbv4je/garturlreq request."""
    garturlreq = [
        "garturlreq",
        [
            [
                "X",
                "X",
                ["X", "X"],
                None,
                None,
                1,
                1,
                "US:en",
                None,
                1,
                None,
                None,
                None,
                None,
                0,
                1,
            ],
            "X",
            "X",
            1,
            [1, 1, 1],
            1,
            1,
            None,
            0,
            0,
            None,
            0,
        ],
        article_id,
        int(timestamp),
        signature,
    ]

    rpc = ["Fbv4je", json.dumps(garturlreq, separators=(",", ":"))]
    outer = json.dumps([[rpc]], separators=(",", ":"))
    return "f.req=" + quote(outer, safe="")


def _post_batchexecute(body: str, cookies: dict[str, str]) -> str:
    response = cffi_requests.post(
        BATCHEXECUTE_URL,
        data=body,
        impersonate="chrome124",
        timeout=25,
        allow_redirects=True,
        proxies=_proxies(),
        cookies=cookies,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Accept": "*/*",
            "Origin": "https://news.google.com",
            "Referer": "https://news.google.com/",
            "User-Agent": UA,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"batchexecute-http-{response.status_code}")
    return response.text


def _walk_garturlres(value: Any) -> str | None:
    """Find only values belonging to a garturlres response record."""
    if isinstance(value, list):
        if value and value[0] == "garturlres":
            if len(value) > 1 and _looks_like_http_url(value[1]):
                return value[1]
        for item in value:
            found = _walk_garturlres(item)
            if found:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = _walk_garturlres(item)
            if found:
                return found
    return None


def _parse_batchexecute(text: str, original_url: str) -> str | None:
    """Parse Google's XSSI/JSON-ish batchexecute response robustly."""
    # First parse every plausible JSON fragment after Google's XSSI prefix.
    fragments = []
    stripped = text.lstrip()
    if stripped.startswith(")]}'"):
        stripped = stripped[4:].lstrip("\r\n")
    fragments.append(stripped)

    # Google commonly separates the JSON payload from a prefix with blank
    # lines. Trying each non-empty line also handles variant responses.
    fragments.extend(x.strip() for x in text.split("\n") if x.strip())

    seen = set()
    for fragment in fragments:
        if fragment in seen:
            continue
        seen.add(fragment)
        try:
            data = json.loads(fragment)
        except Exception:
            continue
        candidate = _walk_garturlres(data)
        valid = _valid_publisher_url(candidate, original_url)
        if valid:
            return valid

    # Fallback: locate an encoded garturlres JSON string, but still validate
    # only the URL immediately associated with that marker.
    patterns = [
        r'garturlres\\?"\s*,\s*\\?"(https?://[^"\\]+)',
        r'garturlres["\']\s*,\s*["\'](https?://[^"\']+)',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            candidate = m.group(1).replace(r"\/", "/")
            valid = _valid_publisher_url(candidate, original_url)
            if valid:
                return valid

    return None


def _decode_with_batchexecute(url: str, article_id: str) -> tuple[str | None, str]:
    """Resolve one URL using Google's official-in-practice internal RPC flow."""
    errors = []

    # Current decoder implementations fetch /articles first, then fall back
    # to /rss/articles when extracting data-n-a-sg/data-n-a-ts.
    page_urls = [
        f"https://news.google.com/articles/{quote(article_id, safe='')}",
        f"https://news.google.com/rss/articles/{quote(article_id, safe='')}",
    ]

    for page_url in page_urls:
        try:
            html, cookies, status = _get_page(page_url)
            if status != 200 or not html:
                errors.append(f"params-http-{status}")
                continue

            sig, ts = _extract_decoding_params(html, article_id)
            if not sig or not ts:
                errors.append("decoding-params-not-found")
                continue

            body = _build_freq(article_id, ts, sig)
            response_text = _post_batchexecute(body, cookies)
            resolved = _parse_batchexecute(response_text, url)
            if resolved:
                return resolved, "batchexecute"

            errors.append("garturlres-not-found")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:100]}")

    return None, "batchexecute-failed:" + ";".join(errors[-4:])


def _decode_with_package(url: str) -> tuple[str | None, str]:
    """Fallback to googlenewsdecoder 0.1.7 if installed."""
    try:
        from googlenewsdecoder import gnewsdecoder
    except Exception:
        return None, "googlenewsdecoder-not-installed"

    try:
        kwargs = {"interval": 1}
        proxy = _proxy()
        if proxy:
            kwargs["proxy"] = proxy

        result = gnewsdecoder(url, **kwargs)
        if isinstance(result, dict) and result.get("status"):
            candidate = _valid_publisher_url(result.get("decoded_url"), url)
            if candidate:
                return candidate, "googlenewsdecoder"
            return None, "googlenewsdecoder-invalid-result"

        message = result.get("message") if isinstance(result, dict) else "decode-failed"
        return None, f"googlenewsdecoder-failed:{str(message)[:120]}"
    except Exception as exc:
        return None, f"googlenewsdecoder-error:{type(exc).__name__}:{str(exc)[:100]}"


async def resolve_google_news(url: str) -> tuple[str | None, str]:
    """Resolve a Google News URL.

    Non-Google-News URLs pass through unchanged.
    A failed Google News resolution returns (None, reason), never a random
    Google image/internal URL.
    """
    original = str(url).strip()

    if not is_google_news_url(original):
        return original, "not-a-gnews-url"

    cached = _cache_get(original)
    if cached:
        return cached, "cache"

    parsed = urlparse(original)
    qs = parse_qs(parsed.query)
    direct = qs.get("url", [None])[0]
    valid_direct = _valid_publisher_url(direct, original)
    if valid_direct:
        _cache_put(original, valid_direct)
        return valid_direct, "url-param"

    article_id = _article_id(original)
    if not article_id:
        return None, "no-article-id"

    # Old Google News tokens can contain the original URL directly.
    offline = _decode_offline(article_id)
    valid_offline = _valid_publisher_url(offline, original)
    if valid_offline:
        _cache_put(original, valid_offline)
        return valid_offline, "b64-decode"

    global _LAST_CALL

    async with _LOCK:
        delay = _LAST_CALL + _MIN_INTERVAL - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        _LAST_CALL = time.monotonic()

        resolved, method = await asyncio.to_thread(
            _decode_with_batchexecute, original, article_id
        )
        if resolved:
            _cache_put(original, resolved)
            return resolved, method

        # Package fallback is deliberately after the direct implementation.
        resolved, package_method = await asyncio.to_thread(
            _decode_with_package, original
        )
        if resolved:
            _cache_put(original, resolved)
            return resolved, package_method

        return None, f"{method};{package_method}"
