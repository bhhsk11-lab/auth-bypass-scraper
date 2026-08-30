"""Content extraction from HTML — multiple strategies."""
import json
import re
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup


def extract_from_json_ld(html: str) -> dict | None:
    """
    Extract full article body from JSON-LD structured data.
    Many publishers embed the ENTIRE article in ld+json for SEO.
    This completely bypasses the paywall since the data was never hidden.
    """
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.text)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                atype = item.get("@type", "")
                if atype in ("NewsArticle", "Article", "BlogPosting",
                             "Report", "ScholarlyArticle", "TechArticle",
                             "WebPage", "WebSite"):
                    body = item.get("articleBody") or item.get("description")
                    if body and len(str(body)) > 200:
                        return {
                            "title": item.get("headline") or item.get("name") or "",
                            "author": item.get("author", {}) if isinstance(item.get("author"), dict)
                                      else (item.get("author", [{}])[0] if isinstance(item.get("author"), list)
                                            else item.get("author", "")),
                            "body": str(body),
                            "date_published": item.get("datePublished", ""),
                            "source": "json-ld",
                        }
        except (json.JSONDecodeError, AttributeError, KeyError):
            continue
    return None


def extract_from_next_data(html: str) -> dict | None:
    """
    Next.js sites embed __NEXT_DATA__ with full content props.
    Nuxt.js uses __NUXT__. Gatsby uses __GATSBY.
    """
    patterns = [
        (r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', "__NEXT_DATA__"),
        (r'<script>window\.__NUXT__\s*=\s*({.*?});</script>', "__NUXT__"),
        (r'<script>window\.__GATSBY.*?=({.*?});</script>', "__GATSBY"),
    ]

    for pattern, name in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
            body = _walk_for_content(data)
            if body and len(body) > 300:
                return {
                    "title": _walk_for_title(data) or "",
                    "body": body,
                    "source": f"{name}",
                }
        except (json.JSONDecodeError, Exception):
            continue
    return None


def _walk_for_content(obj, depth=0):
    """Recursively walk JS props looking for article body content."""
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, str):
        return obj if len(obj) > 500 else None
    if isinstance(obj, dict):
        for key in ("articleBody", "content", "body", "text", "description",
                     "articleContent", "full_content", "article_text"):
            val = obj.get(key)
            if isinstance(val, str) and len(val) > 300:
                return val
            if isinstance(val, dict):
                r = _walk_for_content(val, depth + 1)
                if r:
                    return r
        for key in ("article", "post", "page", "props"):
            val = obj.get(key)
            r = _walk_for_content(val, depth + 1)
            if r:
                return r
    if isinstance(obj, (list, tuple)):
        for item in obj:
            r = _walk_for_content(item, depth + 1)
            if r:
                return r
    return None


def _walk_for_title(obj, depth=0):
    if depth > 6 or obj is None:
        return None
    if isinstance(obj, dict):
        for key in ("headline", "title", "name", "heading"):
            val = obj.get(key)
            if isinstance(val, str) and len(val) > 5 and len(val) < 300:
                return val
        for v in obj.values():
            r = _walk_for_title(v, depth + 1)
            if r:
                return r
    return None


