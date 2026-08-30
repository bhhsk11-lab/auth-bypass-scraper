import asyncio
import ipaddress
import json
import logging
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import httpx
import trafilatura
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

# ============================================================
# Google News resolver
# IMPORTANT: gnews_resolver.py is inside scraper/
# ============================================================
from scraper.gnews_resolver import resolve_gnews_url


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("app")


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(
    title="NEWS BYTE Source Extractor",
    description="Non-AI source article + site-structure extraction service.",
    version="1.5.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================
# Configuration
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36 NEWS-BYTE/1.0"
)

MAX_DOWNLOAD_BYTES = 8_000_000
MIN_GOOD_WORDS = 120
MIN_GOOD_SCORE = 0.30

# Bounded upstream timeouts. Keep these below the platform request
# timeout so /extract can return a useful response instead of being
# killed by an outer 30-second request timeout.
GOOGLE_RESOLVE_TIMEOUT = 10.0
HTTP_TOTAL_TIMEOUT = 12.0
HTTP_CONNECT_TIMEOUT = 5.0
RENDER_NAV_TIMEOUT_MS = 12000
RENDER_MAX_WAIT_MS = 2500
AUTO_RENDER_GOOGLE = True


# ============================================================
# Google News helpers
# ============================================================

def is_google_news_article_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""

        return host == "news.google.com" and (
            path.startswith("/rss/articles/")
            or path.startswith("/articles/")
            or path.startswith("/read/")
        )
    except Exception:
        return False


async def resolve_google_news_url(url: str):
    """
    Resolve a Google News article URL without blocking the FastAPI
    event loop. The resolver itself is synchronous, so it runs in a
    worker thread and is bounded by GOOGLE_RESOLVE_TIMEOUT.

    Returns:
        (resolved_url, method)
    """

    if not is_google_news_article_url(url):
        return url, None

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(resolve_gnews_url, url),
            timeout=GOOGLE_RESOLVE_TIMEOUT,
        )

        if hasattr(result, "success"):
            resolved = result.resolved_url
            method = result.method
            error = result.error
        elif isinstance(result, dict):
            resolved = result.get("resolved_url") or result.get("url")
            method = result.get("method")
            error = result.get("error")
        else:
            resolved = None
            method = None
            error = "invalid-resolver-result"

        logger.info(
            "Google News resolve: %s -> %s (%s)%s",
            url[:120],
            resolved[:160] if resolved else None,
            method or "failed",
            f" error={error}" if error else "",
        )

        # Never treat another Google News URL as a successful resolution.
        if resolved and resolved != url and not is_google_news_article_url(resolved):
            return resolved, method or "resolver"

        return url, method or (f"resolver-failed:{error}" if error else "not-resolved")

    except asyncio.TimeoutError:
        logger.warning(
            "Google News resolver timeout after %.1fs: %s",
            GOOGLE_RESOLVE_TIMEOUT,
            url[:160],
        )
        return url, "resolver-timeout"

    except Exception as exc:
        logger.exception("Google News resolver failed")
        return url, f"resolver-error:{type(exc).__name__}"


# ============================================================
# Pydantic models
# ============================================================

class ExtractRequest(BaseModel):
    url: HttpUrl
    render: bool = False
    max_chars: int = 60000


class ExploreRequest(BaseModel):
    url: HttpUrl
    max_pages: int = 24
    max_depth: int = 1
    concurrency: int = 8


# ============================================================
# SSRF protection
# ============================================================

def is_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        host = parsed.hostname.lower().rstrip(".")

        if host in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            return False

        if host.endswith(".local"):
            return False

        addresses = socket.getaddrinfo(host, None)

        for info in addresses:
            ip = ipaddress.ip_address(info[4][0])

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
# Text cleaning
# ============================================================

def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


