"""
Recursive Site Explorer — BFS crawler with auth bypass on every hop.
Discovers: articles, PDF links, emails, phone numbers across the whole site.
"""
import asyncio
import logging
import re
import time
from collections import deque
from urllib.parse import urlparse, urljoin, unquote

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from scraper.bypass import (
    try_http_fetch,
    build_anti_paywall_cookies,
    build_browser_like_headers,
)
from scraper.cf_decode import decode_cf_protections
from scraper.extractors import extract_from_json_ld, extract_from_next_data, extract_readability

logger = logging.getLogger(__name__)

# URL patterns for interesting resources
PDF_PATTERN = re.compile(r'\.pdf(\?.*)?$', re.I)
DOC_PATTERNS = re.compile(r'\.(doc|docx|ppt|pptx|xls|xlsx|csv|txt|epub)(\?.*)?$', re.I)

# Link patterns to skip (noise)
SKIP_PATTERNS = re.compile(
    r'(login|logout|signin|signup|register|password|cart|checkout|'
    r'facebook\.com|twitter\.com|x\.com|instagram\.com|linkedin\.com|'
    r'youtube\.com|t\.me|whatsapp|javascript:|mailto:|tel:|#|\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|woff|woff2|ttf))(\?|/|$)',
    re.I,
)


class SiteExplorer:
    """
    Explores a website recursively:
    
      seed_url
        ├── /article-1  (extract content + emails)
        │     └── /article-1/sub-topic (extract content...)
        │           └── ... (up to max_depth)
        ├── /pdfs/notes.pdf       (downloaded + extracted)
        └── /contact              (emails + phones harvested)
    """

    def __init__(self, max_depth: int = 3, max_pages: int = 200,
                 delay: float = 1.0, download_pdfs: bool = True):
        self.max_depth = max_depth          # link-hop depth (sublinks of sublinks)
        self.max_pages = max_pages          # total pages cap
        self.delay = delay                  # polite delay between requests
        self.download_pdfs = download_pdfs

        self.visited: set[str] = set()
        self.queue: deque[tuple[str, int]] = deque()  # (url, depth)

        # Results
        self.results = {
            "pages": [],          # extracted articles
            "pdfs": [],           # PDF links found (and extracted content)
            "emails": set(),
            "phones": set(),
            "broken": [],         # URLs that failed all bypass layers
            "graph": {},          # url -> [outbound links] (site map)
        }

    async def explore(self, seed_url: str) -> dict:
        """Run full recursive exploration."""
        domain = urlparse(seed_url).hostname
        self.queue.append((seed_url, 0))
        self.visited.add(self._normalize(seed_url))

        start = time.time()
        logger.info(f"Exploring {domain} (depth={self.max_depth}, cap={self.max_pages})")

        while self.queue and len(self.visited) < self.max_pages:
            url, depth = self.queue.popleft()

            # ── PDF: download & extract ──
            if PDF_PATTERN.search(url):
                if self.download_pdfs:
                    await self._process_pdf(url)
                else:
                    self.results["pdfs"].append({"url": url, "status": "link_only"})
                continue

            # ── Other docs: record link only ──
            if DOC_PATTERNS.search(url):
                self.results["pdfs"].append({"url": url, "status": "link_only"})
                continue

            # ── HTML page: bypass-fetch, extract, discover links ──
            await self._process_page(url, depth, domain)

            await asyncio.sleep(self.delay)

        elapsed = time.time() - start
        return {
            "domain": domain,
            "seed": seed_url,
            "pages_crawled": len(self.visited),
            "depth_reached": self.max_depth,
            "elapsed_seconds": round(elapsed, 1),
            "articles": self.results["pages"],
            "pdfs": self.results["pdfs"],
            "emails": sorted(self.results["emails"]),
            "phones": sorted(self.results["phones"]),
            "broken": self.results["broken"],
            "sitemap": self.results["graph"],
        }

    async def _process_page(self, url: str, depth: int, domain: str):
        """Fetch one page with bypass, extract everything, enqueue child links."""
        html = None
        try:
            # Layer 1: TLS-impersonated fetch (fast)
            html, meta = await try_http_fetch(url, timeout=15)
            # Layer 2: archive fallback if blocked
            if not html:
                from scraper.bypass import try_archive_fetch
                html, _ = await try_archive_fetch(url, timeout=15)
        except Exception as e:
            logger.warning(f"Fetch failed {url}: {e}")

        if not html:
            self.results["broken"].append(url)
            return

        # ── Cloudflare email/phone decode ──
        cf = decode_cf_protections(html)
        self.results["emails"].update(cf["emails"])
        self.results["phones"].update(cf["phones"])
        html = cf["html"]

        # ── Article extraction ──
        article = (
            extract_from_json_ld(html)
            or extract_from_next_data(html)
            or extract_readability(html, url)
        )
        if article and len(article.get("body", "")) > 200:
            self.results["pages"].append({
                "url": url,
                "title": article.get("title", ""),
                "body_preview": article["body"][:2000],
                "body_full": article["body"],
                "source": article.get("source", ""),
                "depth": depth,
            })

        # ── Discover child links (even at max depth, PDFs get enqueued) ──
        if depth < self.max_depth:
            child_links = self._extract_links(html, url, domain)
            self.results["graph"][url] = child_links
            for link in child_links:
                norm = self._normalize(link)
                if norm not in self.visited:
                    self.visited.add(norm)
                    self.queue.append((link, depth + 1))

    def _extract_links(self, html: str, base_url: str, domain: str) -> list[str]:
        """Extract all same-domain links from a page."""
        soup = BeautifulSoup(html, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            if SKIP_PATTERNS.search(href):
                continue
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            # Same-domain only (include subdomains optionally)
            if parsed.hostname and (parsed.hostname == domain or
                                     parsed.hostname.endswith("." + domain)):
                # Clean fragment
                clean = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
                if parsed.query:
                    clean += f"?{parsed.query}"
                links.append(clean)
        return list(set(links))

    async def _process_pdf(self, url: str):
        """Download and extract a PDF found during exploration."""
        try:
            headers = build_browser_like_headers(url, use_bot_ua=False)
            resp = cffi_requests.get(
                url, headers=headers, impersonate="chrome124", timeout=30,
            )
            if resp.status_code == 200 and resp.content[:4] == b"%PDF":
                from scraper.pdf_extract import extract_pdf
                result = extract_pdf(resp.content)
                self.results["pdfs"].append({
                    "url": url,
                    "status": "extracted",
                    "pages": result["pages"],
                    "text": result["text"][:5000],
                    "scanned": result["scanned"],
                })
            else:
                self.results["pdfs"].append({"url": url, "status": "download_failed"})
        except Exception as e:
            self.results["pdfs"].append({"url": url, "status": f"error: {str(e)[:100]}"})

    @staticmethod
    def _normalize(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.hostname}{p.path}".rstrip("/")
