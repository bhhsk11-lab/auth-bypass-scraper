"""Google News URL resolver with an independent Chromium fallback.

Primary path: Google News page parameters + batchexecute RPC.
Fallback path: a dedicated Playwright Chromium context that opens the exact
Google News URL independently and discovers the publisher URL from navigation,
canonical/meta/JSON-LD links, or external anchors.

The browser resolver is intentionally separate from the publisher browser:
Google resolution must not inherit publisher-page routing, cookies, challenge
state, or tracker-blocking rules.
"""
import asyncio
import base64
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from config import settings

logger = logging.getLogger("google_resolver")

GOOGLE_HOST = "news.google.com"
BATCH_ENDPOINT = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

# HTTP/RPC is deliberately conservative. The browser fallback is the
# independent source of truth when Google's internal RPC is unavailable.
PAGE_CONCURRENCY = 1
BATCH_CONCURRENCY = 1
REQUEST_TIMEOUT = httpx.Timeout(8.0, connect=5.0, read=7.0, write=7.0, pool=5.0)
MAX_RETRIES = 1
CACHE_TTL = 6 * 60 * 60
CACHE_MAX = 3000
NEGATIVE_TTL = 0  # never poison a Google URL after a transient failure
RPC_MIN_INTERVAL = 1.0
BROWSER_SETTLE_MS = 2500
BROWSER_SECOND_SETTLE_MS = 2500

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