_BOILERPLATE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(also|must)\s+(read|watch|see)\b",
        r"\bclick here\b",
        r"^read more\b",
        r"\bfollow (us|npr|ndtv)?\s*on\s+(twitter|facebook|instagram|whatsapp|telegram|x)\b",
        r"\bdownload (the|our)\s+app\b",
        r"\bsubscribe to\b.*(newsletter|channel|premium)",
        r"\bsign up for\b.*newsletter",
        r"\bwhatsapp channel\b",
        r"^advertisement$",
        r"^sponsored\b",
        r"\ball rights reserved\b",
        r"^copyright\s*(©|\(c\))",
        r"\bterms (of|and)\s*(use|service|conditions)\b",
        r"\bprivacy policy\b",
        r"\bcookie(s)?\s+policy\b",
        r"\bwe use cookies\b",
        r"^disclaimer\s*:",
        r"\bviews (expressed|are personal)\b",
        r"^catch all the\b",
        r"^stay updated with\b",
        r"this (story|article)\s+(has not been edited|is auto-generated)",
        r"^share (this|via|on)\b",
        r"^(photo gallery|view all images|in pictures)\b",
        r"^trending (news|now|stories)\b",
        r"^(watch|must watch)\s*[:\-]",
        r"^loading\.{2,3}$",
        r"\benable javascript\b",
        r"^\(?(reuters|ap|pti|ani|afp)\)?\s*[-—]\s*$",
        r"^\d+\s+(shares?|comments?|min read)$",
        r"^tags?\s*:",
        r"^published\s*:",
        r"^updated\s*:",
        r"^image\s*(credit|source)\s*:",
    )
]


def is_boilerplate(paragraph: str) -> bool:
    text = paragraph.strip()

    if not text:
        return True

    if len(text) <= 60 and text.isupper():
        return True

    return any(pattern.search(text) for pattern in _BOILERPLATE_PATTERNS)


def clean_title(title: str, url: str) -> str:
    if not title:
        return title

    try:
        domain = (urlparse(url).hostname or "").lower()
        domain_core = re.sub(r"^www\.", "", domain).split(".")[0]
    except Exception:
        domain_core = ""

    for sep in (" | ", " — ", " – ", " - "):
        if sep not in title:
            continue

        head, _, tail = title.rpartition(sep)

        head = head.strip()
        tail = tail.strip()

        if not head or not tail:
            continue

        tail_key = re.sub(r"[^a-z0-9]", "", tail.lower())

        looks_like_site_name = len(tail.split()) <= 5 and (
            (
                domain_core
                and len(domain_core) >= 3
                and domain_core in tail_key
            )
            or len(tail_key) <= 24
        )

        if looks_like_site_name:
            return head

    return title


# ============================================================
# JSON-LD
# ============================================================

def parse_jsonld(html: str) -> dict:
    found = {}

    soup = BeautifulSoup(html, "lxml")

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"},
    ):
        raw = script.string or script.get_text()

        if not raw:
            continue

        try:
            obj = json.loads(raw)
        except Exception:
            continue

        candidates = []

        if isinstance(obj, list):
            candidates.extend(obj)

        elif isinstance(obj, dict):
            candidates.append(obj)

            graph = obj.get("@graph")

            if isinstance(graph, list):
                candidates.extend(graph)

        for item in candidates:
            if not isinstance(item, dict):
                continue

            typ = item.get("@type", "")
            types = typ if isinstance(typ, list) else [typ]

            is_article = (
                any(
                    str(t).lower()
                    in {
                        "newsarticle",
                        "article",
                        "report",
                        "blogposting",
                    }
                    for t in types
                )
                or isinstance(item.get("articleBody"), str)
            )

            if not is_article:
                continue

            for key in (
                "headline",
                "articleBody",
                "datePublished",
                "dateModified",
                "description",
                "image",
                "author",
                "publisher",
            ):
                if key in item:
                    found[key] = item[key]

            return found

    return found


# ============================================================
# Metadata
# ============================================================

