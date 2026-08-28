"""Centralized configuration for auth-bypass scraper service."""
import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service
    port: int = int(os.getenv("PORT", "8080"))
    environment: str = os.getenv("ENVIRONMENT", "production")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Browser
    browser_headless: bool = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
    browser_timeout: int = int(os.getenv("BROWSER_TIMEOUT", "30000"))
    browser_concurrency: int = int(os.getenv("BROWSER_CONCURRENCY", "4"))

    # Proxy (optional — unset for datacenter, set for residential)
    proxy_url: str | None = os.getenv("PROXY_URL", None)
    proxy_username: str | None = os.getenv("PROXY_USERNAME", None)
    proxy_password: str | None = os.getenv("PROXY_PASSWORD", None)

    # Hugging Face
    hf_token: str | None = os.getenv("HF_TOKEN", None)
    hf_model_ocr: str = os.getenv("HF_MODEL_OCR", "naver-clova-ix/donut-base-finetuned-docvqa")
    hf_model_extract: str = os.getenv("HF_MODEL_EXTRACT", "tencent/HunyuanOCR")

    # Redis (optional — for session cache)
    redis_url: str | None = os.getenv("REDIS_URL", None)

    # Rate limiting
    max_concurrent_scrapes: int = int(os.getenv("MAX_CONCURRENT_SCRAPES", "6"))
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "120"))

    # Auth bypass cheat codes
    enable_curl_cffi: bool = True
    enable_stealth_browser: bool = True
    enable_archive_fallback: bool = True
    enable_amp_redirect: bool = True
    enable_jsonld_extraction: bool = True
    enable_nextjs_extraction: bool = True
    enable_cookie_injection: bool = True

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
