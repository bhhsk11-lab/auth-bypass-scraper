"""Central configuration — all env-tunable, Cloud Run friendly."""
import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Server ──────────────────────────────────────────────────────────
    port: int = int(os.getenv("PORT", 8080))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    max_batch: int = int(os.getenv("MAX_BATCH", 25))
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", 6))

    # ── Anti-block layer (NEW in v3.2) ─────────────────────────────────
    # Residential/mobile proxy — routes ALL curl_cffi fetches through it.
    # Format: http://user:pass@host:port  (or socks5://...)
    proxy_url: str | None = os.getenv("PROXY_URL", None)

    # FlareSolverr sidecar URL — solves Cloudflare JS challenges, returns
    # page HTML + cf_clearance cookies we replay on direct fetches.
    # e.g. https://flaresolverr-xxxx-uc.a.run.app
    flaresolverr_url: str | None = os.getenv("FLARESOLVERR_URL", None)

    # ScraperAPI / ZenRows key — final last-resort fetch with their
    # residential pool. Only used when everything else fails.
    scraperapi_key: str | None = os.getenv("SCRAPERAPI_KEY", None)

    # Country for ScraperAPI geo-targeting (IN works well for testbook)
    scraperapi_country: str = os.getenv("SCRAPERAPI_COUNTRY", "in")

    # ── Stealth browser ─────────────────────────────────────────────────
    browser_headless: bool = True
    browser_timeout_ms: int = int(os.getenv("BROWSER_TIMEOUT_MS", 12000))

    # ── PDF / OCR ───────────────────────────────────────────────────────
    pdf_max_bytes: int = int(os.getenv("PDF_MAX_BYTES", 25 * 1024 * 1024))
    ocr_dpi: int = int(os.getenv("OCR_DPI", 200))
    ocr_max_pages: int = int(os.getenv("OCR_MAX_PAGES", 10))

    # ── Caches ──────────────────────────────────────────────────────────
    cache_max_items: int = int(os.getenv("CACHE_MAX_ITEMS", 500))

    # ── Explorer ────────────────────────────────────────────────────────
    explore_max_depth: int = int(os.getenv("EXPLORE_MAX_DEPTH", 3))
    explore_max_pages: int = int(os.getenv("EXPLORE_MAX_PAGES", 50))

    class Config:
        env_file = ".env"


settings = Settings()
