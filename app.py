```python
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl

# IMPORTANT:
# gnews_resolver.py is inside scraper/
from scraper.gnews_resolver import resolve_google_news as gnews_resolve


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("news-byte")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="NEWS BYTE Source Extractor",
    description="Article extraction and Google News URL resolution API.",
    version="1.6.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================
# CONSTANTS
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

MAX_HTML_BYTES = 8_000_000
MAX_IMAGE_BYTES = 8_000_000
MIN_GOOD_WORDS = 120
MIN_GOOD_SCORE = 0.30


# ============================================================
# REQUEST MODELS
# ============================================================

class ExtractRequest(BaseModel):
    url: HttpUrl
    render: bool = False
    max_chars: int = Field(
        default=60000,
        ge=1000,
        le=100000,
    )


class ExploreRequest(BaseModel):
    url: HttpUrl
    max_pages: int = Field(
        default=24,
        ge=1,
        le=100,
    )
    max_depth: int = Field(
        default=1,
        ge=0,
        le=5,
    )
    concurrency: int = Field(
        default=8,
        ge=1,
        le=12,
    )


# ============================================================
# URL / SSRF PROTECTION
# ============================================================

def is_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            return False

        hostname = parsed.hostname

        if not hostname:
            return False

        hostname = hostname.lower().rstrip(".")

        if hostname in {
            "localhost",
            "localhost.localdomain",
            "127.0.0.1",
            "::1",
        }:
            return False

        if hostname.endswith(".local"):
            return False

        addresses = socket.getaddrinfo(
            hostname,
            None,
        )

        for info in addresses:
            ip = ipaddress.ip_address(
                info[4][0]
            )

            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
            ):
                return False

        return True

    except Exception:
        return False


# ============================================================
# GOOGLE NEWS
# ============================================================

def is_google_news_url(url: str) -> bool:
    try:
        parsed = urlparse(url)

        host = (
            parsed.hostname or ""
        ).lower().rstrip(".")

        path = parsed.path.rstrip("/")

        return (
            host == "news.google.com"
            and (
                path.startswith(
                    "/rss/articles/"
                )
                or path.startswith(
                    "/articles/"
                )
                or path.startswith(
                    "/read/"
                )
            )
        )

    except Exception:
        return False


async def resolve_google_news_url(
    url: str,
) -> tuple[str | None, str | None]:
    """
    Resolve Google News URLs.

    IMPORTANT:
    A failed Google News resolution returns None.
    We never pretend the original Google News URL was resolved.
    """

    if not is_google_news_url(url):
        return url, "n/a"

    try:
        resolved, method = await gnews_resolve(url)

        logger.info(
            "Google News resolve | method=%s | input=%s | output=%s",
            method,
            url,
            resolved,
        )

        if resolved:
            return resolved, method

        return None, method

    except Exception as exc:
        logger.exception(
            "Google News resolver crashed"
        )

        return None, (
            f"resolver-exception:"
            f"{type(exc).__name__}"
        )


# ============================================================
# TEXT CLEANING
# ============================================================

_BOILERPLATE_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"^(also|must)\s+(read|watch|see)\b",
        r"\bclick here\b",
        r"^read more\b",
        r"\bfollow us\b",
        r"\bdownload (the|our)\s+app\b",
        r"\bsubscribe to\b",
        r"\bsign up for\b",
        r"\bwhatsapp channel\b",
        r"^advertisement$",
        r"^sponsored\b",
        r"\ball rights reserved\b",
        r"^copyright\b",
        r"\bprivacy policy\b",
        r"\bcookie(s)?\s+policy\b",
        r"\bwe use cookies\b",
        r"^disclaimer\s*:",
        r"^share\b",
        r"^photo gallery\b",
        r"^view all images\b",
        r"^in pictures\b",
        r"^trending\b",
        r"^loading\.{2,3}$",
        r"\benable javascript\b",
        r"^tags?\s*:",
    )
]


def clean(value: str | None) -> str:
    return re.sub(
        r"\s+",
        " ",
        value or "",
    ).strip()


def is_boilerplate(text: str) -> bool:
    text = text.strip()

    if not text:
        return True

    return any(
        pattern.search(text)
        for pattern in _BOILERPLATE_PATTERNS
    )


# ============================================================
# JSON-LD
# ============================================================

def parse_jsonld(html: str) -> dict:
    soup = BeautifulSoup(
        html,
        "lxml",
    )

    for script in soup.find_all(
        "script",
        attrs={
            "type": "application/ld+json"
        },
    ):
        raw = (
            script.string
            or script.get_text()
        )

        if not raw:
            continue

        try:
            obj = __import__(
                "json"
            ).loads(raw)
        except Exception:
            continue

        candidates = []

        if isinstance(obj, dict):
            candidates.append(obj)

            graph = obj.get("@graph")

            if isinstance(graph, list):
                candidates.extend(graph)

        elif isinstance(obj, list):
            candidates.extend(obj)

        for item in candidates:
            if not isinstance(item, dict):
                continue

            article_body = item.get(
                "articleBody"
            )

            typ = item.get(
                "@type",
                "",
            )

            types = (
                typ
                if isinstance(typ, list)
                else [typ]
            )

            if not (
                isinstance(
                    article_body,
                    str,
                )
                or any(
                    str(t).lower()
                    in {
                        "article",
                        "newsarticle",
                        "report",
                        "blogposting",
                    }
                    for t in types
                )
            ):
                continue

            return item

    return {}


# ============================================================
# METADATA
# ============================================================

def extract_metadata(
    html: str,
    url: str,
) -> dict:

    soup = BeautifulSoup(
        html,
        "lxml",
    )

    jsonld = parse_jsonld(html)

    def get_meta(
        *pairs: tuple[str, str],
    ) -> str:

        for attr, value in pairs:
            tag = soup.find(
                "meta",
                attrs={
                    attr: value
                },
            )

            if tag and tag.get(
                "content"
            ):
                return clean(
                    tag.get(
                        "content"
                    )
                )

        return ""

    title = (
        clean(
            str(
                jsonld.get(
                    "headline",
                    "",
                )
            )
        )
        or get_meta(
            ("property", "og:title"),
            ("name", "twitter:title"),
        )
    )

    if not title:
        h1 = soup.find("h1")

        if h1:
            title = clean(
                h1.get_text(
                    " ",
                    strip=True,
                )
            )

    if not title and soup.title:
        title = clean(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )

    description = (
        clean(
            str(
                jsonld.get(
                    "description",
                    "",
                )
            )
        )
        or get_meta(
            ("property", "og:description"),
            ("name", "description"),
            ("name", "twitter:description"),
        )
    )

    image = ""

    jsonld_image = jsonld.get(
        "image"
    )

    if isinstance(
        jsonld_image,
        str,
    ):
        image = jsonld_image

    elif isinstance(
        jsonld_image,
        dict,
    ):
        image = (
            jsonld_image.get(
                "url"
            )
            or jsonld_image.get(
                "contentUrl"
            )
            or ""
        )

    elif isinstance(
        jsonld_image,
        list,
    ):
        for item in jsonld_image:
            if isinstance(
                item,
                str,
            ):
                image = item
                break

            if isinstance(
                item,
                dict,
            ):
                image = (
                    item.get("url")
                    or item.get(
                        "contentUrl"
                    )
                    or ""
                )

                if image:
                    break

    image = (
        image
        or get_meta(
            ("property", "og:image"),
            ("property", "og:image:url"),
            ("property", "og:image:secure_url"),
            ("name", "twitter:image"),
            ("name", "twitter:image:src"),
        )
    )

    if image:
        image = urljoin(
            url,
            image,
        )

    author = ""

    author_data = jsonld.get(
        "author"
    )

    if isinstance(
        author_data,
        dict,
    ):
        author = clean(
            str(
                author_data.get(
                    "name",
                    "",
                )
            )
        )

    elif isinstance(
        author_data,
        list,
    ):
        authors = []

        for item in author_data:
            if isinstance(
                item,
                dict,
            ):
                name = clean(
                    str(
                        item.get(
                            "name",
                            "",
                        )
                    )
                )

                if name:
                    authors.append(
                        name
                    )

            elif isinstance(
                item,
                str,
            ):
                authors.append(
                    clean(item)
                )

        author = ", ".join(
            x
            for x in authors
            if x
        )

    elif author_data:
        author = clean(
            str(author_data)
        )

    published = clean(
        str(
            jsonld.get(
                "datePublished",
                "",
            )
            or jsonld.get(
                "dateModified",
                "",
            )
        )
    )

    return {
        "title": title,
        "description": description,
        "image": image,
        "author": author,
        "published": published,
        "jsonld": jsonld,
    }


# ============================================================
# ARTICLE EXTRACTION
# ============================================================

def extract_article(
    html: str,
    url: str,
    method: str,
) -> dict:

    metadata = extract_metadata(
        html,
        url,
    )

    extracted = {}

    try:
        result = trafilatura.bare_extraction(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            include_links=False,
            favor_precision=True,
            favor_recall=True,
            with_metadata=True,
        )

        if result is not None:

            if hasattr(
                result,
                "as_dict",
            ):
                extracted = (
                    result.as_dict()
                    or {}
                )

            elif isinstance(
                result,
                dict,
            ):
                extracted = result

    except Exception:
        logger.exception(
            "trafilatura.bare_extraction failed"
        )

    text = clean(
        extracted.get(
            "text",
            "",
        )
    )

    # JSON-LD articleBody fallback.
    article_body = metadata[
        "jsonld"
    ].get(
        "articleBody"
    )

    if (
        isinstance(
            article_body,
            str,
        )
        and len(article_body)
        > len(text)
    ):
        text = clean(
            article_body
        )
        method += "+jsonld"

    # DOM fallback.
    if len(
        text.split()
    ) < MIN_GOOD_WORDS:

        soup = BeautifulSoup(
            html,
            "lxml",
        )

        candidates = []

        for selector in (
            "article",
            "main",
            "[role='main']",
            "[itemprop='articleBody']",
            ".article-body",
            ".article-content",
            ".story-body",
            ".story-content",
            ".entry-content",
            ".post-content",
        ):

            for node in soup.select(
                selector
            ):

                paragraphs = []

                for p in node.find_all(
                    [
                        "p",
                        "h2",
                        "h3",
                    ]
                ):

                    paragraph = clean(
                        p.get_text(
                            " ",
                            strip=True,
                        )
                    )

                    if (
                        len(paragraph)
                        >= 40
                        and not is_boilerplate(
                            paragraph
                        )
                    ):
                        paragraphs.append(
                            paragraph
                        )

                if paragraphs:
                    candidates.append(
                        "\n\n".join(
                            paragraphs
                        )
                    )

        if candidates:
            dom_text = max(
                candidates,
                key=len,
            )

            if len(dom_text) > len(text):
                text = dom_text
                method += "+dom"

    paragraphs = []
    seen = set()

    for raw in re.split(
        r"\n+",
        text,
    ):

        paragraph = clean(raw)

        if len(paragraph) < 40:
            continue

        if is_boilerplate(
            paragraph
        ):
            continue

        key = re.sub(
            r"[^a-z0-9]+",
            " ",
            paragraph.lower(),
        ).strip()

        if not key or key in seen:
            continue

        seen.add(key)
        paragraphs.append(
            paragraph
        )

    if paragraphs:
        text = "\n\n".join(
            paragraphs
        )

    words = len(
        text.split()
    )

    score = min(
        1.0,
        words / 900,
    )

    if paragraphs:
        score = min(
            1.0,
            score
            + min(
                0.25,
                len(paragraphs)
                / 40,
            ),
        )

    return {
        "ok": bool(text),
        "url": url,
        "title": (
            clean(
                extracted.get(
                    "title",
                    "",
                )
            )
            or metadata["title"]
        ),
        "description": (
            metadata["description"]
        ),
        "author": (
            clean(
                extracted.get(
                    "author",
                    "",
                )
            )
            or metadata["author"]
        ),
        "published": (
            clean(
                extracted.get(
                    "date",
                    "",
                )
            )
            or metadata["published"]
        ),
        "image": (
            clean(
                extracted.get(
                    "image",
                    "",
                )
            )
            or metadata["image"]
        ),
        "text": text,
        "paragraphs": paragraphs,
        "word_count": words,
        "extraction_score": round(
            score,
            3,
        ),
        "method": method,
        "fetched_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# HTTP FETCH
# ============================================================

async def fetch_html(
    url: str,
) -> tuple[str, str]:

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": (
            "en-US,en;q=0.9"
        ),
        "Cache-Control": "no-cache",
    }

    timeout = httpx.Timeout(
        25.0,
        connect=10.0,
    )

    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        max_redirects=5,
        timeout=timeout,
    ) as client:

        async with client.stream(
            "GET",
            url,
        ) as response:

            response.raise_for_status()

            content_type = (
                response.headers.get(
                    "content-type",
                    "",
                ).lower()
            )

            if (
                "html"
                not in content_type
                and "xml"
                not in content_type
            ):
                raise ValueError(
                    "Response is not HTML"
                )

            chunks = []
            total = 0

            async for chunk in response.aiter_bytes():

                total += len(chunk)

                if (
                    total
                    > MAX_HTML_BYTES
                ):
                    raise ValueError(
                        "HTML response too large"
                    )

                chunks.append(chunk)

            raw = b"".join(
                chunks
            )

            encoding = (
                response.encoding
                or "utf-8"
            )

            html = raw.decode(
                encoding,
                errors="replace",
            )

            return (
                html,
                str(response.url),
            )


# ============================================================
# PLAYWRIGHT FALLBACK
# ============================================================

async def fetch_rendered(
    url: str,
) -> tuple[str, str]:

    from playwright.async_api import (
        async_playwright,
    )

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        page = await browser.new_page(
            user_agent=USER_AGENT,
            viewport={
                "width": 1440,
                "height": 1800,
            },
        )

        try:

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            await page.wait_for_timeout(
                1500
            )

            return (
                await page.content(),
                page.url,
            )

        finally:
            await browser.close()


# ============================================================
# SINGLE ARTICLE
# ============================================================

async def extract_one(
    requested_url: str,
    render: bool,
    max_chars: int,
) -> dict:

    if not is_public_url(
        requested_url
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only public HTTP/HTTPS "
                "URLs are allowed."
            ),
        )

    errors = []

    original_url = requested_url
    resolved_url = requested_url
    google_method = "n/a"

    # --------------------------------------------------------
    # GOOGLE NEWS RESOLUTION
    # --------------------------------------------------------

    if is_google_news_url(
        requested_url
    ):

        resolved_url, google_method = (
            await resolve_google_news_url(
                requested_url
            )
        )

        # CRITICAL:
        # Never continue to the extractor with an
        # unresolved Google News URL.
        if not resolved_url:

            return {
                "ok": False,
                "url": requested_url,
                "requested_url": requested_url,
                "resolved_url": None,
                "google_resolve": google_method,
                "title": "",
                "description": "",
                "author": "",
                "published": "",
                "image": "",
                "text": "",
                "paragraphs": [],
                "word_count": 0,
                "extraction_score": 0,
                "method": "google-resolve-failed",
                "errors": [
                    "Google News URL could not be resolved"
                ],
                "fetched_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

        if is_google_news_url(
            resolved_url
        ):
            return {
                "ok": False,
                "url": requested_url,
                "requested_url": requested_url,
                "resolved_url": resolved_url,
                "google_resolve": google_method,
                "title": "",
                "description": "",
                "author": "",
                "published": "",
                "image": "",
                "text": "",
                "paragraphs": [],
                "word_count": 0,
                "extraction_score": 0,
                "method": "google-resolve-invalid",
                "errors": [
                    "Resolver returned another Google News URL"
                ],
                "fetched_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

        if not is_public_url(
            resolved_url
        ):
            return {
                "ok": False,
                "url": requested_url,
                "requested_url": requested_url,
                "resolved_url": resolved_url,
                "google_resolve": google_method,
                "title": "",
                "description": "",
                "author": "",
                "published": "",
                "image": "",
                "text": "",
                "paragraphs": [],
                "word_count": 0,
                "extraction_score": 0,
                "method": "google-resolve-invalid",
                "errors": [
                    "Resolved URL is not a public URL"
                ],
                "fetched_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

    # --------------------------------------------------------
    # NORMAL HTTP EXTRACTION
    # --------------------------------------------------------

    try:

        html, final_url = (
            await fetch_html(
                resolved_url
            )
        )

        result = extract_article(
            html,
            final_url,
            "http+trafilatura",
        )

        result.update(
            {
                "requested_url": original_url,
                "resolved_url": final_url,
                "google_resolve": google_method,
                "errors": errors,
            }
        )

        if (
            result["word_count"]
            >= MIN_GOOD_WORDS
            and result[
                "extraction_score"
            ]
            >= MIN_GOOD_SCORE
        ):

            result["text"] = (
                result["text"][:max_chars]
            )

            return result

    except Exception as exc:

        logger.exception(
            "HTTP extraction failed"
        )

        errors.append(
            f"http:{type(exc).__name__}"
        )

    # --------------------------------------------------------
    # RENDERED FALLBACK
    # --------------------------------------------------------

    if render:

        try:

            html, final_url = (
                await fetch_rendered(
                    resolved_url
                )
            )

            result = extract_article(
                html,
                final_url,
                "playwright+trafilatura",
            )

            result.update(
                {
                    "requested_url": original_url,
                    "resolved_url": final_url,
                    "google_resolve": google_method,
                    "errors": errors,
                }
            )

            result["text"] = (
                result["text"][:max_chars]
            )

            return result

        except Exception as exc:

            logger.exception(
                "Playwright extraction failed"
            )

            errors.append(
                f"render:{type(exc).__name__}"
            )

    # --------------------------------------------------------
    # FINAL FAILURE
    # --------------------------------------------------------

    return {
        "ok": False,
        "url": original_url,
        "requested_url": original_url,
        "resolved_url": resolved_url,
        "google_resolve": google_method,
        "title": "",
        "description": "",
        "author": "",
        "published": "",
        "image": "",
        "text": "",
        "paragraphs": [],
        "word_count": 0,
        "extraction_score": 0,
        "method": "failed",
        "errors": errors,
        "fetched_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# EXPLORE
# ============================================================

def root_domain(
    host: str,
) -> str:

    parts = [
        p
        for p in host.lower().split(".")
        if p
    ]

    if len(parts) >= 2:
        return ".".join(
            parts[-2:]
        )

    return host


def same_site(
    href: str,
    base_host: str,
    base_root: str,
) -> bool:

    try:

        parsed = urlparse(
            href
        )

        if parsed.scheme not in {
            "http",
            "https",
        }:
            return False

        host = (
            parsed.hostname
            or ""
        ).lower()

        return (
            host == base_host
            or host.endswith(
                "." + base_root
            )
        )

    except Exception:
        return False


async def crawl_site(
    start_url: str,
    max_pages: int,
    max_depth: int,
    concurrency: int,
) -> dict:

    if not is_public_url(
        start_url
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only public HTTP/HTTPS "
                "URLs are allowed."
            ),
        )

    semaphore = asyncio.Semaphore(
        concurrency
    )

    visited = set()
    pages = []

    async def fetch_page(
        url: str,
    ):

        async with semaphore:

            try:

                html, final_url = (
                    await fetch_html(
                        url
                    )
                )

                metadata = extract_metadata(
                    html,
                    final_url,
                )

                soup = BeautifulSoup(
                    html,
                    "lxml",
                )

                links = []

                for a in soup.find_all(
                    "a",
                    href=True,
                ):

                    href = urljoin(
                        final_url,
                        a.get(
                            "href"
                        ),
                    )

                    title = clean(
                        a.get_text(
                            " ",
                            strip=True,
                        )
                    )

                    if (
                        title
                        and same_site(
                            href,
                            (
                                urlparse(
                                    final_url
                                ).hostname
                                or ""
                            ).lower(),
                            root_domain(
                                (
                                    urlparse(
                                        final_url
                                    ).hostname
                                    or ""
                                ).lower()
                            ),
                        )
                    ):
                        links.append(
                            {
                                "href": href,
                                "title": title,
                            }
                        )

                return {
                    "ok": True,
                    "url": final_url,
                    "title": metadata[
                        "title"
                    ],
                    "description": metadata[
                        "description"
                    ],
                    "image": metadata[
                        "image"
                    ],
                    "links": links[
                        :200
                    ],
                }

            except Exception as exc:

                logger.warning(
                    "Explore failed: %s",
                    exc,
                )

                return None

    frontier = [
        start_url
    ]

    for depth in range(
        max_depth + 1
    ):

        if not frontier:
            break

        current = []

        for url in frontier:

            if url in visited:
                continue

            if len(
                visited
            ) >= max_pages:
                break

            visited.add(url)
            current.append(url)

        if not current:
            break

        results = await asyncio.gather(
            *[
                fetch_page(url)
                for url in current
            ]
        )

        next_frontier = []

        for result in results:

            if not result:
                continue

            pages.append(
                result
            )

            if depth >= max_depth:
                continue

            for link in result[
                "links"
            ]:

                href = link[
                    "href"
                ]

                if href not in visited:
                    next_frontier.append(
                        href
                    )

        frontier = list(
            dict.fromkeys(
                next_frontier
            )
        )[:max_pages]

    if not pages:

        return {
            "ok": False,
            "url": start_url,
            "pages": [],
            "error": "No pages extracted",
        }

    first = pages[0]

    return {
        "ok": True,
        "url": first["url"],
        "pageTitle": first[
            "title"
        ],
        "description": first[
            "description"
        ],
        "image": first[
            "image"
        ],
        "pages": pages,
        "pageCount": len(
            pages
        ),
        "crawledUrls": list(
            visited
        ),
    }


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
async def root():
    return {
        "service": (
            "NEWS BYTE Source Extractor"
        ),
        "version": "1.6.0",
        "status": "running",
        "google_news_resolver": True,
        "endpoints": [
            "GET /",
            "GET /health",
            "POST /extract",
            "POST /explore",
            "GET /image",
        ],
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "status": "running",
        "google_news_resolver": True,
    }


@app.post("/extract")
async def extract_endpoint(
    request: ExtractRequest,
):
    return await extract_one(
        requested_url=str(
            request.url
        ),
        render=request.render,
        max_chars=request.max_chars,
    )


@app.post("/explore")
async def explore_endpoint(
    request: ExploreRequest,
):
    return await crawl_site(
        start_url=str(
            request.url
        ),
        max_pages=request.max_pages,
        max_depth=request.max_depth,
        concurrency=request.concurrency,
    )


@app.get("/image")
async def image_proxy(
    url: str,
):

    if not is_public_url(
        url
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only public HTTP/HTTPS "
                "image URLs are allowed."
            ),
        )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/*,*/*;q=0.8",
        "Referer": url,
    }

    try:

        timeout = httpx.Timeout(
            20.0,
            connect=8.0,
        )

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            max_redirects=5,
            timeout=timeout,
        ) as client:

            response = await client.get(
                url
            )

            response.raise_for_status()

            content_type = (
                response.headers.get(
                    "content-type",
                    "",
                )
                .split(";")[0]
                .lower()
            )

            if not content_type.startswith(
                "image/"
            ):
                raise HTTPException(
                    status_code=415,
                    detail=(
                        "URL did not return "
                        "an image"
                    ),
                )

            if (
                len(response.content)
                > MAX_IMAGE_BYTES
            ):
                raise HTTPException(
                    status_code=413,
                    detail="Image too large",
                )

            return Response(
                content=response.content,
                media_type=content_type,
                headers={
                    "Cache-Control": (
                        "public, max-age=86400"
                    )
                },
            )

    except HTTPException:
        raise

    except Exception as exc:

        logger.exception(
            "Image proxy failed"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Image fetch failed: "
                f"{type(exc).__name__}"
            ),
        ) from exc


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=7860,
    )
```
