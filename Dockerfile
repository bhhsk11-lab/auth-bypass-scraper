# ============================================================
# Auth-Bypass Scraper — Cloud Run (fixed build)
# ============================================================
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/.cache/huggingface

WORKDIR /app

# ── System deps (single layer, pinned) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    curl \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps: CPU-only torch first (huge size win) ──
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install torch==2.5.1 torchvision==0.20.1 \
        --index-url https://download.pytorch.org/whl/cpu && \
    pip install -r requirements.txt

# ── Playwright Chromium (as root, then open perms for appuser) ──
RUN playwright install chromium && \
    chmod -R 777 /ms-playwright

# ── App code ──
COPY . .

# ── Non-root user ──
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/.cache/huggingface && \
    chown -R appuser:appuser /app /ms-playwright

USER appuser

ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -sf http://localhost:8080/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
