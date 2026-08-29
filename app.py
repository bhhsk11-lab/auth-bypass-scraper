"""
═══════════════════════════════════════════════════════════════════════════
 Auth-Bypass Scraper v3.0 — Cloud Run Deployable Service
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
   GET  /health           Cloud Run health check
   GET  /                 Service info

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
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from config import settings
from extension_bridge import format_for_extension
from scraper.bypass import (
    build_anti_paywall_cookies,
    build_browser_like_headers,
    html_has_paywall_markers,
    run_bypass_pipeline,
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
    extract_og_tags,
    extract_readability,
)
from scraper.math_pretty import humanize_formulas_in_text
from scraper.models import structure_article
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
    version="3.0",
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

# In-memory article cache (use Redis in multi-instance deployments)
_article_cache: dict[str, dict] = {}
_pdf_cache: dict[str, bytes] = {}
_CACHE_LIMIT = 500


def _cache_put(cache: dict, key: str, value: Any):
    """Simple LRU-ish cache with size cap."""
    if len(cache) >= _CACHE_LIMIT:
        # Evict oldest ~10%
        for old_key in list(cache.keys())[: _CACHE_LIMIT // 10]:
            cache.pop(old_key, None)
    cache[key] = value


# ═══════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════════════

class ScrapeRequest(BaseModel):
    url: str = Field(..., description="Target URL to scrape")
    cookies: list[dict] | None = Field(
        None, description="Session cookies for authenticated access "
        "[{name, value, domain?, path?}]")
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
    url: str = Field(..., description="Gated viewer URL (e.g. testbook "
                     "/pdf-viewer?u=cdn...) — real file URL is decoded "
                     "and fetched directly")


class ExploreRequest(BaseModel):
    url: str = Field(..., description="Seed URL to begin exploration")
    max_depth: int = Field(3, ge=1, le=6,
                           description="Sublink recursion depth")
    max_pages: int = Field(200, ge=1, le=2000, description="Page cap")
    delay: float = Field(1.0, ge=0.2, le=10, description="Delay (s) between requests")
    download_pdfs: bool = Field(True, description="Download + extract found PDFs")


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
    logger.info("AuthBypass Scraper v3.0 starting")
    logger.info(f"  Environment : {settings.environment}")
    logger.info(f"  Browser     : headless={settings.browser_headless}")
    logger.info(f"  Proxy       : {'configured' if settings.proxy_url else 'direct'}")
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
# CORE HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


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
    """Cloudflare contact decode + returns metadata for response."""
    meta: dict = {"emails": [], "phones": []}
    if req.decode_contacts:
        cf = decode_cf_protections(html)
        if cf["count"]:
            html = cf["html"]
            meta["emails"] = cf["emails"]
            meta["phones"] = cf["phones"]
            chain.append("cf-contacts-decoded")
    return html, meta


def _humanize_article(article: dict | None, req: ScrapeRequest,
                      chain: list[str]) -> dict | None:
    """Apply LaTeX → readable conversion to article body if enabled."""
    if article and req.humanize_math and article.get("body"):
        if "\\(" in article["body"] or "\\frac" in article["body"] or "\\times" in article["body"]:
            result = humanize_formulas_in_text(article["body"])
            if result["count"]:
                article["body"] = result["text"]
                article["formulas_converted"] = result["formulas_converted"]
                chain.append(f"math-humanized({result['count']})")
    return article


# ═══════════════════════════════════════════════════════════════════════
# MAIN SCRAPE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

async def _scrape_url(req: ScrapeRequest) -> dict:
    """
    Full bypass pipeline:
      cookies → TLS-impersonated HTTP → alt versions → HTML extraction
      → stealth browser → archive fallback → PDF → post-processing.
    """
    url = req.url.strip()
    chain: list[str] = []
    logger.info(f"▶ Scrape: {url}")

    # ── Validate ──
    if not url.startswith(("http://", "https://")):
        return _failure(url, ["invalid-url"], "URL must start with http:// or https://")

    # ── Step 1: cookies (user-supplied + anti-paywall injection) ──
    all_cookies = list(req.cookies or [])
    all_cookies.extend(build_anti_paywall_cookies(urlparse(url).hostname or ""))
    chain.append("anti-paywall-cookies")

    # Bearer token → will be used via extra headers on browser fetch
    if req.auth_token:
        chain.append("bearer-token")

    # ── Step 2: TLS-impersonated HTTP (fast path) ──
    html: str | None = None
    bypass_meta: dict = {}
    if settings.enable_curl_cffi and not req.force_browser:
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
            article = _humanize_article(article, req, chain)

        # AMP hint (redirect candidate for browser stage)
        if not article:
            amp_hint = extract_amp_content(html, url)
            if amp_hint and amp_hint.get("amp_url"):
                chain.append("amp-hint")

    # ── Step 5: stealth browser (JS walls, Cloudflare challenges) ──
    browser_result: dict | None = None
    needs_browser = (
        req.force_browser
        or (req.use_browser and (not article or len(article.get("body", "")) < 300))
        or req.want_pdf  # PDF capture always needs the browser
    )

    if needs_browser:
        logger.info("  Layer 3: stealth Playwright browser")
        global _browser_in_use
        try:
            async with _browser_lock:
                _browser_in_use += 1
            browser = await get_browser()

            extra_headers = {}
            if req.auth_token:
                extra_headers["Authorization"] = f"Bearer {req.auth_token}"

            browser_result = await browser.fetch(
                url,
                cookies=all_cookies,
                wait_selector=req.wait_selector,
                generate_pdf=req.want_pdf,
            )
            chain.append("stealth-browser✓")
            if browser_result.get("blocked_scripts", 0):
                chain.append(f"scripts-blocked({browser_result['blocked_scripts']})")

            # Extraction from rendered DOM
            browser_html = browser_result.get("html", "")
            if browser_html:
                browser_html, contact_meta = _apply_post_processing(
                    browser_html, req, chain)
                browser_article = _extract_article(browser_html, url)
                if browser_article and (
                    not article
                    or len(browser_article.get("body", "")) > len(article.get("body", ""))
                ):
                    article = browser_article
                    chain.append(f"browser-{article['source']}✓")
                    article = _humanize_article(article, req, chain)
                    html = browser_html

        except Exception as e:
            logger.error(f"  Browser failed: {e}")
            chain.append("browser✗")
        finally:
            async with _browser_lock:
                _browser_in_use -= 1

    # ── Step 6: archive / cache fallback (hard walls) ──
    if not article or len(article.get("body", "")) < 300:
        if settings.enable_archive_fallback:
            logger.info("  Layer 4: archive fallback")
            arch_html, arch_meta = await try_archive_fetch(url, timeout=25)
            if arch_html:
                arch_html, contact_meta = _apply_post_processing(arch_html, req, chain)
                arch_article = _extract_article(arch_html, url)
                if arch_article and len(arch_article.get("body", "")) > 300:
                    article = arch_article
                    chain.append(f"archive-{article['source']}✓")
                    article = _humanize_article(article, req, chain)
                    html = arch_html
                else:
                    chain.append("archive✗")

    # ── Step 7: PDF generation + extraction ──
    pdf_data: str | None = None
    page_count = 0
    images: list[str] = []

    if req.want_pdf:
        if browser_result and browser_result.get("pdf_bytes"):
            pdf_bytes = browser_result["pdf_bytes"]
            pdf_data = base64.b64encode(pdf_bytes).decode()
            chain.append("pdf-generated✓")

            # If article text is thin, mine the PDF for it
            if not article or len(article.get("body", "")) < 300:
                pdf_result = extract_pdf(pdf_bytes)
                page_count = pdf_result["pages"]

                if pdf_result.get("text") and len(pdf_result["text"]) > 300:
                    article = {"title": article.get("title") if article else "",
                               "body": pdf_result["text"], "source": "pdf-text"}
                    chain.append("pdf-text✓")

                # Scanned PDF → OCR
                if pdf_result.get("scanned") and pdf_result.get("images"):
                    chain.append("scanned-pdf")
                    if len(pdf_result["images"]) <= 10:
                        ocr_text = await ocr_images_with_hf(pdf_result["images"])
                        if ocr_text and len(ocr_text) > 200:
                            article = {"title": article.get("title") if article else "",
                                       "body": ocr_text, "source": "hf-ocr"}
                            chain.append("ocr✓")

        elif html:
            chain.append("pdf-skip(no-browser)")

    # ── Cache + build response ──
    if article or pdf_data:
        cid = _content_id(url)
        title = (article or {}).get("title", "") \
                or (browser_result or {}).get("title", "") \
                or url
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
            "emails": contact_meta.get("emails", []) if 'contact_meta' in dir() else [],
            "phones": contact_meta.get("phones", []) if 'contact_meta' in dir() else [],
            "page_count": page_count,
            "bytes": len(body) + len(pdf_data or ""),
            "method": bypass_meta.get("method", "browser"),
            "bypass_chain": chain,
            "timestamp": _now_iso(),
            "error": None,
        }

    logger.error(f"✖ All layers exhausted for {url}")
    return _failure(url, chain,
                    "All authorization bypass layers exhausted — "
                    "site may use a server-side hard paywall")


def _failure(url: str, chain: list[str], error: str) -> dict:
    return {
        "success": False, "url": url, "title": "", "body": "",
        "article_url": None, "pdf_data": None, "pdf_url": None,
        "images": [], "emails": [], "phones": [],
        "page_count": 0, "bytes": 0, "method": "failed",
        "bypass_chain": chain, "timestamp": _now_iso(), "error": error,
    }


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /scrape
# ═══════════════════════════════════════════════════════════════════════

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(req: ScrapeRequest):
    """
    Main endpoint — full bypass pipeline for one URL.

    Pipeline: anti-paywall cookies → TLS impersonation → AMP/print probes
    → JSON-LD/__NEXT_DATA__/readability extraction → stealth browser with
    paywall-script blocking → archive fallback → print-to-PDF → OCR →
    Cloudflare contact decode → math humanization.
    """
    try:
        result = await _scrape_url(req)
        return ScrapeResponse(**result)
    except Exception as e:
        logger.exception(f"Scrape crashed for {req.url}: {e}")
        return ScrapeResponse(**_failure(req.url, [], f"Internal error: {str(e)[:200]}"))


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /batch
# ═══════════════════════════════════════════════════════════════════════

@app.post("/batch", response_model=list[ScrapeResponse])
async def batch_scrape(req: BatchScrapeRequest):
    """Batch scrape up to 25 URLs concurrently."""
    if len(req.urls) > 25:
        raise HTTPException(400, "Max 25 URLs per batch request")

    options = req.options or ScrapeRequest(url="")
    tasks = []
    for u in req.urls:
        opt = options.model_dump(exclude={"url"})
        tasks.append(_scrape_url(ScrapeRequest(url=u, **opt)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = []
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

    Example input (testbook):
      https://testbook.com/pdf-viewer?u=cdn.testbook.com%2F1768369609992-....pdf%2F1768369610.pdf

    Strategies (in order):
      1. Decode 'u' (or any .pdf / base64 param) → fetch CDN file directly
      2. Scan any query param for URL-encoded / base64 .pdf paths
      3. Stealth-render the viewer page, extract embedded .pdf URL, fetch it
    """
    url = req.url.strip()
    chain: list[str] = []
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # ── Collect candidate direct URLs ──
    direct_urls: list[str] = []

    # Strategy 1: the 'u' param
    if "u" in params:
        raw = unquote(params["u"][0]).strip()
        if raw.startswith("http"):
            direct_urls.append(raw)
        elif raw.startswith("//"):
            direct_urls.append(f"https:{raw}")
        else:
            direct_urls.append(f"https://{raw}")

    # Strategy 2: any param containing .pdf
    for k, v in params.items():
        decoded = unquote(v[0]).strip()
        if ".pdf" in decoded.lower():
            candidate = decoded if decoded.startswith("http") else f"https://{decoded}"
            if candidate not in direct_urls:
                direct_urls.append(candidate)

    # Strategy 3: base64-encoded params
    for k, v in params.items():
        try:
            padded = v[0] + "=" * (-len(v[0]) % 4)
            decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
            if ".pdf" in decoded.lower():
                candidate = decoded if decoded.startswith("http") else f"https://{decoded}"
                if candidate not in direct_urls:
                    direct_urls.append(candidate)
                    chain.append("b64-param-decoded")
        except Exception:
            pass

    chain.append(f"candidates({len(direct_urls)})")
    logger.info(f"PDF direct: {len(direct_urls)} candidate URLs from {url}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Referer": f"https://{parsed.hostname}/",   # look like the viewer page
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
                if resp.status_code == 200 and resp.content[:4] == b"%PDF":
                    chain.append(f"cdn-direct✓({imposter})")
                    pdf_result = extract_pdf(resp.content)

                    cid = _content_id(target)
                    _cache_put(_pdf_cache, cid, resp.content)
                    _cache_put(_article_cache, cid, {
                        "url": target,
                        "title": req.filename or target.rsplit("/", 1)[-1],
                        "body": pdf_result["text"],
                        "timestamp": _now_iso(),
                    })

                    # Humanize math in PDF text
                    text = pdf_result["text"]
                    math_meta = {"formulas_converted": [], "count": 0}
                    if req is not None and text and "\\frac" in text:
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
                        "scanned": pdf_result["scanned"],
                        "math": math_meta,
                        "bypass_chain": chain,
                        "timestamp": _now_iso(),
                    }
                last_error = f"{target} → HTTP {resp.status_code}"
            except Exception as e:
                last_error = f"{target} → {str(e)[:120]}"

    # ── Strategy 4: stealth-render the viewer page, find embedded PDF URL ──
    logger.info("  Falling back to stealth browser render of viewer page")
    try:
        browser = await get_browser()
        res = await browser.fetch(url, generate_pdf=False)
        html = res.get("html", "")
        if html:
            html, contact_meta = decode_cf_protections(html), None
            # Look for .pdf URLs in rendered DOM (PDF.js viewers embed them)
            pdf_match = re.search(
                r'https?://[^\s"\'<>\\]+?\.pdf[^\s"\'<>\\]*', html)
            if pdf_match:
                pdf_url = (pdf_match.group(0)
                           .replace("\\u002F", "/").replace("\\/", "/"))
                chain.append("viewer-render✓")
                resp = cffi_requests.get(
                    pdf_url, headers=headers,
                    impersonate="chrome124", timeout=30)
                if resp.status_code == 200 and resp.content[:4] == b"%PDF":
                    pdf_result = extract_pdf(resp.content)
                    cid = _content_id(pdf_url)
                    _cache_put(_pdf_cache, cid, resp.content)
                    return {
                        "success": True,
                        "viewer_url": url,
                        "cdn_url": pdf_url,
                        "viewer_bypassed": True,
                        "pdf_url": f"/pdf/{cid}",
                        "article_url": f"/article/{cid}",
                        "pdf_data": base64.b64encode(resp.content).decode(),
                        "pages": pdf_result["pages"],
                        "text": pdf_result["text"],
                        "scanned": pdf_result["scanned"],
                        "math": {"formulas_converted": [], "count": 0},
                        "bypass_chain": chain + ["embedded-pdf-fetch✓"],
                        "timestamp": _now_iso(),
                    }
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

    if pdf_bytes[:4] != b"%PDF":
        raise HTTPException(400, "Not a valid PDF file (missing %PDF header)")

    result = extract_pdf(pdf_bytes)
    text = result.get("text", "")

    # OCR scanned pages
    if result["scanned"] and req.ocr and result.get("images"):
        logger.info(f"Scanned PDF: OCR on {len(result['images'])} pages")
        ocr_text = await ocr_images_with_hf(result["images"])
        if ocr_text:
            result["ocr_text"] = ocr_text
            text = ocr_text

    # Math humanization
    math_meta = {"formulas_converted": [], "count": 0}
    if req.humanize_math and text:
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
        if page.get("body_full") and "\\frac" in page["body_full"]:
            hm = humanize_formulas_in_text(page["body_full"])
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
# ENDPOINT: /contacts/decode — Cloudflare email/phone protection
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
# ENDPOINT: /article/{id} and /pdf/{id} — cached content retrieval
# ═══════════════════════════════════════════════════════════════════════

@app.get("/article/{article_id}")
async def get_article(article_id: str):
    """Retrieve a cached article by content ID (in-memory, per-instance)."""
    article = _article_cache.get(article_id)
    if not article:
        raise HTTPException(404, f"Article '{article_id}' not in cache "
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
        headers={"Content-Disposition":
                 f'inline; filename="{pdf_id}.pdf"'},
    )


# ═══════════════════════════════════════════════════════════════════════
# ENDPOINT: /health and /
# ═══════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "3.0",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "browsers_active": _browser_in_use,
        "cached_articles": len(_article_cache),
        "cached_pdfs": len(_pdf_cache),
    }


@app.get("/")
async def root():
    return {
        "service": "AuthBypass Scraper",
        "version": "3.0",
        "endpoints": {
            "POST /scrape":          "Full bypass pipeline for one URL",
            "POST /batch":           "Batch scrape (max 25 URLs)",
            "POST /pdf/direct":      "Gated PDF-viewer bypass (?u= param decode)",
            "POST /pdf/extract":     "Extract text from base64 PDF (+OCR)",
            "POST /explore":         "Recursive site explorer (articles, PDFs, contacts)",
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
