"""
Cloudflare Email/Phone Protection decoder.

Cloudflare obfuscates emails/phones as:
  - <a href="/cdn-cgi/l/email-protection#HEXHEXHEX">[email protected]</a>
  - <span data-cfemail="HEXHEXHEX">[email protected]</span>
  - <span class="__cf_email__" data-cfemail="HEXHEXHEX">

Algorithm: first hex byte = XOR key. Each following byte-pair = char XOR key.
"""
import re
from typing import Any


def cf_decode_email(hex_string: str) -> str:
    """Decode a Cloudflare-obfuscated email/phone hex string."""
    try:
        key = int(hex_string[:2], 16)
        decoded = []
        for i in range(2, len(hex_string), 2):
            byte = int(hex_string[i:i + 2], 16)
            decoded.append(chr(byte ^ key))
        return "".join(decoded)
    except (ValueError, IndexError):
        return ""


def decode_cf_protections(html: str) -> dict:
    """
    Find and decode ALL Cloudflare-protected emails and phone numbers
    in an HTML page. Returns decoded lists + the cleaned HTML.
    """
    emails: list[str] = []
    phones: list[str] = []
    decoded_html = html

    # ── Pattern 1: data-cfemail attributes (spans, any tag) ──
    # <span class="__cf_email__" data-cfemail="a3c2...">[email protected]</span>
    pattern_attr = re.compile(
        r'data-cfemail="([0-9a-fA-F]+)"[^>]*>[^<]*</[^>]+>'
    )
    for match in pattern_attr.finditer(html):
        decoded = cf_decode_email(match.group(1))
        if decoded:
            if _looks_like_phone(decoded):
                phones.append(decoded)
            else:
                emails.append(decoded)

    # Replace the whole tag with the decoded value
    def _replace_attr(m: re.Match) -> str:
        decoded = cf_decode_email(m.group(1))
        return decoded if decoded else m.group(0)
    decoded_html = pattern_attr.sub(_replace_attr, decoded_html)

    # ── Pattern 2: /cdn-cgi/l/email-protection#HEX links ──
    # <a href="/cdn-cgi/l/email-protection#a3c2...">...</a>
    pattern_link = re.compile(
        r'href="[^"]*?/cdn-cgi/l/email-protection#([0-9a-fA-F]+)"[^>]*>([^<]*)</a>'
    )
    for match in pattern_link.finditer(html):
        decoded = cf_decode_email(match.group(1))
        if decoded:
            if _looks_like_phone(decoded):
                phones.append(decoded)
            else:
                emails.append(decoded)

    def _replace_link(m: re.Match) -> str:
        decoded = cf_decode_email(m.group(1))
        if not decoded:
            return m.group(0)
        if _looks_like_phone(decoded):
            return f'<a href="tel:{decoded}">{decoded}</a>'
        return f'<a href="mailto:{decoded}">{decoded}</a>'
    decoded_html = pattern_link.sub(_replace_link, decoded_html)

    # ── Pattern 3: /cdn-cgi/l/email-protection inside raw text ──
    pattern_raw = re.compile(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')
    for match in pattern_raw.finditer(html):
        decoded = cf_decode_email(match.group(1))
        if decoded:
            phones.append(decoded) if _looks_like_phone(decoded) else emails.append(decoded)
    decoded_html = pattern_raw.sub(lambda m: cf_decode_email(m.group(1)) or m.group(0), decoded_html)

    return {
        "emails": list(set(emails)),
        "phones": list(set(phones)),
        "html": decoded_html,
        "count": len(set(emails)) + len(set(phones)),
    }


def _looks_like_phone(s: str) -> bool:
    """Heuristic: decoded value is a phone number, not an email."""
    if "@" in s:
        return False
    digits = sum(c.isdigit() for c in s)
    return digits >= 7 and digits / max(len(s), 1) > 0.5


# ── Standalone quick test ──
if __name__ == "__main__":
    # Example: <a href="/cdn-cgi/l/email-protection#6d0e030e0b0403..." >
    sample = 'Contact: <a href="/cdn-cgi/l/email-protection#53363b3e21363a2e23363b616d60207c3b372e">[email protected]</a>'
    print(decode_cf_protections(sample))
