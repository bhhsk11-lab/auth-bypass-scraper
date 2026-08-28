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
    DOM-based extraction: strip all non-article elements.
    Works when content IS in the DOM but hidden by overlay.
    """
    soup = BeautifulSoup(html, "lxml")

    # Strip noise tags
    for tag in soup(["script", "style", "noscript", "iframe", "svg",
                     "nav", "footer", "aside", "header", "form", "button",
                     "select", "input", "textarea"]):
        tag.decompose()

    # Strip ad/promo elements by class/id
    noise_classes = ["ad", "ads", "advert", "advertisement", "promo",
                     "promotion", "banner", "newsletter", "related",
                     "recommended", "share", "social", "comments",
                     "paywall", "gate", "wall", "overlay", "modal",
                     "popup", "pop-up", "subscription", "cta",
                     "sidebar", "widget", "cookie", "consent"]
    for cls in noise_classes:
        for el in soup.find_all(class_=lambda c: c and cls in str(c).lower().split()):
            el.decompose()
        for el in soup.find_all(id=lambda i: i and cls in str(i).lower()):
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

    # Find main content
    article = (
        soup.find("article")
        or soup.find("main", {"role": "main"})
        or soup.find("div", {"class": lambda c: c and "content" in str(c).lower()})
        or soup.find("div", {"class": lambda c: c and "article" in str(c).lower()})
        or soup.find("div", {"id": lambda i: i and ("content" in str(i).lower() or "article" in str(i).lower())})
        or soup.body
    )

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
