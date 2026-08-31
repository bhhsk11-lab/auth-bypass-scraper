"""Stealth HTTP layer: curl_cffi TLS impersonation + anti-paywall tricks."""
import base64
import logging
import re
from urllib.parse import urljoin

from curl_cffi import requests as cffi_requests

from config import settings

logger = logging.getLogger("bypass")

BOT_UAS = {
    "googlebot": ("Mozilla/5.0 (compatible; Googlebot/2.1; "
                  "+http://www.google.com/bot.html)"),
    "bingbot": ("Mozilla/5.0 (compatible; bingbot/2.0; "
                "+http://www.bing.com/bingbot.htm)"),
}

SOCIAL_REFERERS = [
    "https://t.co/",
    "https://www.facebook.com/",
    "https://news.google.com/",
    "https://www.linkedin.com/",
]

ANTI_PAYWALL_COOKIES = [
    # meter-reset / fake-subscriber cookies (Piano/TinyPass-style + common CX)
    {"name": "piano_meter", "value": "0"},
    {"name": "nxti", "value": "0"},
    {"name": "ni_ispaid", "value": "1"},
    {"name": "grv_wl", "value": "1"},
    {"name": "sub", "value": "1"},
    {"name": "edition-paid", "value": "1"},
]


def _proxies() -> dict | None:
    if settings.proxy_url:
        return {"http": settings.proxy_url, "https": settings.proxy_url}
    return None


class StealthFetcher:
    """Layered HTTP fetch: TLS impersonation → bot UA → social referer."""

    IMPERSONATE_CHAIN = ["chrome124", "safari17_0", "firefox133"]

    async def fetch_html(self, url: str) -> str:
        last_err = None
        for imposter in self.IMPERSONATE_CHAIN:
            try:
                r = cffi_requests.get(
                    url,
                    impersonate=imposter,
                    timeout=settings.request_timeout,
                    allow_redirects=True,
                    proxies=_proxies(),
                    headers={
                        "Accept": ("text/html,application/xhtml+xml,"
                                   "application/xml;q=0.9,*/*;q=0.8"),
                        "Accept-Language": "en-US,en;q=0.9",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                if r.status_code == 200:
                    return r.text
                last_err = f"HTTP {r.status_code} ({imposter})"
            except Exception as e:
                last_err = str(e)
        # bot-UA fallback
        for name, ua in BOT_UAS.items():
            try:
                r = cffi_requests.get(
                    url, headers={"User-Agent": ua,
                                  "From": f"googlebot(at)googlebot.com"},
                    impersonate="chrome124",
                    timeout=settings.request_timeout,
                    allow_redirects=True, proxies=_proxies())
                if r.status_code == 200:
                    return r.text
            except Exception as e:
                last_err = str(e)
        raise RuntimeError(f"All stealth HTTP attempts failed: {last_err}")

    async def fetch_archive(self, url: str) -> str | None:
        """archive.is / web.archive.org fallback."""
        for archive in (f"https://archive.ph/newest/{url}",
                        f"https://web.archive.org/web/2024/{url}"):
            try:
                r = cffi_requests.get(
                    archive, impersonate="chrome124",
                    timeout=8, allow_redirects=True, proxies=_proxies())
                if r.status_code == 200 and len(r.text) > 2000:
                    return r.text
            except Exception:
                continue
        return None


# ── Cloudflare email/phone protection decoding ─────────────────────────

def cf_decode_email(hex_str: str) -> str:
    """1-byte XOR reversal of data-cfemail hex."""
    try:
        key = int(hex_str[:2], 16)
        return "".join(
            chr(int(hex_str[i:i + 2], 16) ^ key)
            for i in range(2, len(hex_str), 2))
    except (ValueError, IndexError):
        return ""


def cf_decode_phones(html: str) -> list[str]:
    """Phones obfuscated via /cdn-cgi/l/ links (same XOR scheme)."""
    phones = []
    for m in re.finditer(
            r'href="/cdn-cgi/l/([a-z-]+-protection[^"]*)#([0-9a-fA-F]+)"',
            html):
        decoded = cf_decode_email(m.group(2))
        if decoded and any(c.isdigit() for c in decoded) \
                and "@" not in decoded:
            phones.append(decoded)
    return phones


def decode_cf_protections(html: str) -> dict:
    """Decode all Cloudflare-protected contacts from raw HTML."""
    emails: list[str] = []
    phones: list[str] = []

    # <span data-cfemail="HEX">
    for m in re.finditer(r'data-cfemail="([0-9a-fA-F]+)"', html):
        e = cf_decode_email(m.group(1))
        if e and "@" in e:
            emails.append(e)

    # <a href="/cdn-cgi/l/email-protection#HEX">
    for m in re.finditer(
            r'href="/cdn-cgi/l/email-protection#([0-9a-fA-F]+)"', html):
        e = cf_decode_email(m.group(1))
        if e and "@" in e:
            emails.append(e)

    # phones via cdn-cgi links
    phones = cf_decode_phones(html)

    # plain-text fallback
    for m in re.finditer(
            r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', html):
        emails.append(m.group(0))
    for m in re.finditer(r'(?:\+91[\-\s]?|0)?[6-9]\d{9}', html):
        phones.append(m.group(0))

    # dedupe, preserve order
    emails = list(dict.fromkeys(emails))
    phones = list(dict.fromkeys(p.strip() for p in phones))
    return {"emails": emails, "phones": phones}