def extract_readability(html: str, url: str = "") -> dict | None:
    """
    DOM-based extraction: strip all non-article elements, then pick the
    container that actually looks like the article body — not just the
    first one whose class/id happens to contain a matching word.
    Works when content IS in the DOM but hidden by overlay.
    """
    soup = BeautifulSoup(html, "lxml")

    # Strip noise tags
    for tag in soup(["script", "style", "noscript", "iframe", "svg",
                     "nav", "footer", "aside", "header", "form", "button",
                     "select", "input", "textarea"]):
        tag.decompose()

    # Strip ad/promo elements by class/id. Matched as a whole word bounded
    # by a hyphen/underscore/space or the start/end of the string — e.g.
    # this correctly catches compound classes like "ad-slot" or
    # "cookie-banner" (a naive per-token check misses these, since they're
    # one hyphenated token, not two space-separated ones) while no longer
    # nuking unrelated ids that just happen to contain the word as a raw
    # substring, like "readMoreButton", "leaderboard-standings" or
    # "already-loaded" all containing "ad", or "download-pdf" containing
    # "ad" too — a bug that was silently stripping real article containers
    # on plenty of pages, including ones with no ads on them at all.
    noise_classes = ["ad", "ads", "advert", "advertisement", "promo",
                     "promotion", "banner", "newsletter", "related",
                     "recommended", "share", "social", "comments",
                     "paywall", "gate", "wall", "overlay", "modal",
                     "popup", "pop-up", "subscription", "cta",
                     "sidebar", "widget", "cookie", "consent"]
    noise_re = re.compile(
        r'(?:^|[-_\s])(?:' + "|".join(re.escape(w) for w in noise_classes) + r')(?:[-_\s]|$)',
        re.IGNORECASE)
    def is_noise(el):
        cls = " ".join(el.get("class", []))
        eid = el.get("id", "") or ""
        return bool(noise_re.search(cls) or noise_re.search(eid))
    for el in soup.find_all(True):
        if el.parent is None:
            continue  # already removed as part of an earlier decompose()
        if is_noise(el):
            el.decompose()

    # Remove elements with paywall-indicating text
    paywall_words = ["subscribe", "subscribe now", "sign up", "unlock",
                     "premium", "become a member", "continue reading",
                     "limit reached", "free article", "remaining",
                     "subscription required", "please log in"]
    for el in soup.find_all(["div", "section", "p", "span"]):
        text = el.get_text(strip=True).lower()
        if any(w in text for w in paywall_words) and len(text) < 200:
            el.decompose()

    # Find the main content container. An <article> tag or role=main <main>
    # is trusted outright — those are explicit semantic signals. Otherwise,
    # score every plausible container by how much real paragraph text it
    # holds rather than taking the FIRST div/section whose class or id
    # merely mentions "content"/"article" — a page's first such match in
    # document order is very often a small "related content" or "trending"
    # rail above the real story, not the story itself.
    article = soup.find("article") or soup.find("main", {"role": "main"}) or soup.find("main")
    if not article:
        candidates = soup.find_all(["div", "section"])
        best, best_score = None, 0
        for el in candidates:
            paras = el.find_all("p", recursive=True)
            text_len = sum(len(p.get_text(strip=True)) for p in paras)
            if len(paras) < 2 or text_len < 200:
                continue
            # Prefer containers whose own class/id hints at being the story,
            # but this is now a tie-breaking bonus, not the sole criterion.
            hint = 1.15 if re.search(r'content|article|story|body|post|entry',
                                      f'{" ".join(el.get("class", []))} {el.get("id", "")}', re.I) else 1.0
            score = text_len * hint
            if score > best_score:
                best, best_score = el, score
        article = best or soup.body

    if not article:
        return None

    # Get title
    title_el = soup.find("h1") or article.find("h1")
    title = title_el.get_text(strip=True) if title_el else ""

    # Get body text
    for tag in article(["script", "style", "noscript"]):
        tag.decompose()
    body = article.get_text("\n", strip=True)

    if len(body) < 300:
        return None

    return {
        "title": title,
        "body": body[:200000],  # 200KB limit
        "source": "readability",
    }


def extract_amp_content(html: str, url: str) -> dict | None:
    """
    Check if page has an AMP version and extract from its JSON.
    AMP pages often serve full content unrestricted.
    """
    soup = BeautifulSoup(html, "lxml")
    amp_link = soup.find("link", {"rel": "amphtml"})
    if not amp_link:
        return None
    # Just note the AMP URL for the orchestrator
    return {"amp_url": urljoin(url, amp_link.get("href", "")), "source": "amp-hint"}


