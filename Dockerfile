# ============================================================
# Stage 1: Build environment
# ============================================================
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble AS base

WORKDIR /app

# System deps: PDF rendering, OCR, image processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    wget \
    curl \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Ensure Playwright Chromium is installed
RUN playwright install chromium --with-deps 2>/dev/null || true
RUN playwright install-deps chromium 2>/dev/null || true

# ============================================================
# Stage 2: Python dependencies
# ============================================================
FROM base AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ============================================================
# Stage 3: Runtime (slim)
# ============================================================
FROM builder AS runtime

WORKDIR /app

# Copy application code
COPY . .

# Non-root user for Cloud Run security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app && \
    chmod -R 755 /app

USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Cloud Run uses PORT env var
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TRANSFORMERS_CACHE=/app/.cache/huggingface
ENV HF_HOME=/app/.cache/huggingface

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--loop", "uvloop"]
