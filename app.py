"""
Auth-Bypass Scraper — Cloud Run Deployable Service.
FastAPI server with full orchestration pipeline.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from io import BytesIO
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, HttpUrl

from config import settings
from scraper.bypass import (
    run_bypass_pipeline,
    build_anti_paywall_cookies,
    html_has_paywall_markers,
    try_http_fetch,
    try_archive_fetch,
    try_amp_mobile_print,
    parse_cookie_header,
)
from scraper.browser import get_browser
from scraper.extractors import (
    extract_from_json_ld,
    extract_from_next_data,
    extract_readability,
    extract_amp_content,
)
from scraper.pdf_extract import extract_pdf, ocr_images_with_hf
from scraper.models import structure_article
from extension_bridge import format_for_extension

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AuthBypass Scraper",
    version="2.0",
    description="Multi-layer authorization bypass scraping service with PDF extraction",
    docs_url="/docs",
)

# CORS for extension communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════════════

class ScrapeRequest(BaseModel):
    url: str = Field(..., description="Target URL to scrape")
    cookies: list[dict] | None = Field(None, description="Session cookies for authenticated access")
    auth_token: str | None = Field(None, description="Bearer token for authenticated API access")
    wait_selector: str | None = Field(None, description="CSS selector to wait for before extraction")
    want_pdf: bool = Field(True, description="Generate print-to-PDF of the page")
    use_browser: bool = Field(True, description="Use stealth browser (fallback to HTTP if False)")
    force_browser: bool = Field(False, description="Force browser even if HTTP extraction succeeds")
    max_pages: int = Field(30, description="Max PDF pages to process", ge=1, le=100)
    user_agent: str | None = Field(None, description="Override User-Agent header")


class PDFExtractRequest(BaseModel):
    pdf_data: str = Field(..., description="Base64-encoded PDF data")
    filename: str | None = Field(None, description="Original filename")
    ocr: bool = Field(True, description="Run OCR on scanned PDFs")


class ScrapeResponse(BaseModel):
    success: bool
    url: str
    title: str = ""
    body: str = ""
    article_url: str | None = None
    pdf_data: str | None = None
    pdf_url: str | None = None
    images: list[str] = []
    method: str = ""
    bypass_chain: list[str] = []
    page_count: int = 0
    bytes: int = 0
    error: str | None = None


class BatchScrapeRequest(BaseModel):
    urls: list[str] = Field(..., max_length=25, description="URLs to scrape in batch")
    options: ScrapeRequest | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime: float
    browsers_active: int = 0


# ═══════════════════════════════════════════════════════════════════════
# STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════

_start_time = time.time()
_browser_lock = asyncio.Lock()
_browser_in_use = 0


@app.on_event("startup")
async def startup():
    logger.info(f"Starting AuthBypass Scraper v2.0 on port {settings.port}")
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Browser: headless={settings.browser_headless}, "
                f"concurrency={settings.browser_concurrency}")
    if settings.proxy_url:
        logger.info(f"Proxy configured: {settings.proxy_url.split('@')[-1] if '@' in settings.proxy_url else settings.proxy_url[:30]}...")
    logger.info("Service ready.")


@app.on_event("shutdown")
async def shutdown():
    global _browser_instance
    if _browser_instance:
        logger.info("Shutting down browser...")
        await _browser_instance.close()


# ═══════════════════════════════════════════════════════════════════════
# CORE SCRAPE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

async def _scrape_url(req: ScrapeRequest) -> dict:
    """Full orchestration: bypass → extract → PDF → return."""
    url = req.url.strip()
    chain = []
    logger.info(f"Scraping: {url}")

    # ── Step 1: Prepare cookies ──
    all_cookies = list(req.cookies or [])

    # Inject anti-paywall cookies (meter reset, fake subscription)
    anti_cookies = build_anti_paywall_cookies(urlparse(url).hostname or "")
    all_cookies.extend(anti_cookies)
    chain.append("anti-paywall-cookies")

    # ── Step 2: Try TLS-impersonated HTTP first (fast path) ──
    html = None
    bypass_info = {}
    if settings.enable_curl_cffi and not req.force_browser:
        logger.info("Layer 1: TLS-impersonated HTTP")
        html, meta = await try_http_fetch(url, all_cookies, timeout=20)
        chain.append("curl_cffi")
        if html:
            bypass_info = meta

    # ── Step 3: Try AMP / mobile / print versions ──
    if not html or html_has_paywall_markers(html):
        logger.info("Layer 2: AMP / mobile / print versions")
        alt_html, alt_meta = await try_amp_mobile_print(url, timeout=15)
        if alt_html and not html_has_paywall_markers(alt_html):
            html = alt_html
            bypass_info = alt_meta
            chain.append("alt-version")

    # ── Step 4: Try content extraction from HTML (no browser needed) ──
    article = None
    if html:
        logger.info("Attempting extraction from HTML...")
        article = extract_from_json_ld(html)
        if article:
            chain.append("json-ld")
            logger.info("Extracted from JSON-LD")
        if not article:
            article = extract_from_next_data(html)
            if article:
                chain.append("next-data")
                logger.info("Extracted from __NEXT_DATA__")
        if not article:
            article = extract_readability(html, url)
            if article:
                chain.append("readability")
                logger.info("Extracted via readability")

    # ── Step 4b: AMP hint extraction ──
    if not article and html:
        amp_hint = extract_amp_content(html, url)
        if amp_hint and amp_hint.get("amp_url"):
            logger.info(f"AMP version available: {amp_hint['amp_url']}")
            chain.append("amp-hint-detected")

    # ── Step 5: Stealth browser for JS / Cloudflare walls ──
    browser_result = None
    if req.use_browser and (not article or req.force_browser):
        logger.info("Layer 3: Stealth Playwright browser")
        try:
            async with _browser_lock:
                global _browser_in_use
                _browser_in_use += 1

            browser = await get_browser()
            browser_result = await browser.fetch(
                url,
                cookies=all_cookies,
                wait_selector=req.wait_selector,
                generate_pdf=True,
            )
            chain.append("stealth-browser")

            # Try extraction from browser-rendered HTML
            if browser_result and browser_result["html"]:
                browser_html = browser_result["html"]
                browser_article = (
                    extract_from_json_ld(browser_html)
                    or extract_from_next_data(browser_html)
                    or extract_readability(browser_html, url)
                )
                if browser_article and (not article or len(browser_article.get("body", "")) > len(article.get("body", ""))):
                    article = browser_article
                    chain.append("browser-extracted")
                    logger.info("Extracted from browser-rendered HTML")

        except Exception as e:
            logger.error(f"Browser fetch failed: {e}")
            chain.append("browser-failed")
        finally:
            async with _browser_lock:
                _browser_in_use -= 1

    # ── Step 6: Archive fallback ──
    if not article or (article and len(article.get("body", "")) < 300):
        logger.info("Layer 4: Archive / cache fallback")
        arch_html, arch_meta = await try_archive_fetch(url, timeout=25)
        if arch_html:
            arch_article = (
                extract_from_json_ld(arch_html)
                or extract_readability(arch_html, url)
            )
            if arch_article and len(arch_article.get("body", "")) > 300:
                article = arch_article
                chain.append("archive-fallback")
                logger.info("Extracted from archive")

    # ── Step 7: Generate PDF from browser or build from HTML ──
    pdf_data = None
    if req.want_pdf:
        if browser_result and browser_result.get("pdf_bytes"):
            pdf_data = base64.b64encode(browser_result["pdf_bytes"]).decode()
            chain.append("pdf-generated")
            logger.info("PDF generated from browser")
        elif html:
            # Could generate PDF from HTML using weasyprint or pdfkit, but requires extra deps
            logger.info("No browser PDF available; HTML-only mode")

    # ── No content at all → error ──
    if not article and not pdf_data:
        logger.error(f"All bypass layers exhausted for {url}")
        return {
            "success": False,
            "url": url,
            "title": "",
            "body": "",
            "method": "failed",
            "bypass_chain": chain,
            "error": "All authorization bypass layers exhausted — site may use server-side hard paywall",
            "pdf_data": None,
            "images": [],
            "page_count": 0,
            "bytes": 0,
            "article_url": None,
            "pdf_url": None,
        }

    # ── Step 8: If PDF was generated but no text, try PDF extraction ──
    page_count = 0
    images = []
    if pdf_data:
        pdf_bytes = base64.b64decode(pdf_data)
        pdf_result = extract_pdf(pdf_bytes)
        page_count = pdf_result["pages"]
        images = pdf_result.get("images", [])

        # If PDF has text but we didn't get article body, use PDF text
        if not article or len(article.get("body", "")) < 300:
            pdf_text = pdf_result.get("text", "")
            if pdf_text and len(pdf_text) > 300:
                article = {"title": url, "body": pdf_text, "source": "pdf-extract"}
                chain.append("pdf-text-extracted")

        # If PDF is scanned, run OCR
        if pdf_result.get("scanned") and pdf_result["images"]:
            if len(pdf_result["images"]) <= 10:
                chain.append("scanned-pdf-detected")
                ocr_text = await ocr_images_with_hf(pdf_result["images"])
                if ocr_text:
                    article = {"title": url, "body": ocr_text, "source": "ocr-hf"}
                    chain.append("ocr-applied")

    # ── Structure data for extension ──
    title = (article or {}).get("title", "") or browser_result.get("title", "") or url
    body = (article or {}).get("body", "") or ""

    logger.info(f"Scrape complete: {len(body)} chars body, "
                f"{'PDF' if pdf_data else 'no PDF'}, chain: {' -> '.join(chain)}")

    # Generate article content URL (served from Cloud Run storage / memory)
    article_content_id = hashlib.md5(url.encode()).hexdigest()[:12]

    return {
        "success": True,
        "url": url,
        "title": title[:500],
        "body": body,
        "pdf_data": pdf_data,
        "images": images,
        "page_count": page_count,
        "method": bypass_info.get("method", "browser"),
        "bypass_chain": chain,
        "bytes": len(body) + len(pdf_data or ""),
        "error": None,
        "article_url": f"/article/{article_content_id}",
        "pdf_url": f"/pdf/{article_content_id}" if pdf_data else None,
    }


# ═══════════════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(req: ScrapeRequest, background_tasks: BackgroundTasks):
    """
    Main scrape endpoint.
    
    Full bypass pipeline:
    1. Anti-paywall cookie injection
    2. TLS-impersonated HTTP (curl_cffi)
    3. AMP/mobile/print version probing
    4. Content extraction (JSON-LD, __NEXT_DATA__, readability)
    5. Stealth Playwright browser (JS walls, Cloudflare)
    6. Archive / Google cache fallback
    7. Print-to-PDF generation
    8. PDF text extraction + OCR (if scanned)
    
    Returns structured data ready for extension consumption.
    """
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")

    try:
        result = await _scrape_url(req)
        return ScrapeResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Scrape failed for {req.url}: {e}")
        return ScrapeResponse(
            success=False,
            url=req.url,
            error=f"Internal error: {str(e)[:200]}",
            bypass_chain=[],
        )


@app.post("/batch", response_model=list[ScrapeResponse])
async def batch_scrape(req: BatchScrapeRequest):
    """Scrape multiple URLs. Max 25 per request."""
    if len(req.urls) > 25:
        raise HTTPException(400, "Max 25 URLs per batch request")
    options = req.options or ScrapeRequest(url="")
    tasks = [_scrape_url(ScrapeRequest(url=u, **options.model_dump(exclude={"url"})))
             for u in req.urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [
        ScrapeResponse(**r) if isinstance(r, dict)
        else ScrapeResponse(success=False, url=req.urls[i], error=str(r), bypass_chain=[])
        for i, r in enumerate(results)
    ]


@app.post("/pdf/extract")
async def extract_pdf_endpoint(req: PDFExtractRequest):
    """Extract text from a submitted PDF (base64)."""
    try:
        pdf_bytes = base64.b64decode(req.pdf_data)
        result = extract_pdf(pdf_bytes)

        # If scanned and OCR requested
        if result["scanned"] and req.ocr and result.get("images"):
            ocr_text = await ocr_images_with_hf(result["images"])
            if ocr_text:
                result["ocr_text"] = ocr_text

        return {
            "success": True,
            "pages": result["pages"],
            "text": result["text"],
            "scanned": result["scanned"],
            "ocr_text": result.get("ocr_text", ""),
            "method": result["method"],
            "images": result.get("images", [])[:5],  # Limit images returned
        }
    except Exception as e:
        raise HTTPException(400, f"PDF extraction failed: {e}")


@app.get("/article/{article_id}")
async def get_article(article_id: str):
    """Retrieve a previously scraped article by ID."""
    return Response(
        content=json.dumps({"error": "Article cache not implemented yet"}),
        media_type="application/json",
        status_code=501,
    )


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check for Cloud Run."""
    return HealthResponse(
        status="healthy",
        version="2.0",
        uptime=time.time() - _start_time,
        browsers_active=_browser_in_use,
    )


@app.get("/")
async def root():
    return {
        "service": "AuthBypass Scraper",
        "version": "2.0",
        "endpoints": {
            "POST /scrape": "Scrape a URL with full bypass pipeline",
            "POST /batch": "Batch scrape (max 25 URLs)",
            "POST /pdf/extract": "Extract text from uploaded PDF",
            "GET /article/{id}": "Get cached article content",
            "GET /health": "Health check",
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
        loop="uvloop",
        log_level=settings.log_level.lower(),
    )
