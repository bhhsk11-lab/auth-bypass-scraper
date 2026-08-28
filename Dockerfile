FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System dependencies required by the application
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

# Install Python dependencies
COPY requirements.txt .

RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create a non-root user.
# UID 1000 is already occupied in the Playwright image,
# therefore use UID 1001.
RUN useradd -m -u 1001 -s /bin/bash appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --loop uvloop"]
