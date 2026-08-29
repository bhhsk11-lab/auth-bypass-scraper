FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# ── System deps for Chromium + PDF/OCR tooling (correct Debian names) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    # chromium runtime libs
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libatspi2.0-0 libx11-6 libxcb1 libxext6 \
    fonts-liberation fonts-unifont fonts-freefont-ttf \
    fonts-noto-color-emoji fonts-wqy-zenhei \
    # pdf / ocr tooling
    poppler-utils tesseract-ocr \
    # misc
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Browser install WITHOUT --with-deps (deps already installed above) ──
RUN playwright install chromium

COPY . .

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