def extract_metadata(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    jsonld = parse_jsonld(html)

    def meta(*pairs):
        for attr, value in pairs:
            tag = soup.find("meta", attrs={attr: value})

            if tag and tag.get("content"):
                return clean(tag["content"])

        return ""

    title = (
        clean(jsonld.get("headline", ""))
        or meta(
            ("property", "og:title"),
            ("name", "twitter:title"),
        )
    )

    if not title:
        h1 = soup.find("h1")

        if h1:
            title = clean(h1.get_text(" ", strip=True))

    if not title and soup.title:
        title = clean(soup.title.get_text(" ", strip=True))

    description = (
        clean(jsonld.get("description", ""))
        or meta(
            ("property", "og:description"),
            ("name", "description"),
            ("name", "twitter:description"),
        )
    )

    def image_candidate(value):
        if isinstance(value, str):
            value = value.strip()

            if not value:
                return ""

            if value.startswith("//"):
                value = "https:" + value

            resolved = urljoin(url, value)

            if (
                re.match(r"^https?://", resolved, re.I)
                and "news.google.com" not in resolved.lower()
            ):
                return resolved

        elif isinstance(value, dict):
            for key in (
                "url",
                "contentUrl",
                "thumbnailUrl",
            ):
                result = image_candidate(value.get(key))

                if result:
                    return result

        elif isinstance(value, list):
            for item in value:
                result = image_candidate(item)

                if result:
                    return result

        return ""

    image = (
        image_candidate(jsonld.get("image"))
        or meta(
            ("property", "og:image"),
            ("property", "og:image:url"),
            ("property", "og:image:secure_url"),
            ("name", "og:image"),
            ("name", "twitter:image"),
            ("name", "twitter:image:src"),
        )
    )

    image = image_candidate(image)

    if not image:
        link_img = soup.find(
            "link",
            attrs={
                "rel": re.compile(
                    r"(^|\s)image_src(\s|$)",
                    re.I,
                )
            },
        )

        if link_img:
            image = image_candidate(
                link_img.get("href", "")
            )

    if not image:
        images = []

        for tag in soup.find_all("img"):
            classes = " ".join(tag.get("class", []))

            marker = " ".join(
                [
                    str(tag.get("alt", "")),
                    classes,
                    str(tag.get("id", "")),
                    str(tag.get("data-testid", "")),
                ]
            ).lower()

            if any(
                x in marker
                for x in (
                    "logo",
                    "avatar",
                    "icon",
                    "author",
                    "profile",
                    "social",
                )
            ):
                continue

            candidates = [
                tag.get("src"),
                tag.get("data-src"),
                tag.get("data-original"),
                tag.get("data-lazy-src"),
                tag.get("data-image"),
                tag.get("data-url"),
            ]

            srcset = (
                tag.get("srcset")
                or tag.get("data-srcset")
            )

            if srcset:
                candidates.append(
                    srcset.split(",")[-1]
                    .strip()
                    .split(" ")[0]
                )

            for candidate in candidates:
                result = image_candidate(candidate)

                if result:
                    images.append(result)
                    break

        if images:
            image = images[0]

    if image and "news.google.com" in image.lower():
        image = ""

    author = ""

    author_data = jsonld.get("author", "")

    if isinstance(author_data, dict):
        author = clean(
            str(author_data.get("name", ""))
        )

    elif isinstance(author_data, list):
        names = []

        for author_item in author_data:
            if isinstance(author_item, dict):
                names.append(
                    clean(
                        str(
                            author_item.get(
                                "name",
                                "",
                            )
                        )
                    )
                )

            elif isinstance(author_item, str):
                names.append(clean(author_item))

        author = ", ".join(
            x for x in names if x
        )

    elif author_data:
        author = clean(str(author_data))

    published = clean(
        str(
            jsonld.get("datePublished", "")
            or jsonld.get("dateModified", "")
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
# Article extraction
# ============================================================

def extract_article(
    html: str,
    url: str,
    method: str,
) -> dict:

    meta = extract_metadata(html, url)

    data = {}

    try:
        doc = trafilatura.bare_extraction(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            include_links=False,
            favor_precision=True,
            favor_recall=True,
            with_metadata=True,
        )

        if doc is not None:
            if hasattr(doc, "as_dict"):
                data = doc.as_dict() or {}

            elif isinstance(doc, dict):
                data = doc

            else:
                data = {
                    "text": getattr(doc, "text", "") or "",
                    "title": getattr(doc, "title", "") or "",
                    "author": getattr(doc, "author", "") or "",
                    "date": getattr(doc, "date", "") or "",
                    "image": getattr(doc, "image", "") or "",
                }

    except Exception:
        logger.exception("Trafilatura bare_extraction failed")

    if not data.get("text"):
        try:
            plain = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=True,
                include_links=False,
                favor_precision=False,
                favor_recall=True,
                output_format="txt",
            )

            if plain:
                data["text"] = plain

        except Exception:
            logger.exception("Trafilatura extract failed")

    text = clean(data.get("text", ""))

    title = (
        clean(data.get("title", ""))
        or meta["title"]
    )

    author = (
        clean(data.get("author", ""))
        or meta["author"]
    )

    published = (
        clean(data.get("date", ""))
        or meta["published"]
    )

    image = (
        clean(data.get("image", ""))
        or meta["image"]
    )

    # JSON-LD articleBody fallback
    body = meta["jsonld"].get("articleBody")

    if isinstance(body, str) and len(body) > len(text):
        text = clean(body)
        method += "+jsonld"

    # DOM fallback
    if len(text.split()) < MIN_GOOD_WORDS:
        try:
            soup = BeautifulSoup(html, "lxml")

            candidates = []

            selectors = (
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
                ".article__body",
            )

            for selector in selectors:
                for node in soup.select(selector):
                    parts = []

                    for p in node.find_all(
                        ["p", "h2", "h3"]
                    ):
                        paragraph = clean(
                            p.get_text(
                                " ",
                                strip=True,
                            )
                        )

                        if 45 <= len(paragraph) <= 3000:
                            parts.append(paragraph)

                    if parts:
                        candidates.append(
                            "\n".join(parts)
                        )

            if candidates:
                dom_text = max(
                    candidates,
                    key=len,
                )

                if len(dom_text) > len(text):
                    text = dom_text
                    method += "+dom"

        except Exception:
            logger.exception(
                "DOM article extraction failed"
            )

    # Clean paragraphs
    paragraphs = []
    seen = set()
    junk_dropped = 0

    raw_text = (
        data.get("text", "")
        or text
    )

    for raw in re.split(
        r"\n+",
        raw_text,
    ):
        paragraph = clean(raw)

        if len(paragraph) < 40:
            continue

        if is_boilerplate(paragraph):
            junk_dropped += 1
            continue

        key = re.sub(
            r"[^a-z0-9]+",
            " ",
            paragraph.lower(),
        ).strip()

        if not key or key in seen:
            continue

        seen.add(key)
        paragraphs.append(paragraph)

    if paragraphs:
        text = "\n\n".join(paragraphs)

    title = clean_title(
        title,
        url,
    )

    words = len(text.split())

    word_score = min(
        1.0,
        words / 900,
    )

    paragraph_score = min(
        1.0,
        len(paragraphs) / 10,
    )

    junk_ratio = (
        junk_dropped
        / max(
            1,
            junk_dropped + len(paragraphs),
        )
    )

    quality = max(
        0.0,
        (
            0.65 * word_score
            + 0.35 * paragraph_score
        )
        - 0.4 * junk_ratio,
    )

    description = (
        meta["description"]
        or (
            paragraphs[0][:280]
            if paragraphs
            else ""
        )
    )

    return {
        "ok": bool(text),
        "url": url,
        "title": title,
        "author": author,
        "published": published,
        "image": image,
        "description": description,
        "text": text,
        "paragraphs": paragraphs,
        "word_count": words,
        "extraction_score": round(
            quality,
            3,
        ),
        "method": method,
        "fetched_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# HTTP fetch
# ============================================================

async def fetch_html(url: str):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    timeout = httpx.Timeout(
        HTTP_TOTAL_TIMEOUT,
        connect=HTTP_CONNECT_TIMEOUT,
        read=HTTP_TOTAL_TIMEOUT,
        write=HTTP_TOTAL_TIMEOUT,
        pool=HTTP_CONNECT_TIMEOUT,
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
                response.headers
                .get("content-type", "")
                .lower()
            )

            if (
                "html" not in content_type
                and "xml" not in content_type
            ):
                raise ValueError(
                    "Source response is not HTML"
                )

            chunks = []
            total = 0

            async for chunk in response.aiter_bytes():
                total += len(chunk)

                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        "Source HTML is too large"
                    )

                chunks.append(chunk)

            html = b"".join(chunks).decode(
                response.encoding or "utf-8",
                errors="replace",
            )

            return html, str(response.url)


# ============================================================
# Playwright fetch
# ============================================================

async def fetch_rendered(url: str):
    try:
        from playwright.async_api import (
            async_playwright,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed"
        ) from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
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
                timeout=RENDER_NAV_TIMEOUT_MS,
            )

            await page.wait_for_timeout(900)

            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight * 0.70)"
            )

            await page.wait_for_timeout(900)

            return (
                await page.content(),
                page.url,
            )

        finally:
            await browser.close()


