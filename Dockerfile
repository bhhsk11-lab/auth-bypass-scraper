# ============================================================
# Base image
# ============================================================
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    TRANSFORMERS_CACHE=/app/.cache/huggingface \
    HF_HOME=/app/.cache/huggingface \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ============================================================
# System dependencies
# ============================================================
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender-dev \
        libgomp1 \
        curl \
        wget \
        gnupg && \
    rm -rf /var/lib/apt/lists/*

# Playwright Chromium
RUN playwright install chromium

# ============================================================
# Python dependencies
# ============================================================
FROM base AS builder

WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt

# ============================================================
# Runtime
# ============================================================
FROM builder AS runtime

WORKDIR /app

COPY . .

# Create a non-root user.
# UID 1000 is already used by the Playwright image,
# so use UID 1001.
RUN useradd --create-home --uid 1001 --shell /usr/sbin/nologin appuser && \
    mkdir -p /app/.cache/huggingface && \
    chown -R appuser:appuser /app && \
    chmod -R u+rwX,go+rX /app

USER appuser

# ============================================================
# Cloud Run
# ============================================================
EXPOSE 8080

# Cloud Run supplies PORT. Uvicorn listens on 8080 here.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --loop uvloop"]
