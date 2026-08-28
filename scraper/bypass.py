"""
Multi-layer authorization bypass engine.
Implements the cheat-sheet strategies for bypassing access controls.
"""
import asyncio
import base64
import hashlib
import json
import random
import re
import time
from io import BytesIO
from typing import Any
from urllib.parse import urlparse, urljoin, parse_qs, urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from PIL import Image

# ─── IMPOSTER PROFILES ──────────────────────────────────────────────────

TLS_IMPOSTERS = [
    "chrome124", "chrome123", "chrome120",
    "safari17_0", "safari16_5",
    "edge101", "edge99",
    "firefox123", "firefox118",
]

BOT_AGENTS = [
    # Googlebot variants
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 10_0 like Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Version/10.0 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    # Bingbot
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    # DuckDuckBot
    "Mozilla/5.0 (compatible; DuckDuckBot-Https/1.1; https://duckduckgo.com/duckduckbot)",
    # Facebook crawler
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Mozilla/5.0 (compatible; FacebookBot/1.0; +https://developers.facebook.com/docs/sharing/bot)",
    # Twitter
    "Twitterbot/1.0",
    # Apple/Google preview bots
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15 (Applebot/0.1)",
    "Mozilla/5.0 (compatible; Google-Apps-Script; apps-script; +https://script.google.com)",
]

REAL_BROWSER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

SOCIAL_REFERERS = [
    "https://t.co/",
    "https://www.facebook.com/",
    "https://l.facebook.com/l.php",
    "https://news.google.com/",
    "https://www.google.com/",
    "https://www.google.com/search?q=",
    "https://reddit.com/",
    "https://www.reddit.com/r/all/",
    "https://www.linkedin.com/",
    "https://out.reddit.com/",
    "https://x.com/",
    "https://twitter.com/",
]

# ─── PAYWALL / BOT DETECTION SCRIPTS TO NEUTRALIZE ─────────────────────

BLOCKED_SCRIPT_PATTERNS = [
    # Paywall providers
    "piano.io", "tinypass", "tp_", "permutive", "zephr",
    "paywall", "poool", "sophi", "marfeel", "leaky-paywall",
    "moneypenny", "atlas.recaptcha",
    # Meter detection
    "meter.", "metered", "article-count", "nqs",
    # Analytics that feed paywall decisions
    "chartbeat", "parsely", "criteo", "outbrain", "taboola",
    # Consent/CMP
    "cmp.quantcast", "consentmanager", "onesignal",
    # Google services that track
    "doubleclick", "googlesyndication", "googletagmanager",
    "google-analytics", "gtag", "pagead2",
]

# ─── ANTI-PAYWALL COOKIE NAMES ──────────────────────────────────────────

ANTI_PAYWALL_COOKIE_NAMES = [
    "piano", "tinypass", "tp_", "permutive", "pa-", "meter",
    "nytimes_meter", "wp-settings", "article_views", "read_count",
    "visits", "subscription", "premium", "access", "wall",
    "ngage", "nqs", "nq_meter", "nq_visited", "nq_article_count",
    "ar_debug", "articleCount", "articleCountSession",
]


def build_anti_paywall_cookies(domain: str) -> list[dict]:
    """
    Inject cookies that trick paywall scripts into thinking:
    - User has never visited (meter reset)
    - User has an active subscription
    - User came from social media
    """
    cookies = []
    base = {"domain": domain, "path": "/"}

    # Reset ALL meter counters to zero
    for name in ANTI_PAYWALL_COOKIE_NAMES:
        if any(x in name.lower() for x in ["meter", "count", "visit", "view", "nq_"]):
            cookies.append({**base, "name": name, "value": "0"})

    # Fake subscription cookies for common paywall providers
    cookies.append({**base, "name": "tp_subscriber", "value": "true"})
    cookies.append({**base, "name": "tp_is_logged_in", "value": "true"})
    cookies.append({**base, "name": "piano_user_id", "value": f"bot_{random.randint(100000,999999)}"})
    cookies.append({**base, "name": "piano_is_subscriber", "value": "true"})
    cookies.append({**base, "name": "piano_token", "value": hashlib.sha256(str(random.random()).encode()).hexdigest()})

    # Fake that user came from Google/search
    cookies.append({**base, "name": "ref", "value": "google"})
    cookies.append({**base, "name": "utm_source", "value": "google"})
    cookies.append({**base, "name": "fb_referer", "value": "1"})

    return cookies


def build_browser_like_headers(url: str, use_bot_ua: bool = False) -> dict:
    """Craft headers indistinguishable from a real browser."""
    parsed = urlparse(url)
    agent = random.choice(BOT_AGENTS if use_bot_ua else REAL_BROWSER_AGENTS)
    return {
        "User-Agent": agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": random.choice(SOCIAL_REFERERS),
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Connection": "keep-alive",
        "Host": parsed.hostname or "",
    }


# ═══════════════════════════════════════════════════════════════════════
# LAYER 0: TLS-Impersonated HTTP (curl_cffi)
# ═══════════════════════════════════════════════════════════════════════

async def try_http_fetch(url: str, cookies: list[dict] | None = None,
                         timeout: int = 20) -> tuple[str | None, dict]:
    """
    60-70% of bot-protected sites pass with TLS impersonation alone.

    Returns (html_text, metadata).
    """
    imposter = random.choice(TLS_IMPOSTERS)
    headers = build_browser_like_headers(url, use_bot_ua=False)

    # Also try with bot UA if first attempt fails
    for attempt, ua_type in enumerate([headers, {**headers, "User-Agent": random.choice(BOT_AGENTS)}]):
        try:
            resp = cffi_requests.get(
                url,
                headers=ua_type,
                impersonate=imposter,
                timeout=timeout,
                cookies={c["name"]: c["value"] for c in (cookies or [])},
                allow_redirects=True,
            )
            if resp.status_code == 200 and resp.text and len(resp.text) > 300:
                return resp.text, {
                    "method": "curl_cffi",
                    "impersonate": imposter,
                    "status": resp.status_code,
                    "bot_ua": ua_type != headers,
                }
        except Exception:
            continue

    return None, {"method": "curl_cffi", "status": "failed"}


# ═══════════════════════════════════════════════════════════════════════
# LAYER 1: Archive/Cache Fallback
# ═══════════════════════════════════════════════════════════════════════

ARCHIVE_MIRRORS = [
    "https://archive.ph/newest/{url}",
    "https://archive.is/newest/{url}",
    "https://webcache.googleusercontent.com/search?q=cache:{url}",
    "https://r.jina.ai/http://{url}",
    "https://12ft.io/proxy?q={url}",
    "https://corsproxy.io/?url={url}",
    "https://textise dot iitty/{url}",
]

ARCHIVE_PROXY_ROTATION = [
    # These are proxy services — check for updated URLs before production use
    "https://api.allorigins.win/get?url={url}",
    "https://api.codetabs.com/v1/proxy?quest={url}",
]


async def try_archive_fetch(url: str, timeout: int = 25) -> tuple[str | None, dict]:
    """
    Server-side paywalls: fetch from archives that already have full content.
    """
    for mirror in ARCHIVE_MIRRORS:
        target = mirror.format(url=url)
        try:
            resp = cffi_requests.get(
                target,
                impersonate=random.choice(TLS_IMPOSTERS),
                timeout=timeout,
                headers={"User-Agent": random.choice(BOT_AGENTS)},
                allow_redirects=True,
            )
            if resp.status_code == 200 and resp.text and len(resp.text) > 1500:
                return resp.text, {"method": "archive", "mirror": mirror.split("/")[2]}
        except Exception:
            continue

    # Try proxy rotation services
    for proxy in ARCHIVE_PROXY_ROTATION:
        target = proxy.format(url=url)
        try:
            resp = cffi_requests.get(
                target,
                impersonate="chrome124",
                timeout=timeout + 10,
            )
            if resp.status_code == 200:
                # Some proxies wrap content in JSON
                try:
                    data = resp.json()
                    content = data.get("contents") or data.get("body") or resp.text
                except Exception:
                    content = resp.text
                if content and len(str(content)) > 1000:
                    return str(content), {"method": "proxy", "service": proxy.split("//")[1].split("/")[0]}
        except Exception:
            continue

    return None, {"method": "archive", "status": "exhausted"}


# ═══════════════════════════════════════════════════════════════════════
# LAYER 2: AMP / Mobile / Print Version Detection
# ═══════════════════════════════════════════════════════════════════════

AMP_SIGNALS = [
    lambda u: u.replace("://", "://amp.") if not u.startswith("https://amp.") and not u.startswith("http://amp.") else None,
    lambda u: u + "?amp=1" if "?" not in u else u + "&amp=1",
    lambda u: u + "/amp" if not u.endswith("/amp") else None,
    lambda u: u.replace("/article/", "/amp/"),
    lambda u: u.replace("/story/", "/amp/"),
    lambda u: u.replace("https://", "https://m.").replace("http://", "http://m."),
    lambda u: u + "?output=print" if "?" not in u else u + "&output=print",
    lambda u: u.replace("/article/", "/print/"),
    lambda u: u.replace("/news/", "/print/"),
]


async def try_amp_mobile_print(url: str, timeout: int = 15) -> tuple[str | None, dict]:
    """Probe alternative versions (AMP, mobile, print) that bypass paywalls."""
    for transformer in AMP_SIGNALS:
        try:
            alt_url = transformer(url)
            if not alt_url or alt_url == url:
                continue
            resp = cffi_requests.get(
                alt_url,
                impersonate="chrome124",
                timeout=timeout,
                headers={"User-Agent": random.choice(BOT_AGENTS)},
                allow_redirects=True,
            )
            if resp.status_code == 200 and resp.text and len(resp.text) > 1000:
                return resp.text, {"method": "alt_version", "alt_url": alt_url}
        except Exception:
            continue
    return None, {"method": "alt_version", "status": "failed"}


# ═══════════════════════════════════════════════════════════════════════
# LAYER 3: Cookie / Session Injection
# ═══════════════════════════════════════════════════════════════════════

def parse_cookie_header(cookie_str: str) -> list[dict]:
    """Parse browser-exported cookies into our format."""
    cookies = []
    for line in cookie_str.split(";"):
        if "=" in line:
            name, value = line.strip().split("=", 1)
            cookies.append({"name": name, "value": value, "domain": "", "path": "/"})
    return cookies


def extract_cf_clearance_from_html(html: str) -> str | None:
    """
    Cloudflare issues cf_clearance cookie after JS challenge.
    Extract from HTML meta refresh or redirect URL.
    """
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", {"http-equiv": "refresh"})
    if meta and meta.get("content"):
        m = re.search(r'cf_clearance=([^&;%]+)', meta["content"])
        if m:
            return m.group(1)
    return None


# ═══════════════════════════════════════════════════════════════════════
# PDF AS INTERMEDIARY (print-to-PDF captures rendered content)
# ═══════════════════════════════════════════════════════════════════════

def html_has_paywall_markers(html: str) -> bool:
    """Quick heuristic: check if content appears to be paywalled."""
    text = BeautifulSoup(html, "lxml").get_text()[:5000].lower()
    indicators = ["subscribe now", "sign up to read", "unlock this",
                  "become a subscriber", "subscription required",
                  "you've reached your", "free article limit",
                  "log in to read", "premium content", "continue reading",
                  "please subscribe", "this is a premium"]
    return any(i in text for i in indicators)


# ─── BYPASS PIPELINE ORCHESTRATOR ──────────────────────────────────────

async def run_bypass_pipeline(url: str, cookies: list[dict] | None = None,
                              auth_token: str | None = None,
                              timeout: int = 90) -> dict:
    """
    Full orchestration: try each layer in order.
    Returns the HTML + metadata from the first successful layer.
    """
    chain = []
    result_html = None
    result_meta = {}
    all_cookies = list(cookies or [])

    # Prepend anti-paywall cookies
    if cookies:
        all_cookies = cookies
    all_cookies.extend(build_anti_paywall_cookies(urlparse(url).hostname or ""))

    # Layer 0: TLS-impersonated HTTP
    chain.append("curl_cffi")
    html, meta = await try_http_fetch(url, all_cookies, min(timeout, 20))
    if html:
        result_html = html
        result_meta = {**meta, "layer": "curl_cffi"}

    # Layer 1: AMP / mobile / print versions
    if not result_html or html_has_paywall_markers(result_html):
        chain.append("alt_version")
        html, meta = await try_amp_mobile_print(url, min(timeout, 15))
        if html and not html_has_paywall_markers(html):
            result_html = html
            result_meta = {**meta, "layer": "alt_version"}

    # Layer 2: Archives / cached copies
    if not result_html or html_has_paywall_markers(result_html):
        chain.append("archive")
        html, meta = await try_archive_fetch(url, min(timeout, 25))
        if html:
            result_html = html
            result_meta = {**meta, "layer": "archive"}

    # If we got nothing at all, return failure
    if not result_html:
        return {
            "success": False,
            "chain": chain,
            "error": "All bypass layers exhausted",
            "html": None,
        }

    return {
        "success": True,
        "chain": chain,
        "html": result_html,
        "meta": result_meta,
    }
