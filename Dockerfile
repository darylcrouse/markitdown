FROM python:3.12-slim

# System tools for OCR: tesseract (engine), ghostscript + qpdf (PDF handling).
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        ghostscript \
        qpdf \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. If a parser is exploited, it does not get root.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ocr.py url_fetch.py ./
COPY static/ ./static/

USER appuser
EXPOSE 8000

# One worker keeps the in-process rate limiter accurate.
# Scale by running more replicas behind a proxy, not more workers here.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]