def extract_og_tags(html: str) -> dict:
    """Extract title, description from OG/ meta tags as fallback."""
    soup = BeautifulSoup(html, "lxml")
    result = {}
    for prop, key in [("og:title", "title"), ("og:description", "body"),
                      ("description", "body"), ("twitter:title", "title")]:
        tag = soup.find("meta", {"property": prop}) or soup.find("meta", {"name": prop})
        if tag and tag.get("content"):
            content = tag["content"]
            if key == "title":
                result["title"] = content
            elif key == "body" and not result.get("body") or len(content) > len(result.get("body", "")):
                result["body"] = content
    return result or None


def extract_image(html: str, url: str = "") -> str:
    """
    Publisher hero image — og:image / twitter:image meta tags first (these
    are what the article was actually tagged with for sharing, so they're
    the most reliable single image), falling back to the largest plausible
    content <img> if neither meta tag is present. Always returned as an
    absolute URL, or "" if nothing usable was found.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for prop in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
        tag = soup.find("meta", {"property": prop}) or soup.find("meta", {"name": prop})
        content = tag.get("content", "").strip() if tag else ""
        if content:
            resolved = urljoin(url, content) if url else content
            if resolved.lower().startswith(("http://", "https://")):
                return resolved

    # No meta image — fall back to the first reasonably-sized <img> inside
    # the likely article body, skipping obvious icon/logo/tracking pixels.
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        cls = f'{" ".join(img.get("class", []))} {img.get("id", "")}'.lower()
        if re.search(r'\b(logo|icon|avatar|sprite|pixel|spinner|placeholder)\b', cls):
            continue
        try:
            w, h = int(img.get("width", 0) or 0), int(img.get("height", 0) or 0)
        except ValueError:
            w, h = 0, 0
        if (w and w < 150) or (h and h < 150):
            continue
        resolved = urljoin(url, src) if url else src
        if resolved.lower().startswith(("http://", "https://")):
            return resolved
    return ""


# ═══════════════════════════════════════════════════════════════════════
# Orchestrators — app.py imports these directly. Everything above
# this line is one individual extraction *strategy*; these functions are
# what actually try them in order and normalize the result into the shape
# the rest of the app expects. (These were missing entirely, which is why
# `from scraper.extractors import extract_article, extract_links,
# extract_pdf_links` failed at import time and crashed the whole service
# on startup — none of the strategies above were ever unreachable, the
# names the rest of the app calls simply didn't exist yet.)
# ═══════════════════════════════════════════════════════════════════════

def extract_article(html: str, url: str = "") -> dict | None:
    """
    Try every content-extraction strategy, strongest/least-lossy signal
    first, and normalize whichever succeeds into
    {title, text, html, source, author, date_published}.
    Returns None only if every strategy comes back empty.
    """
    if not html:
        return None

    result = (
        extract_from_json_ld(html)
        or extract_from_next_data(html)
        or extract_readability(html, url)
    )
    if not result:
        og = extract_og_tags(html)
        if og and og.get("body"):
            result = {"title": og.get("title", ""), "body": og["body"], "source": "og-tags"}
    if not result or not result.get("body"):
        return None

    return {
        "title": result.get("title", ""),
        "text": result.get("body", ""),
        "html": html,
        "source": result.get("source", ""),
        "author": result.get("author", ""),
        "date_published": result.get("date_published", ""),
    }


def extract_links(html: str, base_url: str = "") -> list[str]:
    """Every distinct, resolved <a href> on the page (fragment stripped)."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(base_url, href) if base_url else href
        full = full.split("#", 1)[0]
        if full and full not in seen:
            seen.add(full)
            out.append(full)
    return out


def extract_pdf_links(html: str, base_url: str = "") -> list[str]:
    """Every link on the page whose path looks like a PDF."""
    if not html:
        return []
    pdf_pattern = re.compile(r"\.pdf(\?.*)?$", re.I)
    soup = BeautifulSoup(html, "lxml")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        full = urljoin(base_url, href) if base_url else href
        if pdf_pattern.search(urlparse(full).path) and full not in seen:
            seen.add(full)
            out.append(full)
    return out
