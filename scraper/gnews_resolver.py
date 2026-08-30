"""
Google News URL resolver.

Supported Google News forms:
    https://news.google.com/rss/articles/<ID>
    https://news.google.com/articles/<ID>
    https://news.google.com/read/<ID>
    https://news.google.com/__i/rss/rd/articles/<ID>

Resolution strategy:

1. Direct embedded URL extraction from the article ID.
2. googlenewsdecoder package, when installed.
3. Google article-page decoding using Google's batchexecute endpoint.
4. Validate the result before returning it.

This module only resolves Google News redirect URLs to publisher URLs.
It does not bypass publisher authentication, paywalls, or access controls.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

try:
    from selectolax.parser import HTMLParser
except Exception:
    HTMLParser = None

try:
    from googlenewsdecoder import gnewsdecoder as _external_gnewsdecoder
except Exception:
    _external_gnewsdecoder = None


LOG = logging.getLogger("gnews_resolver")


GOOGLE_HOSTS = {
    "news.google.com",
    "www.news.google.com",
}

ARTICLE_PATH_RE = re.compile(
    r"^/(?:rss/)?articles/([^/?#]+)",
    re.IGNORECASE,
)

READ_PATH_RE = re.compile(
    r"^/read/([^/?#]+)",
    re.IGNORECASE,
)

OLD_ARTICLE_PATH_RE = re.compile(
    r"^/__i/rss/rd/articles/([^/?#]+)",
    re.IGNORECASE,
)

URL_RE = re.compile(
    r"https?://[^\s\"'<>]+",
    re.IGNORECASE,
)

VALID_SCHEME_RE = re.compile(r"^https?$", re.IGNORECASE)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class ResolveResult:
    success: bool
    input_url: str
    resolved_url: Optional[str] = None
    method: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GNewsResolver:
    """
    Production-oriented Google News resolver.

    Important:
    - Each strategy has a bounded timeout.
    - A successful result is validated before being returned.
    - Google URLs are never returned as a successful publisher URL.
    """

    def __init__(
        self,
        timeout: float = 7.0,
        decoder_timeout: float = 8.0,
        max_response_bytes: int = 2_000_000,
    ):
        self.timeout = timeout
        self.decoder_timeout = decoder_timeout
        self.max_response_bytes = max_response_bytes

        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_google_news_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url.strip())

            if parsed.scheme.lower() not in {"http", "https"}:
                return False

            host = parsed.hostname
            if not host:
                return False

            host = host.lower().rstrip(".")

            if host not in GOOGLE_HOSTS:
                return False

            path = parsed.path.lower()

            return (
                "/articles/" in path
                or "/read/" in path
                or "/rss/articles/" in path
                or "/__i/rss/rd/articles/" in path
            )

        except Exception:
            return False

    def resolve(self, url: str) -> ResolveResult:
        started = time.monotonic()
        original = (url or "").strip()

        if not original:
            return ResolveResult(
                success=False,
                input_url=original,
                error="empty_url",
            )

        if not self.is_google_news_url(original):
            return ResolveResult(
                success=True,
                input_url=original,
                resolved_url=original,
                method="passthrough",
                elapsed_ms=self._elapsed(started),
            )

        article_id = self._extract_article_id(original)

        if not article_id:
            return ResolveResult(
                success=False,
                input_url=original,
                error="google_news_article_id_not_found",
                elapsed_ms=self._elapsed(started),
            )

        LOG.info("Resolving Google News ID: %s...", article_id[:30])

        # --------------------------------------------------------------
        # Strategy 1: direct payload extraction
        # --------------------------------------------------------------
        try:
            candidate = self._extract_embedded_url(article_id)

            if self._valid_publisher_url(candidate):
                return self._success(
                    original,
                    candidate,
                    "embedded_payload",
                    started,
                )
        except Exception as exc:
            LOG.debug("embedded payload failed: %s", exc)

        # --------------------------------------------------------------
        # Strategy 2: installed googlenewsdecoder
        # --------------------------------------------------------------
        if _external_gnewsdecoder is not None:
            candidate = self._external_decoder(original)

            if self._valid_publisher_url(candidate):
                return self._success(
                    original,
                    candidate,
                    "googlenewsdecoder",
                    started,
                )

        # --------------------------------------------------------------
        # Strategy 3: Google article page + batchexecute
        # --------------------------------------------------------------
        candidate = self._batchexecute_resolve(article_id)

        if self._valid_publisher_url(candidate):
            return self._success(
                original,
                candidate,
                "batchexecute",
                started,
            )

        return ResolveResult(
            success=False,
            input_url=original,
            error="google_news_resolution_failed",
            elapsed_ms=self._elapsed(started),
        )

    # ------------------------------------------------------------------
    # Article ID extraction
    # ------------------------------------------------------------------

    def _extract_article_id(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        path = parsed.path

        match = OLD_ARTICLE_PATH_RE.match(path)
        if match:
            return match.group(1)

        match = ARTICLE_PATH_RE.match(path)
        if match:
            return match.group(1)

        match = READ_PATH_RE.match(path)
        if match:
            return match.group(1)

        return None

    # ------------------------------------------------------------------
    # Strategy 1
    # ------------------------------------------------------------------

    def _extract_embedded_url(self, article_id: str) -> Optional[str]:
        """
        Try the URL-bearing portion of Google's encoded article ID.

        This is deliberately conservative. We only accept a URL if it
        survives validation as a normal HTTP(S) publisher URL.
        """

        token = article_id.split("?", 1)[0]

        # Google uses URL-safe Base64 in these IDs.
        candidates = [
            token,
            token.replace("-", "+").replace("_", "/"),
        ]

        decoded_blobs: list[bytes] = []

        for candidate in candidates:
            try:
                padded = candidate + "=" * (-len(candidate) % 4)
                raw = base64.b64decode(
                    padded,
                    validate=False,
                )

                if raw:
                    decoded_blobs.append(raw)

            except (binascii.Error, ValueError):
                continue

        for raw in decoded_blobs:
            # First inspect UTF-8 text directly.
            text = raw.decode("utf-8", errors="ignore")

            urls = URL_RE.findall(text)

            for url in urls:
                cleaned = self._clean_candidate(url)

                if self._valid_publisher_url(cleaned):
                    return cleaned

            # Some payloads contain nested binary/text fields.
            # Search every reasonably sized printable section.
            printable = re.sub(
                rb"[^\x20-\x7e]+",
                b" ",
                raw,
            ).decode("ascii", errors="ignore")

            urls = URL_RE.findall(printable)

            for url in urls:
                cleaned = self._clean_candidate(url)

                if self._valid_publisher_url(cleaned):
                    return cleaned

        return None

    # ------------------------------------------------------------------
    # Strategy 2
    # ------------------------------------------------------------------

    def _external_decoder(self, url: str) -> Optional[str]:
        try:
            result = _external_gnewsdecoder(
                url,
                interval=0,
            )

            if not isinstance(result, dict):
                return None

            if not result.get("status"):
                LOG.debug(
                    "googlenewsdecoder unsuccessful: %s",
                    result.get("message"),
                )
                return None

            candidate = result.get("decoded_url")

            return self._clean_candidate(candidate)

        except TypeError:
            # Older versions may not accept interval=.
            try:
                result = _external_gnewsdecoder(url)

                if isinstance(result, dict) and result.get("status"):
                    return self._clean_candidate(
                        result.get("decoded_url")
                    )

            except Exception as exc:
                LOG.debug(
                    "external decoder fallback failed: %s",
                    exc,
                )

        except Exception as exc:
            LOG.warning(
                "googlenewsdecoder failed: %s",
                exc,
            )

        return None

    # ------------------------------------------------------------------
    # Strategy 3: Google's internal batchexecute flow
    # ------------------------------------------------------------------

    def _batchexecute_resolve(
        self,
        article_id: str,
    ) -> Optional[str]:
        """
        Reproduce the public reverse-engineered Google News decoding flow:

        1. GET /articles/<ID>
        2. Extract data-n-a-sg and data-n-a-ts
        3. POST garturlreq to batchexecute
        4. Extract the resulting publisher URL
        """

        for endpoint in (
            f"https://news.google.com/articles/{article_id}",
            f"https://news.google.com/rss/articles/{article_id}",
        ):
            try:
                response = self.session.get(
                    endpoint,
                    timeout=self.decoder_timeout,
                    allow_redirects=True,
                )

                response.raise_for_status()

                if len(response.content) > self.max_response_bytes:
                    LOG.debug("Google response too large")
                    continue

                html = response.text

                signature, timestamp = self._extract_google_params(
                    html
                )

                if not signature or not timestamp:
                    continue

                candidate = self._call_batchexecute(
                    article_id,
                    timestamp,
                    signature,
                )

                if self._valid_publisher_url(candidate):
                    return candidate

            except requests.RequestException as exc:
                LOG.debug(
                    "Google endpoint failed %s: %s",
                    endpoint,
                    exc,
                )

            except Exception as exc:
                LOG.debug(
                    "Google decoding failed: %s",
                    exc,
                )

        return None

    def _extract_google_params(
        self,
        html: str,
    ) -> tuple[Optional[str], Optional[str]]:

        # HTML attribute form.
        signature_patterns = [
            r'data-n-a-sg=["\']([^"\']+)["\']',
            r'"data-n-a-sg"\s*:\s*"([^"]+)"',
        ]

        timestamp_patterns = [
            r'data-n-a-ts=["\']([^"\']+)["\']',
            r'"data-n-a-ts"\s*:\s*"([^"]+)"',
        ]

        signature = None
        timestamp = None

        for pattern in signature_patterns:
            match = re.search(
                pattern,
                html,
                re.IGNORECASE,
            )

            if match:
                signature = match.group(1)
                break

        for pattern in timestamp_patterns:
            match = re.search(
                pattern,
                html,
                re.IGNORECASE,
            )

            if match:
                timestamp = match.group(1)
                break

        return signature, timestamp

    def _call_batchexecute(
        self,
        article_id: str,
        timestamp: str,
        signature: str,
    ) -> Optional[str]:

        inner = [
            "garturlreq",
            [
                [
                    "X",
                    "X",
                    ["X", "X"],
                    None,
                    None,
                    1,
                    1,
                    "US:en",
                    None,
                    1,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    1,
                ],
                "X",
                "X",
                1,
                [1, 1, 1],
                1,
                1,
                None,
                0,
                0,
                None,
                0,
            ],
            article_id,
            int(timestamp),
            signature,
        ]

        rpc_payload = json.dumps(
            inner,
            separators=(",", ":"),
            ensure_ascii=False,
        )

        # Google's RPC envelope.
        payload = (
            "f.req="
            + requests.utils.quote(
                json.dumps(
                    [[
                        "Fbv4je",
                        rpc_payload,
                        None,
                        "generic",
                    ]],
                    separators=(",", ":"),
                )
            )
            + "&"
        )

        url = (
            "https://news.google.com/_/DotsSplashUi/"
            "data/batchexecute"
            "?rpcids=Fbv4je"
            "&source-path=/"
            "&f.sid=-"
            "&bl=boq_news-web_"
            "&hl=en-US"
            "&gl=US"
        )

        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": "https://news.google.com",
            "Referer": "https://news.google.com/",
            "User-Agent": USER_AGENT,
        }

        try:
            response = self.session.post(
                url,
                data=payload,
                headers=headers,
                timeout=self.decoder_timeout,
            )

            response.raise_for_status()

            text = response.text

            return self._extract_url_from_batchexecute(
                text
            )

        except requests.RequestException as exc:
            LOG.debug(
                "batchexecute HTTP failure: %s",
                exc,
            )
            return None

    def _extract_url_from_batchexecute(
        self,
        text: str,
    ) -> Optional[str]:

        # Fast path: find normal URLs in the RPC response.
        for match in URL_RE.findall(text):
            candidate = self._clean_candidate(match)

            if self._valid_publisher_url(candidate):
                return candidate

        # The response contains escaped JSON. Decode layers repeatedly.
        current = text

        for _ in range(4):
            try:
                current = bytes(
                    current,
                    "utf-8",
                ).decode(
                    "unicode_escape",
                    errors="ignore",
                )
            except Exception:
                break

            for match in URL_RE.findall(current):
                candidate = self._clean_candidate(match)

                if self._valid_publisher_url(candidate):
                    return candidate

        return None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _valid_publisher_url(
        self,
        url: Optional[str],
    ) -> bool:

        if not url or not isinstance(url, str):
            return False

        url = self._clean_candidate(url)

        try:
            parsed = urlparse(url)

            if not VALID_SCHEME_RE.match(
                parsed.scheme or ""
            ):
                return False

            host = (parsed.hostname or "").lower().rstrip(".")

            if not host:
                return False

            # Never accept another Google News redirect as resolved.
            if host in GOOGLE_HOSTS:
                return False

            if host.endswith(".google.com"):
                return False

            # Avoid obvious malformed candidates.
            if "." not in host and host != "localhost":
                return False

            return True

        except Exception:
            return False

    def _clean_candidate(
        self,
        url: Optional[str],
    ) -> Optional[str]:

        if not url or not isinstance(url, str):
            return None

        value = unquote(url.strip())

        # Remove JSON/string escaping.
        value = value.replace("\\/", "/")
        value = value.replace('\\"', '"')

        value = value.strip(
            " \t\r\n\"'<>[](){}"
        )

        # Remove trailing punctuation accidentally captured by regex.
        while value and value[-1] in ".,;":
            value = value[:-1]

        return value

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _elapsed(started: float) -> int:
        return int(
            (time.monotonic() - started) * 1000
        )

    def _success(
        self,
        original: str,
        resolved: str,
        method: str,
        started: float,
    ) -> ResolveResult:

        return ResolveResult(
            success=True,
            input_url=original,
            resolved_url=resolved,
            method=method,
            elapsed_ms=self._elapsed(started),
        )


_default_resolver = GNewsResolver()


def is_gnews_url(url: str) -> bool:
    return _default_resolver.is_google_news_url(url)


def resolve_gnews_url(url: str) -> ResolveResult:
    return _default_resolver.resolve(url)


def resolve(url: str) -> str:
    """
    Backwards-compatible helper.

    Returns the resolved publisher URL when successful.
    Raises RuntimeError when Google News resolution fails.
    """

    result = resolve_gnews_url(url)

    if result.success and result.resolved_url:
        return result.resolved_url

    raise RuntimeError(
        result.error or "Google News resolution failed"
    )


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if len(sys.argv) != 2:
        print(
            "Usage: python -m scraper.gnews_resolver URL"
        )
        raise SystemExit(2)

    result = resolve_gnews_url(sys.argv[1])

    print(
        json.dumps(
            result.to_dict(),
            indent=2,
            ensure_ascii=False,
        )
    )
