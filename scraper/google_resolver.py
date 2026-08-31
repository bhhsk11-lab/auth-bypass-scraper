"""Google News publisher URL resolver.

Resolves post-2024 Google News RSS article URLs
(news.google.com/rss/articles/...) to the real publisher article URL.

Design:
1. Validate that the input is actually a Google News article URL.
2. Fetch Google's article page and obtain data-n-a-id/data-n-a-sg/data-n-a-ts.
3. Use the documented-by-reverse-engineering garturl Fbv4je batchexecute RPC.
4. Parse garturlres specifically; never choose arbitrary URLs from the response.
5. If RPC is rate-limited/unavailable, use an independent Chromium page only
   as a fallback and extract publisher-looking canonical/OG/JSON-LD/anchor URLs.
6. Never accept Google infrastructure, XML namespaces, schema URLs, assets,
   tracking URLs, or arbitrary third-party links as the publisher URL.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("google_resolver")

GOOGLE_HOST = "news.google.com"
BATCH_ENDPOINT = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

# Keep Google requests bounded. Do not create a browser page per feed item
# unless the RPC path really fails.
PAGE_CONCURRENCY = 3
BATCH_CONCURRENCY = 1
REQUEST_TIMEOUT = httpx.Timeout(7.0, connect=4.0, read=6.0, write=6.0, pool=4.0)
MAX_RETRIES = 2
CACHE_TTL = 6 * 60 * 60
NEGATIVE_TTL = 12
CACHE_MAX = 2000
RPC_MIN_INTERVAL = 0.75
BROWSER_NAV_TIMEOUT_MS = 7000
BROWSER_POLL_MS = 250
BROWSER_POLLS = 12

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

_RPC_HEADERS = {
    "User-Agent": _BROWSER_HEADERS["User-Agent"],
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://news.google.com",
    "Referer": "https://news.google.com/",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}


@dataclass
class ResolveResult:
    url: str
    method: str
    error: str | None = None


class GoogleNewsResolver:
    def __init__(self) -> None:
        self._cache: OrderedDict[str, tuple[float, ResolveResult]] = OrderedDict()
        self._page_sem = asyncio.Semaphore(PAGE_CONCURRENCY)
        self._rpc_sem = asyncio.Semaphore(BATCH_CONCURRENCY)
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._last_rpc_at = 0.0
        self._browser = None
        self._playwright = None
        self._browser_context = None
        self._browser_lock = asyncio.Lock()
        self._browser_page_sem = asyncio.Semaphore(2)
        self._inflight: dict[str, asyncio.Future] = {}

    async def client(self) -> httpx.AsyncClient:
        if self._client and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    headers=_BROWSER_HEADERS,
                    follow_redirects=True,
                    max_redirects=6,
                    timeout=REQUEST_TIMEOUT,
                    http2=True,
                )
        return self._client

    async def close(self) -> None:
        if self._browser_context is not None:
            try:
                await self._browser_context.close()
            except Exception:
                pass
            self._browser_context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get_browser(self):
        """Independent Chromium used only for Google News resolution."""
        if self._browser_context is not None:
            return self._browser_context
        async with self._browser_lock:
            if self._browser_context is not None:
                return self._browser_context
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError("playwright-not-installed") from exc

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._browser_context = await self._browser.new_context(
                user_agent=_BROWSER_HEADERS["User-Agent"],
                viewport={"width": 1365, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
                ignore_https_errors=False,
            )
            await self._browser_context.set_extra_http_headers(
                {"Accept-Language": "en-US,en;q=0.9"}
            )
            return self._browser_context

    @staticmethod
    def is_google_url(url: str) -> bool:
        try:
            p = urlparse(str(url).strip())
            host = (p.hostname or "").lower().rstrip(".")
            return host == GOOGLE_HOST and (
                p.path.startswith("/rss/articles/")
                or p.path.startswith("/articles/")
                or p.path.startswith("/read/")
            )
        except Exception:
            return False

    @staticmethod
    def article_id(url: str) -> str | None:
        try:
            p = urlparse(url)
            part = p.path.rstrip("/").split("/")[-1]
            return unquote(part) if part else None
        except Exception:
            return None

    @staticmethod
    def _host_is_google_infra(host: str) -> bool:
        h = (host or "").lower().rstrip(".")
        return (
            h == "google.com" or h.endswith(".google.com")
            or h == "gstatic.com" or h.endswith(".gstatic.com")
            or h == "googleusercontent.com" or h.endswith(".googleusercontent.com")
            or h == "googleapis.com" or h.endswith(".googleapis.com")
            or h == "ggpht.com" or h.endswith(".ggpht.com")
            or h == "googlevideo.com" or h.endswith(".googlevideo.com")
        )

    @staticmethod
    def _host_is_non_article_infra(host: str) -> bool:
        h = (host or "").lower().rstrip(".")
        exact = {
            "w3.org", "www.w3.org",
            "schema.org", "www.schema.org",
            "xml.org", "www.xml.org",
            "example.com", "example.org", "example.net",
            "localhost",
        }
        if h in exact:
            return True
        if h.endswith(".w3.org") or h.endswith(".schema.org"):
            return True
        # Common tracking/asset/CDN infrastructure. These are not accepted
        # unless the actual publisher domain is separately identified.
        return any(x in h for x in (
            "doubleclick.net", "googlesyndication.com",
            "google-analytics.com", "googletagmanager.com",
            "googleadservices.com", "facebook.com", "facebook.net",
            "twitter.com", "x.com", "youtube.com", "youtube-nocookie.com",
            "instagram.com", "linkedin.com",
        ))

    @classmethod
    def _valid_destination(cls, value: str | None) -> bool:
        """Strict publisher URL gate.

        A URL is NOT a publisher merely because it is external to Google.
        This is the critical protection against values such as
        https://www.w3.org/XML/1998/namespace appearing in XML/HTML.
        """
        if not value:
            return False
        try:
            value = unquote(str(value)).strip()
            p = urlparse(value)
            host = (p.hostname or "").lower().rstrip(".")
            if p.scheme not in ("http", "https") or not host:
                return False
            if cls._host_is_google_infra(host) or cls._host_is_non_article_infra(host):
                return False
            if host.startswith(("cdn.", "static.", "assets.", "fonts.", "img.")):
                # Do not blindly reject real publishers using these prefixes;
                # require an article-like path below.
                path = (p.path or "").lower()
                if not any(x in path for x in (
                    "/article", "/news/", "/story/", "/stories/",
                    "/world/", "/business/", "/sports/", "/technology/",
                )):
                    return False

            path = (p.path or "").lower()
            if re.search(
                r"\.(?:js|css|mjs|woff2?|ttf|otf|eot|png|jpe?g|gif|webp|svg|ico|"
                r"mp4|webm|mp3|wav|json|xml)(?:$|\?)",
                path,
            ):
                return False

            # XML namespace / schema URLs often have these path forms.
            if host in {"w3.org", "www.w3.org"}:
                return False
            if path in {"/xml/1998/namespace", "/2001/xml.xsd"}:
                return False

            return True
        except Exception:
            return False

    @classmethod
    def _article_like_score(cls, url: str, anchor_text: str = "") -> int:
        p = urlparse(url)
        path = (p.path or "").lower()
        score = 0
        if len(path.strip("/")) >= 20:
            score += 8
        if any(x in path for x in (
            "/article", "/news/", "/story/", "/stories/", "/world/",
            "/business/", "/sports/", "/technology/", "/politics/",
            "/india/", "/entertainment/", "/science/", "/health/",
        )):
            score += 15
        if path in ("", "/"):
            score -= 35
        text = (anchor_text or "").strip().lower()
        if len(text) >= 25:
            score += 4
        if text and any(x in text for x in ("read", "article", "news", "story")):
            score += 3
        return score

    @classmethod
    def _browser_candidates(cls, html: str, current_url: str) -> list[str]:
        """Extract only plausible publisher URLs from Google-rendered HTML.

        Never regex every URL in the page and accept the first external URL.
        That old behavior is exactly how the W3 XML namespace became a fake
        publisher.
        """
        scored: dict[str, int] = {}
        soup = BeautifulSoup(html or "", "lxml")

        def add(value: str | None, base_score: int, text: str = "") -> None:
            if not value:
                return
            value = unquote(str(value)).strip()
            if value.startswith("//"):
                value = "https:" + value
            absolute = urljoin(current_url, value).split("#", 1)[0]
            if not cls._valid_destination(absolute):
                return
            score = base_score + cls._article_like_score(absolute, text)
            scored[absolute] = max(scored.get(absolute, -999), score)

        # Canonical / OG are strongest. These should beat arbitrary links.
        for tag_name, attrs, field, score in (
            ("link", {"rel": lambda v: v and "canonical" in v}, "href", 120),
            ("meta", {"property": "og:url"}, "content", 115),
            ("meta", {"name": "twitter:url"}, "content", 105),
        ):
            for tag in soup.find_all(tag_name, attrs=attrs):
                add(tag.get(field), score)

        # JSON-LD URLs, but only after strict validation.
        for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
            try:
                data = json.loads(script.string or script.get_text() or "")
            except Exception:
                continue

            def walk(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k == "url" and isinstance(v, str):
                            add(v, 100)
                        else:
                            walk(v)
                elif isinstance(obj, list):
                    for item in obj:
                        walk(item)

            walk(data)

        # Google sometimes renders the source as an ordinary external anchor.
        # Anchor text is retained for scoring, unlike the old raw URL regex.
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            add(a.get("href"), 65, text)

        return [u for u, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))]

    @staticmethod
    def _extract_params(html: str, article_id: str) -> dict[str, str] | None:
        soup = BeautifulSoup(html or "", "lxml")
        # Prefer the node whose data-n-a-id matches the actual article token.
        nodes = soup.select("c-wiz > div[data-n-a-sg][data-n-a-ts]")
        nodes += soup.select("div[data-n-a-sg][data-n-a-ts]")
        best = None
        for node in nodes:
            sig = node.get("data-n-a-sg")
            ts = node.get("data-n-a-ts")
            data_id = node.get("data-n-a-id") or article_id
            if not sig or not ts or not data_id:
                continue
            candidate = {"id": data_id, "sig": sig, "ts": ts}
            if data_id == article_id:
                return candidate
            if best is None:
                best = candidate
        if best:
            return best

        patterns = [
            r'data-n-a-id=["\']([^"\']+)["\'][^>]*'
            r'data-n-a-sg=["\']([^"\']+)["\'][^>]*'
            r'data-n-a-ts=["\']([^"\']+)',
            r'data-n-a-sg=["\']([^"\']+)["\'][^>]*'
            r'data-n-a-ts=["\']([^"\']+)',
        ]
        for i, pat in enumerate(patterns):
            m = re.search(pat, html or "", re.I | re.S)
            if m:
                if i == 0:
                    return {"id": m.group(1), "sig": m.group(2), "ts": m.group(3)}
                return {"id": article_id, "sig": m.group(1), "ts": m.group(2)}
        return None

    @classmethod
    def _legacy_extract(cls, value: str) -> str | None:
        """Conservative compatibility fallback for older embedded URLs."""
        seen: set[str] = set()
        queue = [value]
        for _ in range(8):
            if not queue:
                break
            current = queue.pop(0)
            if current in seen or len(current) > 200000:
                continue
            seen.add(current)
            for m in re.finditer(r"https?://[^\s\"'<>\\]+", current):
                candidate = unquote(m.group(0)).rstrip(".,)]}")
                if cls._valid_destination(candidate):
                    return candidate
            try:
                for vals in parse_qs(urlparse(current).query).values():
                    queue.extend(vals)
            except Exception:
                pass
            raw = current.split("?", 1)[0].rstrip("/").split("/")[-1]
            for candidate in (raw, current):
                if len(candidate) < 20 or len(candidate) % 4 == 1:
                    continue
                try:
                    padded = candidate + "=" * (-len(candidate) % 4)
                    decoded = base64.urlsafe_b64decode(padded.encode()).decode("utf-8", "ignore")
                    if decoded and decoded != current and re.search(r"https?://|www\.", decoded):
                        queue.append(decoded)
                except Exception:
                    pass
        return None

    @staticmethod
    def _rpc_payload(params: list[dict[str, str]]) -> str:
        """Build the current Fbv4je/garturlreq payload.

        The X placeholders are intentional; this is the request shape used by
        current community implementations. The old resolver used a different
        first nested array, which is more brittle against Google's changes.
        """
        requests = []
        for p in params:
            art = json.dumps(
                [
                    "garturlreq",
                    [
                        [
                            "X", "X", ["X", "X"], None, None, 1, 1,
                            "US:en", None, 1, None, None, None, None,
                            None, 0, 1,
                        ],
                        "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
                    ],
                    p["id"], int(float(p["ts"])), p["sig"],
                ],
                separators=(",", ":"),
            )
            requests.append(["Fbv4je", art, None, "generic"])
        return urlencode({"f.req": json.dumps([requests], separators=(",", ":"))})

    @classmethod
    def _urls_from_rpc(cls, text: str) -> list[str]:
        """Parse the batchexecute response.

        First try the exact garturlres field. Only then use a strict recursive
        URL scan as a compatibility fallback.
        """
        candidates: list[str] = []

        # Exact garturlres extraction. Google wraps the response in an
        # anti-XSSI prefix/newline and nested JSON.
        marker = '\\"garturlres\\",\\"'
        pos = text.find(marker)
        if pos >= 0:
            start = pos + len(marker)
            end = text.find('\\",', start)
            if end > start:
                raw = text[start:end]
                raw = raw.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
                raw = unquote(raw)
                if cls._valid_destination(raw):
                    candidates.append(raw)

        # JSON-aware parsing for current/variant response wrappers.
        for fragment in (text, text.split("\n\n", 1)[1] if "\n\n" in text else ""):
            if not fragment:
                continue
            try:
                obj = json.loads(fragment)
            except Exception:
                continue

            def walk(x):
                if isinstance(x, list):
                    for v in x:
                        walk(v)
                elif isinstance(x, dict):
                    for k, v in x.items():
                        if k == "garturlres" and isinstance(v, str):
                            u = unquote(v).replace("\\u003d", "=").replace("\\u0026", "&")
                            if cls._valid_destination(u):
                                candidates.append(u)
                        walk(v)
                elif isinstance(x, str):
                    # Do not inspect XML namespaces or arbitrary HTML as a
                    # source of publisher URLs. Only explicit URL strings that
                    # pass the strict destination gate are accepted.
                    if x.startswith(("http://", "https://")) and cls._valid_destination(x):
                        candidates.append(x)
                    if x.startswith(("[", "{")):
                        try:
                            walk(json.loads(x))
                        except Exception:
                            pass

            walk(obj)

        return list(dict.fromkeys(candidates))

    async def _fetch_params(self, article_id: str) -> dict[str, str]:
        client = await self.client()
        last = "params-unavailable"

        async with self._page_sem:
            for target in (
                f"https://news.google.com/rss/articles/{article_id}",
                f"https://news.google.com/articles/{article_id}",
            ):
                for attempt in range(MAX_RETRIES + 1):
                    try:
                        response = await client.get(target)
                        if response.status_code == 429:
                            last = "google-429"
                        elif response.status_code == 200:
                            params = self._extract_params(response.text, article_id)
                            if params:
                                return params
                            legacy = self._legacy_extract(response.text)
                            if legacy:
                                return {"legacy_url": legacy}
                            last = "signature-not-found"
                        else:
                            last = f"google-http-{response.status_code}"
                    except Exception as exc:
                        last = f"params-{type(exc).__name__}"

                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(0.35 * (attempt + 1) + random.random() * 0.2)
        raise RuntimeError(last)

    async def _rpc_decode(self, params: dict[str, str]) -> str:
        client = await self.client()
        async with self._rpc_sem:
            for attempt in range(MAX_RETRIES + 1):
                wait = RPC_MIN_INTERVAL - (time.monotonic() - self._last_rpc_at)
                if wait > 0:
                    await asyncio.sleep(wait)

                try:
                    self._last_rpc_at = time.monotonic()
                    response = await client.post(
                        BATCH_ENDPOINT,
                        content=self._rpc_payload([params]),
                        headers=_RPC_HEADERS,
                    )

                    if response.status_code == 429:
                        raise RuntimeError("google-rpc-429")
                    if response.status_code >= 500:
                        raise RuntimeError(f"google-rpc-http-{response.status_code}")
                    response.raise_for_status()

                    urls = self._urls_from_rpc(response.text)
                    if urls:
                        return urls[0]
                    raise RuntimeError("google-rpc-empty")
                except Exception as exc:
                    if attempt >= MAX_RETRIES:
                        raise
                    # 429 deserves a longer pause than a transient 5xx.
                    delay = (1.0 + attempt * 1.5) if "429" in str(exc) else (0.5 + attempt * 0.7)
                    await asyncio.sleep(delay + random.random() * 0.5)

        raise RuntimeError("google-rpc-failed")

    async def _resolve_http(self, url: str) -> ResolveResult:
        article_id = self.article_id(url)
        if not article_id:
            return ResolveResult(url, "invalid-google-url", "missing-article-id")
        try:
            params = await self._fetch_params(article_id)
            if params.get("legacy_url") and self._valid_destination(params["legacy_url"]):
                return ResolveResult(params["legacy_url"], "legacy-embedded")

            destination = await self._rpc_decode(params)
            if self._valid_destination(destination):
                return ResolveResult(destination, "batchexecute")

            return ResolveResult(url, "failed", "google-rpc-destination-rejected")
        except Exception as exc:
            return ResolveResult(url, "failed", str(exc)[:240])

    async def _browser_resolve(self, url: str) -> ResolveResult:
        """Last-resort browser resolver.

        Browser resolution is deliberately NOT allowed to accept arbitrary
        external URLs from the Google page. It only accepts canonical/OG/
        JSON-LD/article-anchor candidates that pass the strict destination gate.
        """
        context = await self._get_browser()
        async with self._browser_page_sem:
            page = await context.new_page()
            try:
                page.set_default_navigation_timeout(BROWSER_NAV_TIMEOUT_MS)
                page.set_default_timeout(2500)
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=BROWSER_NAV_TIMEOUT_MS,
                )

                for _ in range(BROWSER_POLLS):
                    current = page.url
                    if current != url and self._valid_destination(current):
                        return ResolveResult(current, "browser")

                    html = await page.content()
                    candidates = self._browser_candidates(html, current)
                    if candidates:
                        # Only return a high-confidence browser candidate.
                        # Do not use a generic homepage as a false positive.
                        best = candidates[0]
                        if self._article_like_score(best) >= 8:
                            return ResolveResult(best, "browser")

                    await asyncio.sleep(BROWSER_POLL_MS / 1000.0)

                return ResolveResult(url, "browser-failed", "publisher-url-not-observed")
            except Exception as exc:
                return ResolveResult(url, "browser-failed", f"{type(exc).__name__}: {exc}"[:240])
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    def _cache_get(self, key: str) -> ResolveResult | None:
        item = self._cache.get(key)
        if not item:
            return None
        expires, result = item
        if expires <= time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return ResolveResult(result.url, "failed" if result.method == "failed" else "cache", result.error)

    def _cache_put(self, key: str, result: ResolveResult, ttl: float = CACHE_TTL) -> None:
        if ttl <= 0:
            return
        self._cache[key] = (time.monotonic() + ttl, result)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)

    async def resolve(self, url: str) -> ResolveResult:
        url = str(url).strip()
        if not self.is_google_url(url):
            return ResolveResult(url, "passthrough")

        cached = self._cache_get(url)
        if cached:
            return cached

        existing = self._inflight.get(url)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._inflight[url] = future

        try:
            result = await self._resolve_uncached(url)
            if not future.done():
                future.set_result(result)
            return result
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(url, None)

    async def _resolve_uncached(self, url: str) -> ResolveResult:
        # IMPORTANT: the authoritative path is the garturl RPC. Browser is a
        # fallback only. This prevents Google page assets/namespaces from being
        # mistaken for publisher URLs.
        http_result = await self._resolve_http(url)
        if (
            http_result.method not in ("failed", "invalid-google-url")
            and self._valid_destination(http_result.url)
        ):
            self._cache_put(url, http_result)
            return http_result

        browser_result = await self._browser_resolve(url)
        if self._valid_destination(browser_result.url) and browser_result.method.startswith("browser"):
            self._cache_put(url, browser_result)
            return browser_result

        detail = "; ".join(
            x for x in [http_result.error, browser_result.error] if x
        )
        result = ResolveResult(url, "failed", detail[:300] or "google-url-unresolved")
        self._cache_put(url, result, ttl=NEGATIVE_TTL)
        return result

    async def resolve_many(self, urls: list[str]) -> list[ResolveResult]:
        sem = asyncio.Semaphore(PAGE_CONCURRENCY)

        async def one(u: str):
            async with sem:
                return await self.resolve(u)

        return await asyncio.gather(*(one(u) for u in urls))


resolver = GoogleNewsResolver()
