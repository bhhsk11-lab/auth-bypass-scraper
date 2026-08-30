"""
═══════════════════════════════════════════════════════════════════════════
 Auth-Bypass Scraper v3.5 — Cloud Run Deployable Service
═══════════════════════════════════════════════════════════════════════════

 v3.5 changes (fixes EMPTY results on news.google.com/rss/articles/... URLs):
   - NEW: Layer 0 in /scrape and /pdf/direct — Google News redirect
     resolution. news.google.com/rss/articles/CBMi...?oc=5 URLs are
     protobuf-encoded click-tracking redirects, not article URLs. Fetching
     them "succeeds" (HTTP 200 from news.google.com) but yields only
     Google's JS shell — which showed up as 'curl_cffi✓' + 14-word EMPTY
     results with no error anywhere in the chain.
     Resolution: offline protobuf/base64 decode for old-style IDs →
     batchexecute RPC (Fbv4je/garturlreq) with data-n-a-sg/data-n-a-ts
     scraped from the redirect page for the new-style AU_yqL IDs that are
     unresolvable offline (the old base64 trick died July 2024).
     See scraper/gnews_resolver.py. Results cached (LRU, 2k), calls
     serialized + rate-limited, proxied via PROXY_URL (batchexecute is a
     hard 429 hotspot from datacenter IPs).
   - NEW: /scrape + /pdf/direct responses now include resolved_url (the
     real article URL) and original_url. /extract maps resolved_url
     through so the extension logs the actual publisher URL.
   - NEW: POST /gnews/resolve — standalone resolver endpoint so the
     extension's "Google resolve" step can use this server instead of its
     own (now-dead) offline decoder. This is what its "decoder-failed"
     errors were: attempting offline decode on new-style AU_yqL IDs.
   - /scrape fails fast (502) when a Google News URL can't be resolved,
     instead of burning the full browser pipeline on Google's shell page.

 v3.4 changes:
   - SOCIAL_REFERERS + ANTI_PAYWALL_COOKIES actually attached to curl_cffi
     attempts (were defined but never sent).
   - FlareSolverr (Layer 2.5) and ScraperAPI (Layer 4) now also run in
     /scrape, matching /pdf/direct. Both are no-ops unless their env vars
     are set.

 v3.3 changes:
   - StealthBrowser routes through PROXY_URL (scraper/browser.py).
   - Honest bypass_chain entries: stealth-browser⚠(loaded, no article),
     ✗ entries carry real exception messages, archive fallback logged.

 v3.2 changes:
   - /debug/pdf endpoint; residential-proxy routing for curl_cffi;
     FlareSolverr + ScraperAPI fallbacks in /pdf/direct; cookie warm-up;
     %PDF magic-byte validation; single consolidated browser-PDF strategy.

 Endpoints:
   POST /scrape            Full bypass pipeline for one URL
   POST /extract           NEWS BYTE extension compatibility shim (→ /scrape)
   POST /batch             Batch scrape (max 25)
   POST /pdf/direct        Gated PDF-viewer bypass (?u= decode)
   POST /pdf/extract       Extract text from uploaded base64 PDF (+OCR)
   POST /explore           Recursive site explorer
   POST /math/humanize     LaTeX → human-readable Unicode
   POST /contacts/decode   Cloudflare email/phone protection decoder
   POST /debug/pdf         CDN response diagnostics
   POST /gnews/resolve     ★ NEW: Google News URL → original publisher URL
   GET  /article/{id}      Cached article
   GET  /pdf/{id}          Cached PDF download
   GET  /health            Health check
   GET  /                  Service info
"""
import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from config import settings
from scraper.bypass import (
    StealthFetcher, cf_decode_email, cf_decode_phones, decode_cf_protections,
)
from scraper.browser import StealthBrowser
from scraper.extractors import extract_article, extract_links, extract_pdf_links, extract_image
from scraper.gnews_resolver import is_google_news_url, resolve_google_news
from scraper.math_pretty import humanize_formulas_in_text
from scraper.pdf_extract import extract_pdf

logger = logging.getLogger("auth-bypass-scraper")
logging.basicConfig(level=settings.log_level.upper())

app = FastAPI(title="AuthBypass Scraper", version="3.5.0")

_start_time = time.time()

# curl_cffi (TLS-impersonating HTTP client)
from curl_cffi import requests as cffi_requests  # noqa: E402

# httpx — used only for FlareSolverr / ScraperAPI calls
try:
    import httpx
except ImportError:
    httpx = None

