FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ============================================================
# System dependencies
# ============================================================

USER root

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

# ============================================================
# Python dependencies
# ============================================================

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r /app/requirements.txt

# ============================================================
# Application
# ============================================================

COPY . /app

# Give the existing Playwright user access to the application.
RUN chown -R pwuser:pwuser /app

# ============================================================
# Run as Playwright's existing non-root user
# ============================================================

USER pwuser

# ============================================================
# Cloud Run
# ============================================================

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --loop uvloop"]