_RPC_HEADERS = {
    "User-Agent": _BROWSER_UA,
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

        # Independent browser state. This is NOT the publisher StealthBrowser.
        self._pw = None
        self._browser = None
        self._browser_context = None
        self._browser_lock = asyncio.Lock()
        self._browser_sem = asyncio.Semaphore(1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the independent Google-resolution Chromium once per process."""
        if self._browser_context:
            return
        async with self._browser_lock:
            if self._browser_context:
                return
            self._pw = await async_playwright().start()
            proxy = self._playwright_proxy()
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            }
            if proxy:
                launch_kwargs["proxy"] = proxy
            self._browser = await self._pw.chromium.launch(**launch_kwargs)
            self._browser_context = await self._browser.new_context(
                user_agent=_BROWSER_UA,
                locale="en-US",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
                ignore_https_errors=False,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            # No browser timeout is imposed here. The caller's HTTP request
            # is not aborted by a resolver timer, and Playwright navigation is
            # started at `commit` so a slow page cannot block waiting for every
            # subresource before we inspect the destination.
            self._browser_context.set_default_timeout(0)
            self._browser_context.set_default_navigation_timeout(0)
            await self._browser_context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            logger.info("Independent Google News browser resolver ready")

    async def close(self) -> None:
        if self._browser_context:
            try:
                await self._browser_context.close()
            except Exception:
                pass
            self._browser_context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _playwright_proxy() -> dict | None:
        if not settings.proxy_url:
            return None
        try:
            p = urlparse(settings.proxy_url)
            if not p.scheme or not p.hostname:
                return None
            out = {"server": f"{p.scheme}://{p.hostname}:{p.port}" if p.port else f"{p.scheme}://{p.hostname}"}
            if p.username:
                out["username"] = p.username
            if p.password:
                out["password"] = p.password
            return out
        except Exception:
            logger.warning("Invalid PROXY_URL for Google browser; using direct egress")
            return None

    # ------------------------------------------------------------------
    # Classification / validation
    # ------------------------------------------------------------------
    @staticmethod
    def is_google_url(url: str) -> bool:
        try:
            p = urlparse(str(url))
            return (p.hostname or "").lower() == GOOGLE_HOST and (
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
    def _host_is_google(host: str) -> bool:
        h = (host or "").lower().rstrip(".")
        return h == "google.com" or h.endswith(".google.com") or h == "googleusercontent.com" or h.endswith(".googleusercontent.com") or h == "gstatic.com" or h.endswith(".gstatic.com")

    @classmethod
    def _valid_destination(cls, value: str | None) -> bool:
        if not value:
            return False
        try:
            p = urlparse(value)
            host = (p.hostname or "").lower()
            if p.scheme not in ("http", "https") or not host:
                return False
            if cls._host_is_google(host):
                return False
            # Ignore obvious telemetry/asset destinations if found in DOM.
            if any(x in host for x in ("doubleclick.net", "googlesyndication.com", "google-analytics.com")):
                return False
            return True
        except Exception:
            return False

    @classmethod
    def _normalize_candidate(cls, value: str, base_url: str) -> str | None:
        try:
            value = unquote(value).strip().replace("\\/", "/")
            if not value or value.startswith(("javascript:", "data:", "mailto:", "tel:")):
                return None
            from urllib.parse import urljoin
            absolute = urljoin(base_url, value)
            p = urlparse(absolute)
            if p.scheme not in ("http", "https"):
                return None
            # Remove fragments only; preserve query because it can be part of
            # the publisher's canonical URL.
            return absolute.split("#", 1)[0] if cls._valid_destination(absolute) else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------
    def _cache_get(self, key: str) -> ResolveResult | None:
        item = self._cache.get(key)
        if not item:
            return None
        expires, result = item
        if expires <= time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return ResolveResult(result.url, "cache", result.error)

    def _cache_put(self, key: str, result: ResolveResult, ttl: float = CACHE_TTL) -> None:
        if ttl <= 0:
            return
        self._cache[key] = (time.monotonic() + ttl, result)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)

    # ------------------------------------------------------------------
    # HTTP/RPC resolver
    # ------------------------------------------------------------------
    async def client(self) -> httpx.AsyncClient:
        if self._client and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    headers=_BROWSER_HEADERS,
                    follow_redirects=True,
                    max_redirects=8,
                    timeout=REQUEST_TIMEOUT,
                    http2=True,
                )
        return self._client

    @staticmethod
    def _extract_params(html: str, article_id: str) -> dict[str, str] | None:
        soup = BeautifulSoup(html, "lxml")
        candidates = soup.select("c-wiz > div[data-n-a-sg][data-n-a-ts]")
        candidates += soup.select("div[data-n-a-sg][data-n-a-ts]")
        for node in candidates:
            sig = node.get("data-n-a-sg")
            ts = node.get("data-n-a-ts")
            data_id = node.get("data-n-a-id") or article_id
            if sig and ts and data_id:
                return {"id": data_id, "sig": sig, "ts": ts}
        patterns = [
            r'data-n-a-id=["\']([^"\']+)["\'][^>]*data-n-a-sg=["\']([^"\']+)["\'][^>]*data-n-a-ts=["\']([^"\']+)',
            r'data-n-a-sg=["\']([^"\']+)["\'][^>]*data-n-a-ts=["\']([^"\']+)',
        ]
        for i, pat in enumerate(patterns):
            m = re.search(pat, html, re.I | re.S)
            if m:
                if i == 0:
                    return {"id": m.group(1), "sig": m.group(2), "ts": m.group(3)}
                return {"id": article_id, "sig": m.group(1), "ts": m.group(2)}
        return None

    @classmethod
    def _legacy_extract(cls, value: str) -> str | None:
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
        requests = []
        for p in params:
            art = json.dumps([
                "garturlreq",
                [[
                    ["en-US", "US", ["FINANCE_TOP_INDICES", "WEB_TEST_1_0"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                    p["id"], int(float(p["ts"])), p["sig"],
                ]],
            ], separators=(",", ":"))
            requests.append(["Fbv4je", art, None, "generic"])
        return urlencode({"f.req": json.dumps([requests], separators=(",", ":"))})

    @classmethod
    def _urls_from_rpc(cls, text: str) -> list[str]:
        candidates: list[str] = []
        fragments = [text]
        if "\n\n" in text:
            fragments.append(text.split("\n\n", 1)[1])
        for fragment in fragments:
            try:
                obj = json.loads(fragment)
            except Exception:
                continue
            stack = [obj]
            while stack:
                cur = stack.pop()
                if isinstance(cur, str):
                    if "http" in cur:
                        for m in re.finditer(r"https?://[^\s\"'<>\\]+", cur):
                            u = unquote(m.group(0)).replace("\\u003d", "=").replace("\\u0026", "&").rstrip(".,)]}")
                            if cls._valid_destination(u):
                                candidates.append(u)
                    if cur.startswith(("[", "{")):
                        try:
                            stack.append(json.loads(cur))
                        except Exception:
                            pass
                elif isinstance(cur, list):
                    stack.extend(cur)
                elif isinstance(cur, dict):
                    stack.extend(cur.values())
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
                        r = await client.get(target)
                        if r.status_code == 429:
                            last = "google-429"
                        elif r.status_code == 200:
                            params = self._extract_params(r.text, article_id)
                            if params:
                                return params
                            legacy = self._legacy_extract(r.text)
                            if legacy:
                                return {"legacy_url": legacy}
                            last = "signature-not-found"
                        else:
                            last = f"google-http-{r.status_code}"
                    except Exception as exc:
                        last = f"params-{type(exc).__name__}"
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(0.4 * (attempt + 1))
        raise RuntimeError(last)

    async def _rpc_decode(self, params: dict[str, str]) -> str:
        client = await self.client()
        async with self._rpc_sem:
            wait = RPC_MIN_INTERVAL - (time.monotonic() - self._last_rpc_at)
            if wait > 0:
                await asyncio.sleep(wait)
            for attempt in range(MAX_RETRIES + 1):
                try:
                    self._last_rpc_at = time.monotonic()
                    response = await client.post(BATCH_ENDPOINT, content=self._rpc_payload([params]), headers=_RPC_HEADERS)
                    if response.status_code == 429:
                        raise RuntimeError("google-rpc-429")
                    if response.status_code >= 500:
                        raise RuntimeError(f"google-rpc-http-{response.status_code}")
                    response.raise_for_status()
                    urls = self._urls_from_rpc(response.text)
                    if urls:
                        return urls[0]
                    raise RuntimeError("google-rpc-empty")
                except Exception:
                    if attempt >= MAX_RETRIES:
                        raise
                    await asyncio.sleep(0.8 * (attempt + 1))
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
            return ResolveResult(url, "failed", "google-rpc-no-publisher-url")
        except Exception as exc:
            return ResolveResult(url, "failed", str(exc)[:200])

    # ------------------------------------------------------------------
    # Independent browser resolver
    # ------------------------------------------------------------------
    @classmethod
    async def _page_candidates(cls, page, source_url: str) -> list[str]:
        """Collect publisher candidates without trusting a single DOM field."""
        candidates: list[tuple[str, int]] = []
        final_url = page.url
        if cls._valid_destination(final_url):
            candidates.append((final_url, 100))

        try:
            values = await page.evaluate("""
            () => {
              const out=[];
              const add=(v,score)=>{if(v) out.push([v,score]);};
              const canon=document.querySelector('link[rel="canonical"]');
              if(canon?.href) add(canon.href,95);
              const og=document.querySelector('meta[property="og:url"]');
              if(og?.content) add(og.content,90);
              document.querySelectorAll('script[type="application/ld+json"]').forEach(s=>{
                try{
                  const walk=x=>{
                    if(!x)return;
                    if(Array.isArray(x)){x.forEach(walk);return;}
                    if(typeof x==='object'){
                      if(typeof x.url==='string') add(x.url,88);
                      Object.values(x).forEach(walk);
                    }
                  };
                  walk(JSON.parse(s.textContent||''));
                }catch{}
              });
              document.querySelectorAll('a[href]').forEach(a=>{
                const h=a.href||'';
                if(/^https?:/i.test(h)) add(h,60);
              });
              return out;
            }
            """)
            candidates.extend(values)
        except Exception:
            pass

        clean: dict[str, int] = {}
        for raw, score in candidates:
            u = cls._normalize_candidate(raw, source_url)
            if not u:
                continue
            host = (urlparse(u).hostname or "").lower()
            # Prefer article-like URLs over generic publisher homepages.
            path = urlparse(u).path.lower()
            bonus = 0
            if len(path.strip("/")) > 12:
                bonus += 8
            if any(x in path for x in ("/article", "/news/", "/story/", "/stories/", "/world/", "/business/", "/technology/")):
                bonus += 10
            if host in {"facebook.com", "x.com", "twitter.com", "youtube.com", "instagram.com", "linkedin.com"}:
                bonus -= 50
            clean[u] = max(clean.get(u, -999), int(score) + bonus)

        return [u for u, _ in sorted(clean.items(), key=lambda kv: kv[1], reverse=True)]

    async def _resolve_browser(self, url: str) -> ResolveResult:
        """Resolve a Google News URL using a completely independent browser.

        The browser is deliberately independent from the publisher scraper:
        no publisher routes, cookie jar, tracker filters, or HTTP session are
        reused.  We open the exact Google URL, allow client-side navigation to
        settle, then inspect navigation + structured metadata + links.  If the
        page presents a normal public article link without navigating itself,
        we follow the best candidate once in the same browser and use the
        resulting URL as the publisher destination.
        """
        await self.start()
        if not self._browser_context:
            return ResolveResult(url, "browser-failed", "google-browser-not-ready")

        async with self._browser_sem:
            page = await self._browser_context.new_page()
            page.set_default_navigation_timeout(0)
            page.set_default_timeout(0)
            try:
                # Exact original URL.  Do not add query parameters or rewrite
                # the Google News token before browser navigation.
                await page.goto(url, wait_until="domcontentloaded", timeout=0)
                await page.wait_for_timeout(BROWSER_SETTLE_MS)

                candidates = await self._page_candidates(page, url)
                for candidate in candidates:
                    if self._valid_destination(candidate):
                        logger.info("Google browser resolved %s -> %s", url, candidate)
                        return ResolveResult(candidate, "browser")

                # Some Google News pages expose the publisher only as a normal
                # article anchor after client-side rendering. Follow the best
                # external candidate once. This is navigation, not an RPC call.
                if candidates:
                    target = candidates[0]
                    try:
                        await page.goto(target, wait_until="domcontentloaded", timeout=0)
                        await page.wait_for_timeout(BROWSER_SECOND_SETTLE_MS)
                        final = page.url
                        if self._valid_destination(final):
                            logger.info("Google browser followed publisher %s -> %s", target, final)
                            return ResolveResult(final, "browser-follow")
                    except Exception as exc:
                        logger.debug("Google browser candidate follow failed: %s", exc)

                # One final DOM inspection catches delayed navigation/metadata.
                candidates = await self._page_candidates(page, url)
                for candidate in candidates:
                    if self._valid_destination(candidate):
                        logger.info("Google browser resolved on final inspection %s -> %s", url, candidate)
                        return ResolveResult(candidate, "browser")

                title = ""
                try:
                    title = (await page.title()).strip()[:120]
                except Exception:
                    pass
                return ResolveResult(
                    url,
                    "browser-failed",
                    f"publisher-url-not-found{': ' + title if title else ''}",
                )
            except Exception as exc:
                return ResolveResult(url, "browser-failed", f"{type(exc).__name__}: {exc}"[:300])
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def resolve(self, url: str) -> ResolveResult:
        url = str(url).strip()
        if not self.is_google_url(url):
            return ResolveResult(url, "passthrough")

        cached = self._cache_get(url)
        if cached:
            return cached

        # Browser-first is intentional. Google frequently rate-limits the
        # internal batchexecute RPC. The independent Chromium path does not
        # depend on that RPC and opens the exact user-supplied Google URL.
        browser_result = await self._resolve_browser(url)
        if self._valid_destination(browser_result.url):
            self._cache_put(url, browser_result)
            return browser_result

        # Only after the browser has failed do we spend an RPC attempt. This
        # keeps the normal path independent of Google's internal endpoint and
        # prevents a 429 from becoming the sole source of truth.
        http_result = await self._resolve_http(url)
        if http_result.method not in ("failed", "invalid-google-url") and self._valid_destination(http_result.url):
            self._cache_put(url, http_result)
            return http_result

        # Do not negative-cache failures. A transient Google/network problem
        # must not poison the same article for subsequent requests.
        detail = "; ".join(x for x in [browser_result.error, http_result.error] if x)
        return ResolveResult(url, "failed", detail[:300] or "google-url-unresolved")

    async def resolve_many(self, urls: list[str]) -> list[ResolveResult]:
        # Keep association exact. The browser fallback itself is serialized so
        # multiple simultaneous Google tabs cannot create a burst of Chromium
        # sessions against Google.
        sem = asyncio.Semaphore(PAGE_CONCURRENCY)

        async def one(u: str):
            async with sem:
                return await self.resolve(u)

        return await asyncio.gather(*(one(u) for u in urls))


resolver = GoogleNewsResolver()
