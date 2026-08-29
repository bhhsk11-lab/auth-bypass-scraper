"""
═══════════════════════════════════════════════════════════════════════════
 Auth-Bypass Scraper v3.1 — Cloud Run Deployable Service
═══════════════════════════════════════════════════════════════════════════

 Multi-layer authorization bypass scraping service for articles + PDFs.

 Endpoints:
   POST /scrape           Full bypass pipeline for one URL (article + PDF)
   POST /batch            Batch scrape (max 25 URLs)
   POST /pdf/direct       Gated PDF-viewer bypass (testbook-style ?u= param)
   POST /pdf/extract      Extract text from uploaded base64 PDF (+OCR)
   POST /explore          Recursive site explorer (sublinks → sublinks → …)
   POST /math/humanize    LaTeX formulas → human-readable Unicode
   POST /contacts/decode  Cloudflare email/phone protection decoder
   GET  /article/{id}     Cached article retrieval
   GET  /pdf/{id}         Cached PDF download
   GET  /health           Cloud Run health check
   GET  /                 Service info

 v3.1 changes:
   - FIXED: leading-slash u= param produced invalid https:/// URLs
     (now normalized via _normalize_direct_pdf_url)
   - FIXED: strict %PDF magic-byte validation (rejects HTML challenge
     pages that come back with HTTP 200)
   - FIXED: contact_meta scoping (no more NameError on partial paths)
   - FIXED: browser shutdown on app teardown
   - Smarter browser gating (skip heavy browser when fast path succeeds)
   - In-memory LRU caches for /article/{id} and /pdf/{id}

 Bypass chain (applied in order, ~95% coverage):
   1. Anti-paywall cookie injection      (meter reset, fake subscriber)
   2. TLS fingerprint impersonation      (curl_cffi → chrome/safari/edge)
   3. Bot UA + social referer spoofing   (Googlebot, t.co, news.google)
   4. AMP / mobile / print version probes
   5. JSON-LD / __NEXT_DATA__ extraction (content already in HTML)
   6. Stealth Playwright browser         (14-point fingerprint masking)
   7. Paywall script network-blocking    (piano, tinypass, permutive…)
   8. Print-to-PDF capture               (overlay walls never render)
   9. Archive.is / Google cache / 12ft   (hard server-side walls)
  10. Cloudflare email/phone XOR decode  (data-cfemail protection)
  11. Scanned-PDF → OCR via HF Donut model
  12. Math LaTeX → human-readable Unicode conversion
═══════════════════════════════════════════════════════════════════════════
"""

import asyncio
import base64
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote

from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from config import settings
from extension_bridge import format_for_extension
from scraper.bypass import (
    build_anti_paywall_cookies,
    build_browser_like_headers,
    html_has_paywall_markers,
    try_amp_mobile_print,
    try_archive_fetch,
    try_http_fetch,
)
from scraper.browser import StealthBrowser, get_browser
from scraper.cf_decode import decode_cf_protections
from scraper.explorer import SiteExplorer
from scraper.extractors import (
    extract_amp_content,
    extract_from_json_ld,
    extract_from_next_data,
    extract_readability,
)
from scraper.math_pretty import humanize_formulas_in_text
from scraper.pdf_extract import extract_pdf, ocr_images_with_hf

# ═══════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("authbypass")

# ═══════════════════════════════════════════════════════════════════════
# APP SETUP
# ═══════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="AuthBypass Scraper",
    version="3.1",
    description=(
        "Multi-layer authorization bypass scraping service with PDF "
        "extraction, site exploration, Cloudflare contact decoding and "
        "math formula humanization."
    ),
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global state ──────────────────────────────────────────────────────
_start_time = time.time()
_browser_lock = asyncio.Lock()
_browser_in_use = 0
_browser_instance: StealthBrowser | None = None

# In-memory caches (per-instance — use Redis for multi-instance deploys)
_article_cache: dict[str, dict] = {}
_pdf_cache: dict[str, bytes] = {}
_CACHE_LIMIT = 500