# ═══════════════════════════════════════════════════════════════════════
# GLOBALS
# ═══════════════════════════════════════════════════════════════════════

_fetcher = StealthFetcher()
_browser: StealthBrowser | None = None
_browser_in_use = 0


class LRUCache:
    """Simple thread-safe in-memory LRU. Swap for Redis in multi-instance setups."""

    def __init__(self, max_items: int):
        self.max_items = max_items
        self._data: OrderedDict = OrderedDict()

    def get(self, key: str):
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: str, value):
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def __len__(self):
        return len(self._data)


_article_cache = LRUCache(settings.cache_max_items)
_pdf_cache = LRUCache(settings.cache_max_items)

_HAS_LATEX = re.compile(r"(\\\(|\\\[|\$\\?[a-zA-Z]|\\frac|\\sum|\\int)")


# ═══════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    global _browser
    _browser = StealthBrowser(headless=settings.browser_headless)
    await _browser.start()
    logger.info("Stealth browser ready")


@app.on_event("shutdown")
async def shutdown():
    global _browser
    if _browser:
        try:
            await _browser.stop()
        except Exception:
            pass
        _browser = None
    logger.info("Browser shut down")


async def get_browser() -> StealthBrowser:
    if _browser is None:
        raise HTTPException(503, "Browser not initialized")
    return _browser


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _cache_put(cache: LRUCache, key: str, value):
    cache.put(key, value)


def _cffi_proxies() -> dict | None:
    """Route HTTP fetches through residential proxy if configured.

    This is THE critical fix for Cloud Run: GCP egress IPs (AS15169) are
    datacenter IPs that Cloudflare challenges regardless of TLS/UA spoofing.
    """
    if settings.proxy_url:
        return {"http": settings.proxy_url, "https": settings.proxy_url}
    return None


async def _resolve_gnews_url(url: str, chain: list[str]) -> str:
    """
    Layer 0: if url is a Google News redirect, resolve it to the real
    publisher URL before running any bypass layers. Appends a chain entry
    describing what happened. Raises 502 (fails fast — Google's redirect
    page is a JS shell with no article, so running the full pipeline on
    it can only ever produce EMPTY 14-word results) when resolution
    fails and the URL is a Google News redirect.
    """
    if not is_google_news_url(url):
        return url
    resolved, how = await resolve_google_news(url)
    if resolved:
        chain.append(f"gnews-resolve✓({how})")
        return resolved
    chain.append(f"gnews-resolve✗({how})")
    raise HTTPException(502, detail={
        "success": False, "url": url, "bypass_chain": chain,
        "last_error": f"google-news resolve failed: {how}",
        "hint": "news.google.com/rss/articles/... URLs are protobuf "
                "redirects. Resolution needs batchexecute access; if this "
                "recurs, set PROXY_URL (residential) — Google 429s "
                "datacenter IPs on the batchexecute endpoint."})


def _normalize_direct_pdf_url(raw: str) -> str | None:
    """
    Normalize a decoded u= param into a fetchable URL.

    Handles both testbook variants:
      'cdn.testbook.com/foo.pdf'            → https://cdn.testbook.com/foo.pdf
      '/blogmedia.testbook.com/foo.pdf'     → https://blogmedia.testbook.com/foo.pdf
      'https://cdn.testbook.com/foo.pdf'    → unchanged

    v3.2 FIX: strips ALL leading slashes before prepending the scheme,
    so '%2Fblogmedia...' never becomes 'https:///blogmedia...'.
    """
    if not raw:
        return None
    raw = unquote(raw).strip()
    if raw.startswith(("http://", "https://")):
        return raw
    raw = raw.lstrip("/")          # ← the triple-slash fix
    if not raw:
        return None
    return f"https://{raw}"


def _is_pdf_bytes(body: bytes) -> bool:
    """Strict PDF magic-byte check. Challenge pages returned with HTTP 200
    fail this and fall through to the next strategy."""
    return bool(body) and body[:5].lstrip() == b"%PDF-"


def _looks_like_challenge(body: bytes) -> bool:
    head = body[:4096].lower()
    return (
        b"cf-challenge" in head
        or b"just a moment" in head
        or b"attention required" in head
        or b"checking your browser" in head
        or b"cf-browser-verification" in head
    )