# ============================================================
# Single article extraction
# ============================================================

async def extract_one(
    url: str,
    render: bool,
    max_chars: int,
):

    if not is_public_url(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only public HTTP/HTTPS "
                "URLs are allowed."
            ),
        )

    errors = []

    requested_url = url
    was_google_news = is_google_news_article_url(requested_url)
    last_result = None

    # --------------------------------------------------------
    # Google News resolver
    # --------------------------------------------------------

    resolved_url, resolve_method = (
        await resolve_google_news_url(url)
    )

    url = resolved_url

    if (
        resolve_method
        and resolve_method
        not in (
            "cache",
            "not-resolved",
        )
    ):
        errors.append(
            "google-resolve:"
            + str(resolve_method)
        )

        # Only fail here when resolver explicitly
        # failed and URL is still Google News.
        if is_google_news_article_url(url):
            return {
                "ok": False,
                "url": requested_url,
                "requested_url": requested_url,
                "resolved_url": url,
                "google_resolve": resolve_method,
                "title": "",
                "author": "",
                "published": "",
                "image": "",
                "description": "",
                "text": "",
                "paragraphs": [],
                "word_count": 0,
                "extraction_score": 0,
                "method": "google-resolve-failed",
                "errors": errors,
                "fetched_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

    # --------------------------------------------------------
    # Fast HTTP extraction
    # --------------------------------------------------------

    try:
        html, final_url = await fetch_html(url)

        result = extract_article(
            html,
            final_url,
            "http+trafilatura",
        )

        result["requested_url"] = requested_url
        result["resolved_url"] = final_url
        result["google_resolve"] = resolve_method

        last_result = result

        if (
            result["word_count"]
            >= MIN_GOOD_WORDS
            and result["extraction_score"]
            >= MIN_GOOD_SCORE
        ):
            result["text"] = result[
                "text"
            ][:max_chars]

            return result

    except Exception as exc:
        logger.exception(
            "HTTP extraction failed"
        )

        errors.append(
            "http:"
            + type(exc).__name__
        )

    # --------------------------------------------------------
    # Playwright fallback
    # --------------------------------------------------------

    if render or (AUTO_RENDER_GOOGLE and was_google_news):
        try:
            html, final_url = (
                await fetch_rendered(url)
            )

            result = extract_article(
                html,
                final_url,
                "playwright+trafilatura",
            )

            result["requested_url"] = requested_url
            result["resolved_url"] = final_url
            result["google_resolve"] = resolve_method

            if result["ok"]:
                result["text"] = result[
                    "text"
                ][:max_chars]

                return result

        except Exception as exc:
            logger.exception(
                "Playwright extraction failed"
            )

            errors.append(
                "render:"
                + type(exc).__name__
            )

    # --------------------------------------------------------
    # Low-quality HTTP result
    # --------------------------------------------------------

    if last_result:
        last_result["ok"] = False

        last_result["method"] = (
            last_result.get(
                "method",
                "failed",
            )
            + "+low-quality"
        )

        last_result["errors"] = errors

        last_result["text"] = (
            last_result.get(
                "text",
                "",
            )[:max_chars]
        )

        return last_result

    # --------------------------------------------------------
    # Complete failure
    # --------------------------------------------------------

    return {
        "ok": False,
        "url": requested_url,
        "requested_url": requested_url,
        "resolved_url": url,
        "google_resolve": resolve_method,
        "title": "",
        "author": "",
        "published": "",
        "image": "",
        "description": "",
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
# Explore helpers
# ============================================================

_BAD_RX = re.compile(
    r"(^|[-_ ])("
    r"ad|ads|advert|advertisement|banner|cookie|consent|"
    r"subscribe|newsletter|nav|navbar|menu|footer|header|"
    r"sidebar|related|recommended|comments?|social|share|"
    r"promo|modal|popup|paywall|login|register|breadcrumb|"
    r"utility|toolbar|app-promo|download-app"
    r")([-_ ]|$)",
    re.I,
)


def _cls_id(tag) -> str:
    try:
        classes = " ".join(
            tag.get("class") or []
        )
    except Exception:
        classes = ""

    return (
        f"{tag.get('id', '')} {classes}"
    )


def _is_boilerplate_tag(tag) -> bool:
    return bool(
        _BAD_RX.search(
            _cls_id(tag)
        )
    )


def root_domain(host: str) -> str:
    parts = [
        p
        for p in (host or "").lower().split(".")
        if p
    ]

    if len(parts) > 2:
        return ".".join(parts[-2:])

    return ".".join(parts)


def is_same_site(
    href: str,
    base_host: str,
    base_root: str,
) -> bool:

    try:
        parsed = urlparse(href)

        if parsed.scheme not in (
            "http",
            "https",
        ):
            return False

        host = (
            parsed.hostname or ""
        ).lower()

        return (
            host == base_host
            or host.endswith(
                "." + base_root
            )
        )

    except Exception:
        return False


# ============================================================
# Structured page extraction
# ============================================================

def build_structured_page(
    html: str,
    url: str,
) -> dict:

    soup = BeautifulSoup(
        html,
        "lxml",
    )

    parsed = urlparse(url)

    base_host = (
        parsed.hostname or ""
    ).lower()

    base_root = root_domain(
        base_host
    )

    for tag in soup.find_all(
        [
            "script",
            "style",
            "noscript",
            "iframe",
        ]
    ):
        tag.decompose()

    meta = extract_metadata(
        html,
        url,
    )

    sections = []
    stack = {}

    def new_section(
        text: str,
        level: int,
    ):
        section = {
            "title": text,
            "level": level,
            "paragraphs": [],
            "bullets": [],
        }

        sections.append(section)
        stack[level] = section

        for lv in [
            lv for lv in stack
            if lv > level
        ]:
            del stack[lv]

        return section

    def current_section():
        for level in (
            4,
            3,
            2,
            1,
        ):
            if level in stack:
                return stack[level]

        return None

    links = []
    pdf_links = []
    book_links = []
    magazine_links = []

    tag_links = []
    category_links = []
    pagination_links = []

    seen_hrefs = set()

    media = []
    seen_media = set()

    current_media_section = "Other"

    for el in soup.find_all(
        [
            "h1",
            "h2",
            "h3",
            "h4",
            "p",
            "li",
            "blockquote",
            "a",
            "img",
        ]
    ):

        name = el.name

        # ----------------------------------------------------
        # Headings
        # ----------------------------------------------------

        if name in (
            "h1",
            "h2",
            "h3",
            "h4",
        ):
            text = clean(
                el.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            new_section(
                text,
                int(name[1]),
            )

            current_media_section = text

            continue

        # ----------------------------------------------------
        # Text
        # ----------------------------------------------------

        if name in (
            "p",
            "li",
            "blockquote",
        ):

            if _is_boilerplate_tag(el):
                continue

            text = clean(
                el.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            section = current_section()

            if not section:
                continue

            if name == "li":
                if len(text) >= 3:
                    section[
                        "bullets"
                    ].append(text)

            elif len(text) >= 25:
                section[
                    "paragraphs"
                ].append(text)

            continue

        # ----------------------------------------------------
        # Images
        # ----------------------------------------------------

        if name == "img":

            src = (
                el.get("src")
                or el.get("data-src")
                or el.get("data-lazy-src")
                or ""
            )

            if not src:
                continue

            src = urljoin(
                url,
                src,
            )

            if (
                not re.match(
                    r"^https?://",
                    src,
                    re.I,
                )
                or src in seen_media
            ):
                continue

            alt = clean(
                el.get("alt", "")
                or ""
            )

            figure = el.find_parent(
                "figure"
            )

            caption = ""

            if figure is not None:
                caption_tag = (
                    figure.find("figcaption")
                )

                if caption_tag:
                    caption = clean(
                        caption_tag.get_text(
                            " ",
                            strip=True,
                        )
                    )

            hint = (
                f"{alt} {caption} "
                f"{' '.join(el.get('class') or [])}"
            ).lower()

            kind = (
                "map"
                if re.search(
                    r"\b(map|gis|location|route|"
                    r"roadmap|political map|india map)\b",
                    hint,
                )
                else "image"
            )

            seen_media.add(src)

            media.append(
                {
                    "src": src,
                    "alt": alt,
                    "caption": caption,
                    "kind": kind,
                    "section": current_media_section,
                }
            )

            continue

        # ----------------------------------------------------
        # Links
        # ----------------------------------------------------

        if name == "a":

            href = el.get("href") or ""

            if not href:
                continue

            href = urljoin(
                url,
                href,
            )

            if not is_same_site(
                href,
                base_host,
                base_root,
            ):
                continue

            if _is_boilerplate_tag(el):
                continue

            link_title = (
                clean(
                    el.get_text(
                        " ",
                        strip=True,
                    )
                )
                or clean(
                    el.get(
                        "aria-label",
                        "",
                    )
                    or ""
                )
                or clean(
                    el.get(
                        "title",
                        "",
                    )
                    or ""
                )
            )

            if (
                not link_title
                or len(link_title) > 160
                or href in seen_hrefs
            ):
                continue

            seen_hrefs.add(href)

            section = current_section()

            item = {
                "href": href,
                "title": link_title,
                "section": (
                    section["title"]
                    if section
                    else "Other useful links"
                ),
            }

            path = urlparse(
                href
            ).path.lower()

            path_title = (
                f"{path} {link_title}"
            ).lower()

            if (
                re.search(
                    r"\.pdf(?:$|[?#])",
                    href,
                    re.I,
                )
                or re.search(
                    r"/pdf(?:/|$)",
                    path,
                )
            ):
                pdf_links.append(item)

            if re.search(
                r"\b(book|books|ebook|e-book)\b|/books?/",
                path_title,
            ):
                book_links.append(item)

            if re.search(
                r"\b(magazine|magazines|monthly|edition)\b|/magazines?/",
                path_title,
            ):
                magazine_links.append(item)

            if re.search(
                r"/tags?/",
                path,
            ):
                tag_links.append(item)

            if re.search(
                r"/category|/categories|/subjects?|/topics?|/section",
                path,
            ):
                category_links.append(item)

            if (
                re.search(
                    r"\b(next|previous|prev|older|newer)\b",
                    path_title,
                )
                or re.search(
                    r"[?&](page|paged)=\d+",
                    href,
                    re.I,
                )
                or re.search(
                    r"/page/\d+",
                    path,
                )
            ):
                pagination_links.append(item)

            articleish = bool(
                re.search(
                    r"/(daily-updates|current-affairs|news|"
                    r"editorial|article|articles|study|notes|"
                    r"courses|analysis|magazine|books?|topics?|"
                    r"subjects?|blog|upsc|ias|exam)",
                    path,
                    re.I,
                )
            ) or len(
                link_title.split()
            ) >= 4

            if (
                articleish
                and not re.search(
                    r"/(login|signup|register|contact|"
                    r"privacy|terms|careers|about|search)\b",
                    path,
                    re.I,
                )
            ):
                links.append(item)

    sections = [
        section
        for section in sections
        if (
            section["paragraphs"]
            or section["bullets"]
        )
    ]

    if not sections:
        paragraphs = []

        for p in soup.find_all("p"):

            if _is_boilerplate_tag(p):
                continue

            text = clean(
                p.get_text(
                    " ",
                    strip=True,
                )
            )

            if len(text) >= 35:
                paragraphs.append(text)

        if paragraphs:
            sections = [
                {
                    "title": (
                        meta["title"]
                        or "Page"
                    ),
                    "level": 1,
                    "paragraphs": paragraphs,
                    "bullets": [],
                }
            ]

    return {
        "ok": True,
        "url": url,
        "pageTitle": meta["title"],
        "description": meta["description"],
        "author": meta["author"],
        "date": meta["published"],
        "sections": sections[:200],
        "links": links[:240],
        "pdfLinks": pdf_links[:80],
        "bookLinks": book_links[:60],
        "magazineLinks": magazine_links[:60],
        "tagLinks": tag_links[:80],
        "categoryLinks": category_links[:100],
        "paginationLinks": pagination_links[:40],
        "media": media[:40],
        "heroImage": meta["image"],
    }


# ============================================================
# Site crawler
# ============================================================

async def crawl_site(
    start_url: str,
    max_pages: int,
    max_depth: int,
    concurrency: int,
) -> dict:

    if not is_public_url(start_url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only public HTTP/HTTPS "
                "URLs are allowed."
            ),
        )

    max_pages = max(
        1,
        min(100, max_pages),
    )

    max_depth = max(
        0,
        min(5, max_depth),
    )

    concurrency = max(
        1,
        min(12, concurrency),
    )

    semaphore = asyncio.Semaphore(
        concurrency
    )

    async def fetch_one(url: str):

        async with semaphore:
            try:
                html, final_url = (
                    await fetch_html(url)
                )

                return build_structured_page(
                    html,
                    final_url,
                )

            except Exception:
                logger.exception(
                    "Crawl failed: %s",
                    url,
                )

                return None

    seen = set()
    all_links = []

    first = None

    frontier = [start_url]
    depth = 0

    while (
        frontier
        and len(seen) < max_pages
        and depth <= max_depth
    ):

        batch = []

        for url in frontier:

            if (
                url not in seen
                and len(seen) + len(batch)
                < max_pages
            ):
                batch.append(url)

        if not batch:
            break

        seen.update(batch)

        results = await asyncio.gather(
            *[
                fetch_one(url)
                for url in batch
            ]
        )

        next_frontier = {}

        for url, data in zip(
            batch,
            results,
        ):

            if not data or not data.get("ok"):
                continue

            if first is None:
                first = data

            for link in data.get(
                "links",
                [],
            ):
                all_links.append(
                    {
                        **link,
                        "depth": depth,
                    }
                )

            if depth < max_depth:

                base_host = (
                    urlparse(url)
                    .hostname
                    or ""
                ).lower()

                root = root_domain(
                    base_host
                )

                for link in data.get(
                    "links",
                    [],
                ):

                    href = link["href"]

                    host = (
                        urlparse(href)
                        .hostname
                        or ""
                    ).lower()

                    if (
                        (
                            host == base_host
                            or host.endswith(
                                "." + root
                            )
                        )
                        and href not in seen
                    ):
                        next_frontier[
                            href
                        ] = True

                for link in (
                    data.get(
                        "paginationLinks"
                    )
                    or []
                )[:4]:

                    href = link["href"]

                    if href not in seen:
                        next_frontier[
                            href
                        ] = True

        remaining = max(
            0,
            (max_pages - len(seen)) * 2,
        )

        frontier = list(
            next_frontier.keys()
        )[:remaining]

        depth += 1

    if first is None:
        return {
            "ok": False,
            "error": (
                "No public pages could be extracted. "
                "The site may require sign-in or "
                "block automated reading."
            ),
            "url": start_url,
        }

    dedup = {}

    for link in all_links:
        dedup.setdefault(
            link["href"],
            link,
        )

    result = dict(first)

    result["links"] = list(
        dedup.values()
    )[:1000]

    result["crawlPages"] = len(seen)

    result["crawledUrls"] = list(
        seen
    )

    return result


# ============================================================
# API endpoints
# ============================================================

@app.get("/")
async def root():
    return {
        "service": "NEWS BYTE Source Extractor",
        "version": "1.5.1",
        "ai": False,
        "status": "running",
        "usage": {
            "extract": (
                "POST /extract with "
                "{url, render, max_chars}"
            ),
            "explore": (
                "POST /explore with "
                "{url, max_pages, max_depth, concurrency}"
            ),
        },
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "news-byte-source-extractor",
        "ai": False,
    }


@app.post("/extract")
async def extract_endpoint(
    request: ExtractRequest,
):
    return await extract_one(
        str(request.url),
        request.render,
        min(
            max(request.max_chars, 1000),
            100000,
        ),
    )


@app.post("/explore")
async def explore_endpoint(
    request: ExploreRequest,
):
    return await crawl_site(
        str(request.url),
        max_pages=request.max_pages,
        max_depth=request.max_depth,
        concurrency=request.concurrency,
    )


@app.get("/image")
async def proxy_image(url: str):

    if not is_public_url(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only public HTTP/HTTPS "
                "image URLs are allowed."
            ),
        )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "image/avif,image/webp,"
            "image/apng,image/svg+xml,"
            "image/*,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": url,
    }

    try:

        timeout = httpx.Timeout(
            15.0,
            connect=8.0,
        )

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            max_redirects=5,
            timeout=timeout,
        ) as client:

            response = await client.get(url)

            response.raise_for_status()

            content_type = (
                response.headers
                .get("content-type", "")
                .split(";", 1)[0]
                .lower()
            )

            if not content_type.startswith(
                "image/"
            ):
                raise HTTPException(
                    status_code=415,
                    detail=(
                        "URL did not return an image"
                    ),
                )

            if len(response.content) > 8_000_000:
                raise HTTPException(
                    status_code=413,
                    detail="Image is too large",
                )

            return Response(
                content=response.content,
                media_type=content_type,
                headers={
                    "Cache-Control": (
                        "public, max-age=86400, "
                        "stale-while-revalidate=604800"
                    )
                },
            )

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "Image fetch failed"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Image fetch failed: "
                f"{type(exc).__name__}"
            ),
        ) from exc


# ============================================================
# Local execution
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=7860,
    )