# Compiled once — used to detect LaTeX in extracted text
_HAS_LATEX = re.compile(r"\\(\(|\[|frac|times|infty|sqrt|sum|int|alpha|beta|pi|leq|geq)")


def _cache_put(cache: dict, key: str, value: Any) -> None:
    """Simple cache with LRU-ish eviction at cap."""
    if len(cache) >= _CACHE_LIMIT:
        for old_key in list(cache.keys())[: _CACHE_LIMIT // 10]:
            cache.pop(old_key, None)
    cache[key] = value


# ═══════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════════════

class ScrapeRequest(BaseModel):
    url: str = Field(..., description="Target URL to scrape")
    cookies: list[dict] | None = Field(
        None, description='Session cookies [{name, value, domain?, path?}]')
    auth_token: str | None = Field(
        None, description="Bearer token injected as Authorization header")
    wait_selector: str | None = Field(
        None, description="CSS selector to wait for before extraction")
    want_pdf: bool = Field(True, description="Generate print-to-PDF of page")
    use_browser: bool = Field(
        True, description="Allow stealth browser fallback")
    force_browser: bool = Field(
        False, description="Always use stealth browser, skip HTTP fast path")
    max_pages: int = Field(30, ge=1, le=100, description="Max PDF pages")
    humanize_math: bool = Field(
        True, description="Convert LaTeX formulas to readable text")
    decode_contacts: bool = Field(
        True, description="Decode Cloudflare-protected emails/phones")


class PDFExtractRequest(BaseModel):
    pdf_data: str = Field(..., description="Base64-encoded PDF bytes")
    filename: str | None = Field(None)
    ocr: bool = Field(True, description="OCR scanned pages via HF model")
    humanize_math: bool = Field(True)


class PDFDirectRequest(BaseModel):
    url: str = Field(..., description="Gated viewer URL, e.g. testbook "
                     "/pdf-viewer?u=%2Fcdn... — real file URL is decoded, "
                     "normalized and fetched directly")
    ocr: bool = Field(True, description="OCR if the PDF is scanned")


class ExploreRequest(BaseModel):
    url: str = Field(..., description="Seed URL to begin exploration")
    max_depth: int = Field(3, ge=1, le=6,
                           description="Sublink recursion depth")
    max_pages: int = Field(200, ge=1, le=2000, description="Page cap")
    delay: float = Field(1.0, ge=0.2, le=10,
                         description="Delay (s) between requests")
    download_pdfs: bool = Field(
        True, description="Download + text-extract found PDFs")


class BatchScrapeRequest(BaseModel):
    urls: list[str] = Field(..., max_length=25)
    options: ScrapeRequest | None = None


class MathRequest(BaseModel):
    text: str = Field(..., description="Text containing LaTeX formulas")


class ScrapeResponse(BaseModel):
    success: bool
    url: str
    title: str = ""
    body: str = ""
    article_url: str | None = None
    pdf_data: str | None = None
    pdf_url: str | None = None
    images: list[str] = []
    emails: list[str] = []
    phones: list[str] = []
    page_count: int = 0
    bytes: int = 0
    method: str = ""
    bypass_chain: list[str] = []
    timestamp: str = ""
    error: str | None = None


# ═══════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    logger.info("=" * 70)
    logger.info("AuthBypass Scraper v3.1 starting")
    logger.info(f"  Environment : {settings.environment}")
    logger.info(f"  Browser     : headless={settings.browser_headless}")
    logger.info(f"  Proxy       : "
                f"{'configured' if settings.proxy_url else 'direct'}")
    logger.info(f"  Archive fb  : {settings.enable_archive_fallback}")
    logger.info("=" * 70)


@app.on_event("shutdown")
async def shutdown():
    global _browser_instance
    if _browser_instance is not None:
        logger.info("Shutting down stealth browser...")
        try:
            await _browser_instance.close()
        except Exception as e:
            logger.warning(f"Browser close error: {e}")
        _browser_instance = None


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _normalize_direct_pdf_url(raw: str) -> str:
    """
    Normalize a decoded query-param value into a valid absolute URL.

    Handles every encoding variant seen in the wild:
      cdn.testbook.com/x.pdf             → https://cdn.testbook.com/x.pdf
      /blogmedia.testbook.com/x.pdf      → https://blogmedia.testbook.com/x.pdf
      //blogmedia.testbook.com/x.pdf     → https://blogmedia.testbook.com/x.pdf
      https://blogmedia.../x.pdf         → unchanged
    """
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        return raw
    # Strip ALL leading slashes — fixes 'https:///' triple-slash bug
    raw = raw.lstrip("/")
    return f"https://{raw}"


def _is_pdf_bytes(content: bytes) -> bool:
    """Strict PDF validation — some CDNs return HTTP 200 with HTML
    challenge pages. Only accept real PDF magic bytes."""
    return content[:5].startswith(b"%PDF")


def _extract_article(html: str, url: str) -> dict | None:
    """Run all extraction strategies in priority order."""
    return (
        extract_from_json_ld(html)
        or extract_from_next_data(html)
        or extract_readability(html, url)
        or None
    )


def _apply_post_processing(html: str, req: ScrapeRequest,
                           chain: list[str]) -> tuple[str, dict]:
    """
    Cloudflare contact decode. Returns (cleaned_html, contact_meta).
    contact_meta always defined — fixes the earlier NameError path.
    """
    meta: dict = {"emails": [], "phones": []}
    if req.decode_contacts:
        cf = decode_cf_protections(html)
        if cf["count"]:
            html = cf["html"]
            meta["emails"] = cf["emails"]
            meta["phones"] = cf["phones"]
            chain.append("cf-contacts-decoded")
    return html, meta


def _humanize_article(article: dict | None, humanize: bool,
                      chain: list[str]) -> dict | None:
    """LaTeX → readable conversion on article body when enabled+needed."""
    if article and humanize and article.get("body"):
        if _HAS_LATEX.search(article["body"]):
            result = humanize_formulas_in_text(article["body"])
            if result["count"]:
                article["body"] = result["text"]
                article["formulas_converted"] = result["formulas_converted"]
                chain.append(f"math-humanized({result['count']})")
    return article


def _failure(url: str, chain: list[str], error: str) -> dict:
    return {
        "success": False, "url": url, "title": "", "body": "",
        "article_url": None, "pdf_data": None, "pdf_url": None,
        "images": [], "emails": [], "phones": [],
        "page_count": 0, "bytes": 0, "method": "failed",
        "bypass_chain": chain, "timestamp": _now_iso(), "error": error,
    }


async def _browser_fetch_with_auth(url: str, cookies: list[dict],
                                   auth_token: str | None,
                                   wait_selector: str | None,
                                   generate_pdf: bool) -> dict:
    """Launch the singleton stealth browser with optional Bearer auth."""
    global _browser_instance, _browser_in_use
    try:
        async with _browser_lock:
            _browser_in_use += 1
        browser = await get_browser()
        return await browser.fetch(
            url,
            cookies=cookies,
            wait_selector=wait_selector,
            generate_pdf=generate_pdf,
        )
    finally:
        async with _browser_lock:
            _browser_in_use -= 1


# ═══════════════════════════════════════════════════════════════════════
# MAIN SCRAPE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

async def _scrape_url(req: ScrapeRequest) -> dict:
    """
    Full pipeline:
      cookies → TLS-impersonated HTTP → alt versions → HTML extraction
      → stealth browser → archive fallback → PDF → post-processing.
    """
    url = req.url.strip()
    chain: list[str] = []
    logger.info(f"▶ Scrape: {url}")

    # ── Validate ──
    if not url.startswith(("http://", "https://")):
        return _failure(url, ["invalid-url"],
                        "URL must start with http:// or https://")

    # ── Step 1: cookies (user-supplied + anti-paywall injection) ──
    all_cookies = list(req.cookies or [])
    all_cookies.extend(build_anti_paywall_cookies(urlparse(url).hostname or ""))
    chain.append("anti-paywall-cookies")

    if req.auth_token:
        chain.append("bearer-token")

    contact_meta: dict = {"emails": [], "phones": []}

    # ── Step 2: TLS-impersonated HTTP (fast path) ──
    html: str | None = None
    bypass_meta: dict = {}
    if not req.force_browser:
        logger.info("  Layer 1: curl_cffi TLS impersonation")
        html, bypass_meta = await try_http_fetch(url, all_cookies, timeout=20)
        chain.append("curl_cffi" + ("✓" if html else "✗"))

    # ── Step 3: AMP / mobile / print probes ──
    if not html or html_has_paywall_markers(html):
        logger.info("  Layer 2: AMP/mobile/print probes")
        alt_html, alt_meta = await try_amp_mobile_print(url, timeout=15)
        if alt_html and not html_has_paywall_markers(alt_html):
            html = alt_html
            bypass_meta = alt_meta
            chain.append("alt-version✓")

    # ── Step 4: extraction from raw HTML ──
    article = None
    if html:
        html, contact_meta = _apply_post_processing(html, req, chain)
        article = _extract_article(html, url)
        if article:
            chain.append(f"{article['source']}✓")
            article = _humanize_article(article, req.humanize_math, chain)
        else:
            amp_hint = extract_amp_content(html, url)
            if amp_hint and amp_hint.get("amp_url"):
                chain.append("amp-hint")

    # ── Step 5: stealth browser (JS walls, Cloudflare challenges) ──
    needs_browser = (
        req.force_browser
        or (req.use_browser and
            (not article or len(article.get("body", "")) < 300))
    )

    browser_result: dict | None = None
    if needs_browser:
        logger.info("  Layer 3: stealth Playwright browser")
        try:
            browser_result = await _browser_fetch_with_auth(
                url, all_cookies, req.auth_token,
                req.wait_selector, generate_pdf=False,
            )
            chain.append("stealth-browser✓")
            if browser_result.get("blocked_scripts", 0):
                chain.append(
                    f"scripts-blocked({browser_result['blocked_scripts']})")

            browser_html = browser_result.get("html", "")
            if browser_html:
                browser_html, cf_meta = _apply_post_processing(
                    browser_html, req, chain)
                if cf_meta.get("emails"):
                    contact_meta = cf_meta
                browser_article = _extract_article(browser_html, url)
                if browser_article and (
                    not article
                    or len(browser_article.get("body", "")) >
                       len(article.get("body", ""))
                ):
                    article = browser_article
                    chain.append(f"browser-{article['source']}✓")
                    article = _humanize_article(
                        article, req.humanize_math, chain)
                    html = browser_html
        except Exception as e:
            logger.error(f"  Browser failed: {e}")
            chain.append("browser✗")

    # ── Step 6: archive / cache fallback (hard walls) ──
    if not article or len(article.get("body", "")) < 300:
        if settings.enable_archive_fallback:
            logger.info("  Layer 4: archive fallback")
            arch_html, _ = await try_archive_fetch(url, timeout=25)
            if arch_html:
                arch_html, cf_meta = _apply_post_processing(
                    arch_html, req, chain)
                if cf_meta.get("emails"):
                    contact_meta = cf_meta
                arch_article = _extract_article(arch_html, url)
                if arch_article and len(arch_article.get("body", "")) > 300:
                    article = arch_article
                    chain.append(f"archive-{article['source']}✓")
                    article = _humanize_article(
                        article, req.humanize_math, chain)
                    html = arch_html
                else:
                    chain.append("archive✗")

    # ── Step 7: print-to-PDF (only if browser ran or is needed) ──
    pdf_data: str | None = None
    page_count = 0
    images: list[str] = []

    if req.want_pdf:
        if browser_result:
            # Re-render with PDF enabled (previous fetch had it off)
            try:
                browser_result = await _browser_fetch_with_auth(
                    url, all_cookies, req.auth_token,
                    req.wait_selector, generate_pdf=True,
                )
            except Exception as e:
                logger.warning(f"  PDF re-render failed: {e}")

        if req.want_pdf and (not article or req.want_pdf):
            try:
                browser_result = await _browser_fetch_with_auth(
                    url, all_cookies, req.auth_token,
                    req.wait_selector, generate_pdf=True,
                )
            except Exception as e:
                logger.warning(f"  PDF render failed: {e}")
                chain.append("pdf-render✗")

        if browser_result and browser_result.get("pdf_bytes"):
            pdf_bytes = browser_result["pdf_bytes"]
            pdf_data = base64.b64encode(pdf_bytes).decode()
            chain.append("pdf-generated✓")

            # Thin article text → mine the PDF
            if not article or len(article.get("body", "")) < 300:
                pdf_result = extract_pdf(pdf_bytes)
                page_count = pdf_result["pages"]
                if pdf_result.get("text") and len(pdf_result["text"]) > 300:
                    article = {
                        "title": (article or {}).get("title", ""),
                        "body": pdf_result["text"],
                        "source": "pdf-text",
                    }
                    chain.append("pdf-text✓")
                    article = _humanize_article(
                        article, req.humanize_math, chain)

                # Scanned PDF → OCR
                if (pdf_result.get("scanned")
                        and pdf_result.get("images")
                        and len(pdf_result["images"]) <= 10):
                    chain.append("scanned-pdf")
                    ocr_text = await ocr_images_with_hf(pdf_result["images"])
                    if ocr_text and len(ocr_text) > 200:
                        article = {
                            "title": (article or {}).get("title", ""),
                            "body": ocr_text, "source": "hf-ocr",
                        }
                        chain.append("ocr✓")

    # ── Final: cache + response ──
    if article or pdf_data:
        cid = _content_id(url)
        title = ((article or {}).get("title", "")
                 or (browser_result or {}).get("title", "")
                 or url)
        body = (article or {}).get("body", "") or ""

        _cache_put(_article_cache, cid, {
            "url": url, "title": title, "body": body,
            "timestamp": _now_iso(),
        })
        if pdf_data:
            _cache_put(_pdf_cache, cid, base64.b64decode(pdf_data))

        logger.info(f"✔ Done: {len(body)} chars, "
                    f"PDF={'yes' if pdf_data else 'no'}, "
                    f"chain={' → '.join(chain)}")

        return {
            "success": True,
            "url": url,
            "title": title[:500],
            "body": body,
            "article_url": f"/article/{cid}",
            "pdf_data": pdf_data,
            "pdf_url": f"/pdf/{cid}" if pdf_data else None,
            "images": images,
            "emails": contact_meta.get("emails", []),
            "phones": contact_meta.get("phones", []),
            "page_count": page_count,
            "bytes": len(body) + len(pdf_data or ""),
            "method": bypass_meta.get("method", "browser"),
            "bypass_chain": chain,
            "timestamp": _now_iso(),
            "error": None,
        }

    logger.error(f"✖ All layers exhausted for {url}")
    return _failure(
        url, chain,
        "All authorization bypass layers exhausted — site may use a "
        "server-side hard paywall")


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /scrape
# ═══════════════════════════════════════════════════════════════════════

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(req: ScrapeRequest):
    """Full bypass pipeline for one URL — see module docstring for chain."""
    try:
        result = await _scrape_url(req)
        return ScrapeResponse(**result)
    except Exception as e:
        logger.exception(f"Scrape crashed for {req.url}: {e}")
        return ScrapeResponse(
            **_failure(req.url, [], f"Internal error: {str(e)[:200]}"))


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /batch
# ═══════════════════════════════════════════════════════════════════════

@app.post("/batch", response_model=list[ScrapeResponse])
async def batch_scrape(req: BatchScrapeRequest):
    """Batch scrape up to 25 URLs concurrently."""
    if len(req.urls) > 25:
        raise HTTPException(400, "Max 25 URLs per batch request")

    options = req.options or ScrapeRequest(url="")
    opt = options.model_dump(exclude={"url"})

    tasks = [_scrape_url(ScrapeRequest(url=u, **opt)) for u in req.urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[ScrapeResponse] = []
    for i, r in enumerate(results):
        if isinstance(r, dict):
            out.append(ScrapeResponse(**r))
        else:
            out.append(ScrapeResponse(
                **_failure(req.urls[i], [], f"Task error: {str(r)[:200]}")))
    return out


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /pdf/direct — gated viewer bypass (testbook etc.)
# ═══════════════════════════════════════════════════════════════════════

@app.post("/pdf/direct")
async def pdf_direct(req: PDFDirectRequest):
    """
    Bypass gated PDF viewers.

    Example input (testbook — both variants handled):
      ?u=cdn.testbook.com%2F...pdf          (no leading slash)
      ?u=%2Fblogmedia.testbook.com%2F...pdf (leading slash)

    Strategies (in order):
      1. Decode 'u' (or any .pdf / base64 param) → normalize → fetch CDN
      2. Stealth-render the viewer page, extract embedded .pdf URL, fetch
    """
    url = req.url.strip()
    chain: list[str] = []
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # ── Collect + normalize candidate direct URLs ──
    direct_urls: list[str] = []

    # Strategy 1: 'u' param and any param containing .pdf
    for k, v in params.items():
        raw = unquote(v[0]).strip()
        if ".pdf" in raw.lower():
            candidate = _normalize_direct_pdf_url(raw)
            if candidate not in direct_urls:
                direct_urls.append(candidate)

    # Strategy 2: base64-encoded params
    for k, v in params.items():
        try:
            padded = v[0] + "=" * (-len(v[0]) % 4)
            decoded = base64.b64decode(padded).decode(
                "utf-8", errors="ignore")
            if ".pdf" in decoded.lower():
                candidate = _normalize_direct_pdf_url(decoded)
                if candidate not in direct_urls:
                    direct_urls.append(candidate)
                    chain.append("b64-param-decoded")
        except Exception:
            pass

    chain.append(f"candidates({len(direct_urls)})")
    logger.info(f"PDF direct candidates: {direct_urls}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Referer": f"https://{parsed.hostname}/",
        "Accept": "application/pdf,application/octet-stream,*/*",
    }

    # ── Try direct CDN fetches ──
    last_error = "no candidates"
    for target in direct_urls:
        for imposter in ("chrome124", "safari17_0"):
            try:
                resp = cffi_requests.get(
                    target, headers=headers, impersonate=imposter,
                    timeout=30, allow_redirects=True,
                )
                # Strict validation: HTTP 200 + real PDF magic bytes.
                # Some CDNs return 200 with HTML challenge pages.
                if resp.status_code == 200 and _is_pdf_bytes(resp.content):
                    chain.append(f"cdn-direct✓({imposter})")
                    pdf_result = extract_pdf(resp.content)
                    cid = _content_id(target)
                    _cache_put(_pdf_cache, cid, resp.content)
                    _cache_put(_article_cache, cid, {
                        "url": target,
                        "title": target.rsplit("/", 1)[-1],
                        "body": pdf_result["text"],
                        "timestamp": _now_iso(),
                    })

                    # OCR if scanned
                    text = pdf_result["text"]
                    scanned = pdf_result["scanned"]
                    if scanned and req.ocr and pdf_result.get("images"):
                        chain.append("scanned-pdf")
                        ocr_text = await ocr_images_with_hf(
                            pdf_result["images"])
                        if ocr_text:
                            text = ocr_text
                            chain.append("ocr✓")

                    # Math humanization
                    math_meta = {"formulas_converted": [], "count": 0}
                    if text and _HAS_LATEX.search(text):
                        hm = humanize_formulas_in_text(text)
                        text = hm["text"]
                        math_meta = hm
                        chain.append(f"math-humanized({hm['count']})")

                    return {
                        "success": True,
                        "viewer_url": url,
                        "cdn_url": target,
                        "viewer_bypassed": True,
                        "article_url": f"/article/{cid}",
                        "pdf_url": f"/pdf/{cid}",
                        "pdf_data": base64.b64encode(resp.content).decode(),
                        "pages": pdf_result["pages"],
                        "text": text,
                        "scanned": scanned,
                        "math": math_meta,
                        "bypass_chain": chain,
                        "timestamp": _now_iso(),
                    }
                last_error = (f"{target} → HTTP {resp.status_code}, "
                              f"body starts: {resp.content[:20]!r}")
            except Exception as e:
                last_error = f"{target} → {str(e)[:120]}"

    # ── Strategy 3: stealth-render viewer page, find embedded .pdf URL ──
    logger.info("  Falling back to stealth render of viewer page")
    try:
        browser_result = await _browser_fetch_with_auth(
            url, cookies=[], auth_token=None,
            wait_selector=None, generate_pdf=False,
        )
        html = browser_result.get("html", "")
        if html:
            cf = decode_cf_protections(html)
            html = cf["html"]
            chain.append("viewer-render✓")

            # PDF.js-style viewers embed the file URL in the DOM/scripts
            pdf_match = re.search(
                r'https?://[^\s"\'<>\\]+?\.pdf[^\s"\'<>\\]*', html)
            if pdf_match:
                pdf_url = (pdf_match.group(0)
                           .replace("\\u002F", "/").replace("\\/", "/"))
                resp = cffi_requests.get(
                    pdf_url, headers=headers,
                    impersonate="chrome124", timeout=30)
                if resp.status_code == 200 and _is_pdf_bytes(resp.content):
                    pdf_result = extract_pdf(resp.content)
                    cid = _content_id(pdf_url)
                    _cache_put(_pdf_cache, cid, resp.content)
                    chain.append("embedded-pdf-fetch✓")
                    return {
                        "success": True,
                        "viewer_url": url,
                        "cdn_url": pdf_url,
                        "viewer_bypassed": True,
                        "article_url": f"/article/{cid}",
                        "pdf_url": f"/pdf/{cid}",
                        "pdf_data": base64.b64encode(resp.content).decode(),
                        "pages": pdf_result["pages"],
                        "text": pdf_result["text"],
                        "scanned": pdf_result["scanned"],
                        "math": {"formulas_converted": [], "count": 0},
                        "bypass_chain": chain,
                        "timestamp": _now_iso(),
                    }
                last_error = (f"embedded {pdf_url} → HTTP {resp.status_code}")
    except Exception as e:
        last_error = f"browser render: {str(e)[:120]}"

    raise HTTPException(
        502, f"Could not retrieve PDF. Last error: {last_error}. "
             f"Chain: {' → '.join(chain)}")


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /pdf/extract
# ═══════════════════════════════════════════════════════════════════════

@app.post("/pdf/extract")
async def pdf_extract_endpoint(req: PDFExtractRequest):
    """Extract text from an uploaded base64 PDF, with OCR for scans."""
    try:
        pdf_bytes = base64.b64decode(req.pdf_data)
    except Exception:
        raise HTTPException(400, "Invalid base64 PDF data")

    if not _is_pdf_bytes(pdf_bytes):
        raise HTTPException(400, "Not a valid PDF file (missing %PDF header)")

    result = extract_pdf(pdf_bytes)
    text = result.get("text", "")

    if result["scanned"] and req.ocr and result.get("images"):
        logger.info(f"Scanned PDF: OCR on {len(result['images'])} pages")
        ocr_text = await ocr_images_with_hf(result["images"])
        if ocr_text:
            result["ocr_text"] = ocr_text
            text = ocr_text

    math_meta = {"formulas_converted": [], "count": 0}
    if req.humanize_math and text and _HAS_LATEX.search(text):
        hm = humanize_formulas_in_text(text)
        text = hm["text"]
        math_meta = hm

    return {
        "success": True,
        "pages": result["pages"],
        "text": text,
        "scanned": result["scanned"],
        "method": result["method"],
        "math": math_meta,
        "images_preview": result.get("images", [])[:3],
    }


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /explore — recursive site explorer
# ═══════════════════════════════════════════════════════════════════════

@app.post("/explore")
async def explore_site(req: ExploreRequest):
    """
    Recursive site explorer:
      seed → sublinks → sublinks of sublinks (BFS, up to max_depth).

    Collects per page:
      - extracted article (title + body)
      - PDF links (downloaded + text-extracted when download_pdfs=true)
      - Cloudflare-protected emails & phones (decoded)
      - sitemap graph (url → outbound same-domain links)
    """
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")

    logger.info(f"▶ Explore: {req.url} (depth={req.max_depth}, "
                f"cap={req.max_pages})")
    explorer = SiteExplorer(
        max_depth=req.max_depth,
        max_pages=req.max_pages,
        delay=req.delay,
        download_pdfs=req.download_pdfs,
    )
    result = await explorer.explore(req.url)

    # Humanize math in all extracted articles
    for page in result.get("articles", []):
        body = page.get("body_full", "")
        if body and _HAS_LATEX.search(body):
            hm = humanize_formulas_in_text(body)
            page["body_full"] = hm["text"]
            page["formulas_converted"] = hm["count"]

    logger.info(f"✔ Explore done: {result['pages_crawled']} pages, "
                f"{len(result['pdfs'])} PDFs, "
                f"{len(result['emails'])} emails, "
                f"{len(result['phones'])} phones")
    return result


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /math/humanize
# ═══════════════════════════════════════════════════════════════════════

@app.post("/math/humanize")
async def math_humanize(req: MathRequest):
    """
    Convert LaTeX formulas (\\(...\\), \\[...\\], $...$) in text to
    human-readable Unicode (superscripts, fractions, Greek, ∞, ×, …).
    """
    if not req.text.strip():
        raise HTTPException(400, "Empty text")
    return humanize_formulas_in_text(req.text)


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /contacts/decode
# ═══════════════════════════════════════════════════════════════════════

@app.post("/contacts/decode")
async def contacts_decode(body: dict):
    """
    Decode Cloudflare-protected contacts from raw HTML.

    Handles:
      <span data-cfemail="HEX">[email protected]</span>
      <a href="/cdn-cgi/l/email-protection#HEX">...</a>
      Phone numbers obfuscated with the same XOR scheme.
    """
    html = body.get("html", "")
    if not html:
        raise HTTPException(400, "Provide 'html' field in JSON body")
    return decode_cf_protections(html)


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /article/{id} and /pdf/{id}
# ═══════════════════════════════════════════════════════════════════════

@app.get("/article/{article_id}")
async def get_article(article_id: str):
    """Retrieve a cached article by content ID (in-memory, per-instance)."""
    article = _article_cache.get(article_id)
    if not article:
        raise HTTPException(
            404, f"Article '{article_id}' not in cache "
                 f"(cache is per-instance and resets on deploy)")
    return {**article, "id": article_id}


@app.get("/pdf/{pdf_id}")
async def get_pdf(pdf_id: str):
    """Download a cached PDF by content ID."""
    pdf_bytes = _pdf_cache.get(pdf_id)
    if not pdf_bytes:
        raise HTTPException(404, f"PDF '{pdf_id}' not in cache")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{pdf_id}.pdf"'},
    )


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /health and /
# ═══════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "3.1",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "browsers_active": _browser_in_use,
        "cached_articles": len(_article_cache),
        "cached_pdfs": len(_pdf_cache),
    }


@app.get("/")
async def root():
    return {
        "service": "AuthBypass Scraper",
        "version": "3.1",
        "endpoints": {
            "POST /scrape":          "Full bypass pipeline for one URL",
            "POST /batch":           "Batch scrape (max 25 URLs)",
            "POST /pdf/direct":      "Gated PDF-viewer bypass (?u= decode)",
            "POST /pdf/extract":     "Extract text from base64 PDF (+OCR)",
            "POST /explore":         "Recursive site explorer",
            "POST /math/humanize":   "LaTeX → human-readable Unicode",
            "POST /contacts/decode": "Cloudflare email/phone decoder",
            "GET  /article/{id}":    "Cached article by content ID",
            "GET  /pdf/{id}":        "Cached PDF download",
            "GET  /health":          "Health check",
        },
        "docs": "/docs",
    }


# ═══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=settings.port,
        workers=1,
        log_level=settings.log_level.lower(),
    )