def _viewer_u_param(url: str) -> str | None:
    """Extract the u= param from a gated PDF-viewer URL (testbook-style)."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("u", "url", "file", "pdf", "src"):
        if key in qs and qs[key]:
            return qs[key][0]
    return None


def _pdf_success(url: str, cdn_url: str, pdf_bytes: bytes,
                 chain: list[str]) -> dict:
    """Shared success-response builder for /pdf/direct."""
    pdf_result = extract_pdf(pdf_bytes)
    cid = _content_id(url)
    _cache_put(_pdf_cache, cid, pdf_bytes)

    text = pdf_result["text"]
    formulas: list[dict] = []
    if text and _HAS_LATEX.search(text):
        text, formulas = humanize_formulas_in_text(text)

    return {
        "success": True,
        "viewer_url": url,
        "cdn_url": cdn_url,
        "viewer_bypassed": True,
        "article_url": f"/article/{cid}",
        "pdf_url": f"/pdf/{cid}",
        "pdf_data": base64.b64encode(pdf_bytes).decode(),
        "pages": pdf_result["pages"],
        "text": text,
        "scanned": pdf_result["scanned"],
        "math": {"formulas_converted": formulas,
                 "count": len(formulas)},
        "bypass_chain": chain,
        "timestamp": _now_iso(),
    }


# ═══════════════════════════════════════════════════════════════════════
# ANTI-BLOCK FALLBACK LAYERS
# ═══════════════════════════════════════════════════════════════════════

async def _flaresolverr_fetch(url: str) -> tuple[str | None, list[dict]]:
    """
    Solve a Cloudflare challenge via FlareSolverr sidecar.
    Returns (page_html, cookies). The cookies — especially cf_clearance —
    are replayed on subsequent direct CDN fetches.
    """
    if not settings.flaresolverr_url or httpx is None:
        return None, []
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(settings.flaresolverr_url, json={
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
            })
            data = r.json()
            if data.get("status") == "ok":
                sol = data.get("solution", {})
                return sol.get("response", ""), sol.get("cookies", [])
    except Exception as e:
        logger.warning(f"FlareSolverr failed: {e}")
    return None, []


async def _scraperapi_fetch(url: str) -> bytes | None:
    """Last-resort: fetch via ScraperAPI's residential pool (solves CF)."""
    if not settings.scraperapi_key or httpx is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.get(
                "https://api.scraperapi.com/",
                params={
                    "api_key": settings.scraperapi_key,
                    "url": url,
                    "country_code": settings.scraperapi_country,
                },
                follow_redirects=True,
            )
            if r.status_code == 200 and _is_pdf_bytes(r.content):
                return r.content
            if r.status_code == 200 and not _looks_like_challenge(r.content):
                return r.content  # HTML page (e.g. article) — caller decides
    except Exception as e:
        logger.warning(f"ScraperAPI failed: {e}")
    return None


def _cffi_get(url: str, headers: dict, imposter: str = "chrome124",
              timeout: int = 30):
    """Blocking curl_cffi GET with proxy routing. Run via asyncio.to_thread."""
    return cffi_requests.get(
        url, headers=headers, impersonate=imposter,
        timeout=timeout, allow_redirects=True,
        proxies=_cffi_proxies(),
    )


# ═══════════════════════════════════════════════════════════════════════
# REQUEST MODELS
# ═══════════════════════════════════════════════════════════════════════

class ScrapeRequest(BaseModel):
    url: str
    want_pdf: bool = True
    want_text: bool = True
    humanize_math: bool = True
    explore: bool = False


class BatchRequest(BaseModel):
    urls: list[str] = Field(..., max_length=25)


class PdfDirectRequest(BaseModel):
    url: str


class PdfExtractRequest(BaseModel):
    pdf_base64: str
    filename: str = "upload.pdf"


class ExploreRequest(BaseModel):
    url: str
    depth: int = Field(default=2, ge=1, le=settings.explore_max_depth)
    max_pages: int = Field(default=20, ge=1, le=settings.explore_max_pages)


class MathRequest(BaseModel):
    text: str


class ExtractRequest(BaseModel):
    """Matches the request body the NEWS BYTE extension sends to BOTH of
    its Render servers (extractWithRenderServer() posts the same shape to
    whichever host it's trying)."""
    url: str
    render: bool = False
    max_chars: int = 60000


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /debug/pdf — run this FIRST when bypass fails
# ═══════════════════════════════════════════════════════════════════════

