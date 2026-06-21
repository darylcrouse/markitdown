"""
MarkItDown conversion API.

An authenticated HTTP service that converts documents to Markdown using
Microsoft MarkItDown, with an OCR pre-step for scanned PDFs and images and a
safe URL fetch endpoint. Built to sit behind TLS and an API key so it can be
called from a case management application.
"""

import os
import io
import time
import asyncio
import secrets
import logging
import threading
from pathlib import Path
from collections import defaultdict, deque

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from markitdown import MarkItDown, StreamInfo

import ocr
import url_fetch

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
API_KEYS = {
    k.strip() for k in os.getenv("MARKITDOWN_API_KEYS", "").split(",") if k.strip()
}
MAX_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
CONVERT_TIMEOUT = int(os.getenv("CONVERT_TIMEOUT_SECONDS", "120"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
]
ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv",
    ".html", ".htm", ".txt", ".md", ".json", ".xml", ".rtf", ".epub",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".zip",
}
OCR_MODES = {"auto", "force", "off"}

# Optional: set this secret to pre-fill the API key in the UI so users
# do not have to type it. Leave blank to require manual entry.
UI_API_KEY = os.getenv("UI_API_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("markitdown-api")

_converter = MarkItDown(enable_plugins=False)
app = FastAPI(title="MarkItDown API", version="1.2.0")

if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["POST", "GET"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

# Serve static assets (CSS overrides, future additions)
_static = Path(__file__).parent / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")

# ----------------------------------------------------------------------
# Rate limiting and auth
# ----------------------------------------------------------------------
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def _rate_ok(key: str) -> bool:
    now = time.time()
    with _hits_lock:
        dq = _hits[key]
        while dq and dq[0] <= now - 60.0:
            dq.popleft()
        if len(dq) >= RATE_LIMIT:
            return False
        dq.append(now)
        return True


def _check_key(provided: str | None) -> str:
    if not API_KEYS:
        raise HTTPException(503, "Server has no API keys configured.")
    if not provided:
        raise HTTPException(401, "Missing X-API-Key header.")
    for valid in API_KEYS:
        if secrets.compare_digest(provided, valid):
            return provided
    raise HTTPException(401, "Invalid API key.")


def _ext(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


# ----------------------------------------------------------------------
# Shared conversion path
# ----------------------------------------------------------------------
def _to_markdown(data: bytes, ext: str, mimetype: str | None,
                 filename: str | None, ocr_mode: str):
    if ext in ocr.IMAGE_EXTS and ocr_mode != "off":
        text = ocr.ocr_image(data)
        return text, filename
    if ext == ".pdf":
        data = ocr.maybe_ocr_pdf(data, ocr_mode)
    info = StreamInfo(extension=ext or None, mimetype=mimetype, filename=filename)
    result = _converter.convert_stream(io.BytesIO(data), stream_info=info)
    return result.markdown or "", result.title


async def _run_conversion(data, ext, mimetype, filename, ocr_mode):
    if ocr_mode not in OCR_MODES:
        raise HTTPException(400, f"ocr must be one of {sorted(OCR_MODES)}.")
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file type: {ext}")
    loop = asyncio.get_running_loop()
    try:
        markdown, title = await asyncio.wait_for(
            loop.run_in_executor(
                None, _to_markdown, data, ext, mimetype, filename, ocr_mode
            ),
            timeout=CONVERT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning("conversion timed out for %s", filename)
        raise HTTPException(504, "Conversion timed out.")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("conversion failed")
        raise HTTPException(422, "Could not convert this file.") from e
    return JSONResponse(
        {"filename": filename, "title": title, "chars": len(markdown),
         "markdown": markdown}
    )


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Serve the web UI."""
    index_path = _static / "index.html"
    if not index_path.exists():
        return HTMLResponse(
            "<h2>UI not found</h2><p>static/index.html is missing from the container. "
            "Check that the Dockerfile copies the static/ directory.</p>",
            status_code=404,
        )
    html = index_path.read_text(encoding="utf-8")
    if UI_API_KEY:
        hint = f'<meta name="api-key-hint" content="{UI_API_KEY}" />'
        html = html.replace("</head>", hint + "\n</head>", 1)
    return HTMLResponse(html)


@app.get("/health")
def health():
    return {"status": "ok", "service": "markitdown-api", "version": "1.2.0"}


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    ocr_mode: str = Form("auto", alias="ocr"),
    x_api_key: str | None = Header(default=None),
):
    key = _check_key(x_api_key)
    if not _rate_ok(key):
        raise HTTPException(429, "Rate limit exceeded. Slow down.")
    ext = _ext(file.filename)
    buf = io.BytesIO()
    read = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        read += len(chunk)
        if read > MAX_BYTES:
            raise HTTPException(413, f"File exceeds {MAX_BYTES} bytes.")
        buf.write(chunk)
    return await _run_conversion(
        buf.getvalue(), ext, file.content_type, file.filename, ocr_mode
    )


class UrlRequest(BaseModel):
    url: str
    ocr: str = "auto"


@app.post("/convert/url")
async def convert_url(
    body: UrlRequest,
    x_api_key: str | None = Header(default=None),
):
    key = _check_key(x_api_key)
    if not _rate_ok(key):
        raise HTTPException(429, "Rate limit exceeded. Slow down.")
    try:
        data, filename, content_type = url_fetch.fetch(body.url)
    except url_fetch.FetchError as e:
        raise HTTPException(400, str(e)) from e
    return await _run_conversion(
        data, _ext(filename), content_type, filename, body.ocr
    )