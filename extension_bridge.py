"""
Data formatting bridge for browser extension.
Formats scraped data into the structure the extension expects.
"""
import base64
import json
from datetime import datetime
from typing import Any


def format_for_extension(url: str, title: str, body: str,
                         pdf_b64: str | None = None,
                         article_url: str | None = None,
                         pdf_url: str | None = None,
                         images: list[str] | None = None,
                         metadata: dict | None = None) -> dict:
    """
    Format scraped data into a structure the extension can consume.
    
    Extension receives:
    {
        "source_url": original URL
        "title": article title
        "body": extracted plain text
        "pdf_data": base64 PDF (for inline display)
        "pdf_url": link to PDF hosted on Cloud Run
        "article_url": link to extracted article text
        "images": base64 PNGs from scanned PDF pages
        "metadata": extracted metadata
        "timestamp": when it was scraped
        "bypass_chain": techniques used
    }
    """
    timestamp = datetime.utcnow().isoformat() + "Z"

    result = {
        "source_url": url,
        "title": title or "Untitled",
        "body": body or "",
        "pdf_data": pdf_b64,
        "pdf_url": pdf_url,
        "article_url": article_url,
        "images": images or [],
        "metadata": metadata or {},
        "timestamp": timestamp,
        "format_version": "2.0",
    }

    return result


def format_scrape_response(scrape_result: dict) -> dict:
    """Convert raw scrape result to extension-ready format."""
    return format_for_extension(
        url=scrape_result.get("url", ""),
        title=scrape_result.get("title", ""),
        body=scrape_result.get("body", ""),
        pdf_b64=scrape_result.get("pdf_b64"),
        article_url=scrape_result.get("article_url"),
        pdf_url=scrape_result.get("pdf_url"),
        images=scrape_result.get("images"),
        metadata={
            "bytes": scrape_result.get("bytes", 0),
            "method": scrape_result.get("method", ""),
            "bypass_chain": scrape_result.get("bypass_chain", []),
            "page_count": scrape_result.get("page_count", 0),
        },
    )