@app.post("/debug/pdf")
async def debug_pdf(req: PdfDirectRequest):
    """
    Diagnose exactly what the target returns from THIS instance's IP.
    Pass the DIRECT cdn URL (not the viewer URL).
    """
    url = _normalize_direct_pdf_url(req.url) or req.url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/pdf,*/*",
    }
    try:
        resp = await asyncio.to_thread(_cffi_get, url, headers)
        body_head = resp.content[:400]
        return {
            "requested": req.url,
            "normalized": url,
            "proxy_in_use": bool(settings.proxy_url),
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "server": resp.headers.get("server", ""),
            "cf_ray": resp.headers.get("cf-ray", ""),
            "is_real_pdf": _is_pdf_bytes(resp.content),
            "is_cf_challenge": _looks_like_challenge(resp.content),
            "body_head": body_head.decode("utf-8", errors="replace"),
        }
    except Exception as e:
        return {"requested": req.url, "normalized": url,
                "proxy_in_use": bool(settings.proxy_url),
                "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /gnews/resolve ★ NEW — standalone Google News URL resolver
# ═══════════════════════════════════════════════════════════════════════

@app.post("/gnews/resolve")
async def gnews_resolve_endpoint(req: PdfDirectRequest):
    """
    Resolve a news.google.com/rss/articles/... redirect to the original
    publisher URL. Use this instead of the extension's own (dead,
    offline-decode) Google resolver.
    """
    resolved, how = await resolve_google_news(req.url)
    if not resolved:
        raise HTTPException(502, detail={
            "success": False, "url": req.url,
            "error": how,
            "hint": "If this fails repeatedly, set PROXY_URL "
                    "(residential) — Google 429s batchexecute from "
                    "datacenter IPs."})
    return {"success": True, "original_url": req.url,
            "resolved_url": resolved, "method": how}


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /pdf/direct — gated PDF-viewer bypass
# ═══════════════════════════════════════════════════════════════════════

@app.post("/pdf/direct")
async def pdf_direct(req: PdfDirectRequest):
    """
    Bypass gated PDF viewers (testbook-style ?u= param).

    Strategy chain (each falls through to next on failure):
      0. Google News redirect resolution (if the URL is one) ★ NEW
      1. Decode u= param → direct CDN URLs (variants with/without scheme)
      2. Direct CDN fetch with viewer Referer (proxy-routed curl_cffi)
      2.5 FlareSolverr: solve CF challenge, replay cf_clearance cookies
      2.75 Cookie warm-up: stealth-browser visit to main domain, replay cookies
      3. Embedded-URL / base64 extraction from viewer page
      4. ScraperAPI residential-pool fetch (last resort)
      5. Stealth browser render + page.pdf()
    """
    url = req.url.strip()
    chain: list[str] = []

    # ── Step 0: Google News redirect resolution ────────────────────────
    url = await _resolve_gnews(url_=url, chain=chain)

    # ── Step 1: decode viewer u= param into direct CDN candidates ──────
    u_param = _viewer_u_param(url)
    direct_urls: list[str] = []
    if u_param:
        normalized = _normalize_direct_pdf_url(u_param)
        if normalized:
            direct_urls.append(normalized)
        chain.append(f"u-decode({normalized or 'failed'})")
    else:
        direct_urls.append(_normalize_direct_pdf_url(url) or url)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": url,                       # viewer URL as referer
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-site",
    }

    last_error = "no strategy succeeded"

    # ── Step 2: direct CDN fetches (proxy-routed) ──────────────────────
    for imposter in ("chrome124", "safari17_0", "edge101"):
        for target in direct_urls:
            try:
                resp = await asyncio.to_thread(
                    _cffi_get, target, headers, imposter)
                if resp.status_code == 200 and _is_pdf_bytes(resp.content):
                    chain.append(f"cdn-direct✓({imposter})")
                    return _pdf_success(url, target, resp.content, chain)
                if _looks_like_challenge(resp.content):
                    last_error = f"CF challenge on {target} " \
                                 f"(status {resp.status_code})"
                else:
                    body_head = resp.content[:20]
                    last_error = (f"{target} → HTTP {resp.status_code}, "
                                  f"body[:20]={body_head!r}")
            except Exception as e:
                last_error = f"{target} → {e}"

    # ── Step 2.5: FlareSolverr — solve challenge, replay cookies ───────
    if settings.flaresolverr_url:
        html, fs_cookies = await _flaresolverr_fetch(url)
        if html and not _looks_like_challenge(html.encode()):
            chain.append("flaresolverr-solved✓")
            cookie_header = "; ".join(
                f"{c['name']}={c['value']}" for c in fs_cookies
                if c.get("name") and c.get("value"))
            warm_headers = {**headers, **({"Cookie": cookie_header}
                                          if cookie_header else {})}
            m = re.search(
                r'https?://[^\s"\'<>\\]+?\.pdf[^\s"\'<>\\]*', html)
            candidates = ([m.group(0).replace("\\u002F", "/").replace("\\/", "/")]
                          if m else []) + direct_urls
            for target in candidates:
                try:
                    resp = await asyncio.to_thread(
                        _cffi_get, target, warm_headers)
                    if resp.status_code == 200 and _is_pdf_bytes(resp.content):
                        chain.append("flaresolverr+cookies✓")
                        return _pdf_success(url, target, resp.content, chain)
                except Exception as e:
                    last_error = f"flaresolverr-cookie-fetch {target} → {e}"

    # ── Step 2.75: cookie warm-up via stealth browser ──────────────────
    try:
        parsed = urlparse(url)
        main_domain = f"{parsed.scheme}://{parsed.netloc}/"
        browser = await get_browser()
        warm = await browser.fetch(main_domain, generate_pdf=False)
        if warm.get("cookies"):
            cookie_header = "; ".join(
                f"{c['name']}={c['value']}" for c in warm["cookies"]
                if c.get("name") and c.get("value"))
            if cookie_header:
                warm_headers = {**headers, "Cookie": cookie_header}
                chain.append("warm-session")
                for target in direct_urls:
                    try:
                        resp = await asyncio.to_thread(
                            _cffi_get, target, warm_headers)
                        if resp.status_code == 200 and _is_pdf_bytes(resp.content):
                            chain.append("warm-session✓")
                            return _pdf_success(url, target, resp.content, chain)
                    except Exception as e:
                        last_error = f"warm-fetch {target} → {e}"
    except Exception as e:
        logger.warning(f"Warm-up failed: {e}")

    # ── Step 3: fetch viewer page, look for embedded PDF URLs ─────────
    try:
        resp = await asyncio.to_thread(_cffi_get, url, headers)
        if resp.status_code == 200 and not _looks_like_challenge(resp.content):
            html = resp.text
            pdf_links = extract_pdf_links(html, base_url=url)
            if pdf_links:
                chain.append(f"viewer-embed({len(pdf_links)})")
                for pl in pdf_links[:3]:
                    try:
                        r2 = await asyncio.to_thread(
                            _cffi_get, pl, headers)
                        if r2.status_code == 200 and _is_pdf_bytes(r2.content):
                            chain.append("viewer-embed✓")
                            return _pdf_success(url, pl, r2.content, chain)
                    except Exception as e:
                        last_error = f"embed-fetch {pl} → {e}"
    except Exception as e:
        last_error = f"viewer-fetch → {e}"

    # ── Step 4: ScraperAPI last resort ─────────────────────────────────
    pdf_bytes = await _scraperapi_fetch(direct_urls[0] if direct_urls else url)
    if pdf_bytes and _is_pdf_bytes(pdf_bytes):
        chain.append("scraperapi✓")
        return _pdf_success(url, direct_urls[0], pdf_bytes, chain)

    # ── Step 5: stealth browser render as absolute last resort ─────────
    try:
        browser = await get_browser()
        result = await browser.fetch(url, generate_pdf=True)
        if result.get("pdf_base64"):
            chain.append("browser-render✓")
            pdf_bytes = base64.b64decode(result["pdf_base64"])
            cid = _content_id(url)
            _cache_put(_pdf_cache, cid, pdf_bytes)
            text = extract_pdf(pdf_bytes)["text"] if _is_pdf_bytes(pdf_bytes) \
                else result.get("text", "")
            formulas = []
            if text and _HAS_LATEX.search(text):
                text, formulas = humanize_formulas_in_text(text)
            return {
                "success": True,
                "viewer_url": url,
                "cdn_url": None,
                "viewer_bypassed": True,
                "article_url": f"/article/{cid}",
                "pdf_url": f"/pdf/{cid}",
                "pdf_data": result["pdf_base64"],
                "text": text,
                "pages": None,
                "scanned": False,
                "math": {"formulas_converted": formulas,
                         "count": len(formulas)},
                "bypass_chain": chain,
                "timestamp": _now_iso(),
            }
        last_error = result.get("last_error", "browser render produced no PDF")
    except Exception as e:
        last_error = f"browser-render → {e}"

    raise HTTPException(
        502,
        detail={"success": False, "url": url,
                "bypass_chain": chain, "last_error": last_error,
                "hint": "Set PROXY_URL (residential) or FLARESOLVERR_URL "
                        "env vars — datacenter IPs from Cloud Run are "
                        "challenged regardless of fingerprint spoofing. "
                        "Run POST /debug/pdf with the direct CDN URL to "
                        "confirm."})


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /scrape — full pipeline
# ═══════════════════════════════════════════════════════════════════════

@app.post("/scrape")
async def scrape(req: ScrapeRequest):
    """Full bypass pipeline: article text + PDFs + contacts + math."""
    original_url = req.url.strip()
    url = original_url
    chain: list[str] = []
    contact_meta: dict = {"emails": [], "phones": []}

    # ── Layer 0: Google News redirect resolution ★ NEW ─────────────────
    # news.google.com/rss/articles/CBMi...?oc=5 URLs are protobuf-encoded
    # click-tracking redirects. Fetching them directly "succeeds" (HTTP
    # 200 from news.google.com) but yields only Google's JS shell — the
    # source of EMPTY 14-word 'successes'. Resolve to the real publisher
    # URL first; fail fast if resolution fails, since no bypass layer can
    # extract an article from Google's shell page.
    url = await _resolve_gnews(url_=url, chain=chain)

    # ── Layer 1+2: fast HTTP path (curl_cffi, proxy-routed) ────────────
    html = None
    used_browser = False
    try:
        html = await _fetcher.fetch_html(url)
        chain.append("curl_cffi✓")
    except Exception as e:
        # str(e) is bypass.py's "All stealth HTTP attempts failed:
        # {last_err}" message — contains the real per-fingerprint reason.
        detail = str(e).strip().replace("\n", " ")[:200]
        chain.append(f"curl_cffi✗({type(e).__name__}: {detail})" if detail
                     else f"curl_cffi✗({type(e).__name__})")

    if html and _looks_like_challenge(html.encode()):
        chain.append("cf-challenge-detected")
        html = None

    # ── Layer 2.5: FlareSolverr (solves Cloudflare JS challenges) ──────
    if not html and settings.flaresolverr_url:
        fs_html, _fs_cookies = await _flaresolverr_fetch(url)
        if fs_html and not _looks_like_challenge(fs_html.encode()):
            html = fs_html
            chain.append("flaresolverr✓")
        else:
            chain.append("flaresolverr✗(no usable html)")

    # JSON-LD / __NEXT_DATA__ fast extraction from HTTP path
    article = None
    if html:
        article = extract_article(html, url)
        decoded = decode_cf_protections(html)
        contact_meta = {
            "emails": decoded.get("emails", []),
            "phones": decoded.get("phones", []),
        }

    needs_browser = (
        article is None
        or not article.get("text")
        or (req.want_pdf and not extract_pdf_links(article.get("html", html or ""),
                                                    base_url=url))
    )

    # ── Layer 3: stealth browser ───────────────────────────────────────
    if needs_browser:
        try:
            browser = await get_browser()
            result = await browser.fetch(url, generate_pdf=req.want_pdf)
            if result.get("html"):
                html = result["html"]
                article = extract_article(html, url)
                decoded = decode_cf_protections(html)
                contact_meta = {
                    "emails": decoded.get("emails", []),
                    "phones": decoded.get("phones", []),
                }
                used_browser = True
                if article and article.get("text"):
                    chain.append("stealth-browser✓")
                else:
                    # Browser loaded a page (valid HTML) but none of
                    # extract_article's strategies found real content —
                    # what a bot-check/consent/paywall page looks like.
                    err = result.get("last_error")
                    chain.append(
                        f"stealth-browser⚠(loaded, no article extracted"
                        f"{': ' + err if err else ''})"
                    )
            else:
                err = (result.get("last_error") or "no html returned").strip()[:200]
                chain.append(f"stealth-browser✗({err})")
        except Exception as e:
            detail = str(e).strip().replace("\n", " ")[:200]
            chain.append(f"browser✗({type(e).__name__}: {detail})" if detail
                         else f"browser✗({type(e).__name__})")

    if article is None or not article.get("text"):
        # ── Layer 4: ScraperAPI residential pool (last resort before
        # archive). The layer most likely to turn a hard IP-reputation
        # 403 (Reuters-style) into a 200 — only a different IP can do
        # that. Only runs if SCRAPERAPI_KEY is configured.
        if settings.scraperapi_key:
            sa_bytes = await _scraperapi_fetch(url)
            if sa_bytes and not _is_pdf_bytes(sa_bytes):
                sa_html = sa_bytes.decode("utf-8", errors="replace")
                sa_article = extract_article(sa_html, url)
                if sa_article and sa_article.get("text"):
                    html = sa_html
                    article = sa_article
                    decoded = decode_cf_protections(sa_html)
                    contact_meta = {
                        "emails": decoded.get("emails", []),
                        "phones": decoded.get("phones", []),
                    }
                    chain.append("scraperapi✓")
                else:
                    chain.append("scraperapi⚠(fetched, no article extracted)")
            else:
                chain.append("scraperapi✗(no usable html)")

    if article is None or not article.get("text"):
        # ── Layer 5: archive fallback ──────────────────────────────────
        try:
            archive_html = await _fetcher.fetch_archive(url)
            if archive_html:
                article = extract_article(archive_html, url)
                chain.append("archive✓" if article and article.get("text")
                             else "archive⚠(fetched, no article extracted)")
            else:
                chain.append("archive✗(no archived copy found)")
        except Exception as e:
            detail = str(e).strip().replace("\n", " ")[:200]
            chain.append(f"archive✗({type(e).__name__}: {detail})" if detail
                         else f"archive✗({type(e).__name__})")

    if article is None:
        raise HTTPException(502, detail={
            "success": False, "url": url, "original_url": original_url,
            "bypass_chain": chain,
            "last_error": "all layers failed — see bypass_chain"})

    cid = _content_id(url)
    article["id"] = cid
    _cache_put(_article_cache, cid, article)

    text = article.get("text", "")
    formulas: list[dict] = []
    if req.humanize_math and text and _HAS_LATEX.search(text):
        text, formulas = humanize_formulas_in_text(text)
        article["text"] = text

    # PDF links from final HTML
    final_html = article.get("html", html or "")
    pdf_links = extract_pdf_links(final_html, base_url=url)
    image = extract_image(final_html, url)

    return {
        "success": True,
        "url": url,                    # final (resolved) URL
        "original_url": original_url,  # what the caller sent
        "resolved_url": url,           # explicit alias for /extract shim
        "article_url": f"/article/{cid}",
        "pdf_url": None,
        "pdf_data": None,
        "pdf_links": pdf_links,
        "links": extract_links(final_html, base_url=url)[:100],
        "title": article.get("title"),
        "text": text if req.want_text else None,
        "image": image,
        "emails": contact_meta.get("emails", []),
        "phones": contact_meta.get("phones", []),
        "math": {"formulas_converted": formulas, "count": len(formulas)},
        "bypass_chain": chain,
        "used_browser": used_browser,
        "timestamp": _now_iso(),
    }


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /extract — compatibility shim for the NEWS BYTE extension
# ═══════════════════════════════════════════════════════════════════════
# The extension's extractWithRenderServer() always POSTs to "{server}/extract"
# with {url, render, max_chars} and reads back {paragraphs, text, image,
# word_count, method, resolved_url, extraction_score, errors, diagnostics}.
# v3.5: resolved_url now carries the REAL publisher URL (post-Google-News
# resolution) instead of the news.google.com redirect, so the extension's
# logs stop showing the CBMi... redirect as "resolved".
@app.post("/extract")
async def extract_compat(req: ExtractRequest):
    try:
        result = await scrape(ScrapeRequest(url=req.url, want_pdf=False))
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {}
        return {
            "paragraphs": [], "text": "", "image": "",
            "word_count": 0, "method": "bypass-failed",
            "resolved_url": req.url, "extraction_score": 0,
            "errors": [str(detail.get("last_error", e.detail))],
            "diagnostics": {"bypass_chain": detail.get("bypass_chain", [])},
        }
    text = (result.get("text") or "")[: req.max_chars]
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    return {
        "paragraphs": paragraphs,
        "text": text,
        "image": result.get("image") or "",
        "word_count": len(text.split()),
        "method": "bypass-browser" if result.get("used_browser") else "bypass-http",
        "resolved_url": result.get("resolved_url") or result.get("url", req.url),
        "extraction_score": 100 if len(text) >= 250 else 0,
        "errors": [],
        "diagnostics": {"bypass_chain": result.get("bypass_chain", [])},
    }


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /batch
# ═══════════════════════════════════════════════════════════════════════

@app.post("/batch")
async def batch(req: BatchRequest):
    if len(req.urls) > settings.max_batch:
        raise HTTPException(400, f"max {settings.max_batch} URLs per batch")
    results = await asyncio.gather(
        *[scrape(ScrapeRequest(url=u)) for u in req.urls],
        return_exceptions=True)
    out = []
    for u, r in zip(req.urls, results):
        if isinstance(r, Exception):
            detail = r.detail if isinstance(r, HTTPException) else str(r)
            out.append({"url": u, "success": False, "error": detail})
        else:
            out.append(r)
    return {"count": len(out), "results": out}


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /pdf/extract
# ═══════════════════════════════════════════════════════════════════════

@app.post("/pdf/extract")
async def pdf_extract(req: PdfExtractRequest):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
    except Exception:
        raise HTTPException(400, "Invalid base64")
    if len(pdf_bytes) > settings.pdf_max_bytes:
        raise HTTPException(413, f"PDF exceeds {settings.pdf_max_bytes} bytes")
    if not _is_pdf_bytes(pdf_bytes):
        raise HTTPException(400, "Not a valid PDF (missing %PDF magic bytes)")
    result = extract_pdf(pdf_bytes)
    text = result["text"]
    formulas = []
    if text and _HAS_LATEX.search(text):
        text, formulas = humanize_formulas_in_text(text)
    return {"success": True, "filename": req.filename,
            "pages": result["pages"], "scanned": result["scanned"],
            "text": text,
            "math": {"formulas_converted": formulas,
                     "count": len(formulas)},
            "timestamp": _now_iso()}


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /explore — recursive BFS site explorer
# ═══════════════════════════════════════════════════════════════════════

@app.post("/explore")
async def explore(req: ExploreRequest):
    from urllib.parse import urljoin, urldefrag

    base_netloc = urlparse(req.url).netloc
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(req.url, 0)]
    graph: dict[str, dict] = {}

    async def fetch_and_record(current: str, depth: int):
        if current in visited or len(visited) >= req.max_pages:
            return
        visited.add(current)
        entry: dict = {"depth": depth, "pdf_links": [], "emails": [],
                       "phones": [], "sublinks": []}
        html = None
        try:
            html = await _fetcher.fetch_html(current)
        except Exception:
            pass
        if not html or _looks_like_challenge(html.encode()):
            try:
                browser = await get_browser()
                result = await browser.fetch(current, generate_pdf=False)
                html = result.get("html")
            except Exception:
                html = None
        if not html:
            entry["error"] = "fetch failed"
            graph[current] = entry
            return
        decoded = decode_cf_protections(html)
        entry["emails"] = decoded.get("emails", [])
        entry["phones"] = decoded.get("phones", [])
        entry["pdf_links"] = extract_pdf_links(html, base_url=current)
        if depth < req.depth:
            links = extract_links(html, base_url=current)
            internal = []
            for link in links:
                clean = urldefrag(link)[0]
                if urlparse(clean).netloc == base_netloc and clean not in visited:
                    internal.append(clean)
            entry["sublinks"] = internal[:20]
            graph[current] = entry
            for sub in internal[:20]:
                await fetch_and_record(sub, depth + 1)
        else:
            graph[current] = entry

    await fetch_and_record(req.url, 0)
    return {"success": True, "root": req.url, "pages_visited": len(visited),
            "graph": graph, "timestamp": _now_iso()}


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /math/humanize and /contacts/decode
# ═══════════════════════════════════════════════════════════════════════

@app.post("/math/humanize")
async def math_humanize(req: MathRequest):
    if not req.text.strip():
        raise HTTPException(400, "Empty text")
    text, formulas = humanize_formulas_in_text(req.text)
    return {"success": True, "text": text,
            "formulas_converted": formulas, "count": len(formulas)}


@app.post("/contacts/decode")
async def contacts_decode(body: dict):
    html = body.get("html", "")
    if not html:
        raise HTTPException(400, "Provide 'html' field in JSON body")
    return decode_cf_protections(html)


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: caches / health / root
# ═══════════════════════════════════════════════════════════════════════

@app.get("/article/{article_id}")
async def get_article(article_id: str):
    article = _article_cache.get(article_id)
    if not article:
        raise HTTPException(404, f"Article '{article_id}' not in cache "
                                 f"(per-instance, resets on deploy)")
    return {**article, "id": article_id}


@app.get("/pdf/{pdf_id}")
async def get_pdf(pdf_id: str):
    pdf_bytes = _pdf_cache.get(pdf_id)
    if not pdf_bytes:
        raise HTTPException(404, f"PDF '{pdf_id}' not in cache")
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="{pdf_id}.pdf"'})


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "3.5",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "browsers_active": _browser_in_use,
        "proxy_configured": bool(settings.proxy_url),
        "flaresolverr_configured": bool(settings.flaresolverr_url),
        "scraperapi_configured": bool(settings.scraperapi_key),
        "cached_articles": len(_article_cache),
        "cached_pdfs": len(_pdf_cache),
",
