"""
OCR helpers.

MarkItDown reads a PDF's existing text layer. A scanned, image only PDF has
no text layer, so it converts to nothing. These helpers detect that case and
add a text layer with OCR before MarkItDown runs. Loose image files are OCRed
directly, since MarkItDown does not OCR images on its own.
"""

import io
import os
import tempfile
import logging

import pypdf
import ocrmypdf
import pytesseract
from PIL import Image

log = logging.getLogger("markitdown-api.ocr")

# If a PDF yields fewer than this many characters of real text, treat it as
# scanned and OCR it.
OCR_TEXT_THRESHOLD = int(os.getenv("OCR_TEXT_THRESHOLD", "32"))

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif"}


def pdf_text_chars(data: bytes) -> int:
    """Count characters of extractable text in a PDF. Returns 0 on any error."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = "".join((page.extract_text() or "") for page in reader.pages)
        return len(text.strip())
    except Exception:
        return 0


def maybe_ocr_pdf(data: bytes, mode: str) -> bytes:
    """
    Return PDF bytes ready for MarkItDown.

    mode = "off"   never OCR, return input unchanged
    mode = "force" OCR every page, rasterizing existing text
    mode = "auto"  OCR only if the PDF has little or no text layer
    """
    if mode == "off":
        return data

    if mode == "auto" and pdf_text_chars(data) >= OCR_TEXT_THRESHOLD:
        return data  # already has a usable text layer

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.pdf")
        out = os.path.join(d, "out.pdf")
        with open(src, "wb") as f:
            f.write(data)
        ocrmypdf.ocr(
            src,
            out,
            force_ocr=(mode == "force"),
            skip_text=(mode != "force"),
            progress_bar=False,
            quiet=True,
	    tesseract_timeout=600,
	    rotate_pages=False,
        )
        with open(out, "rb") as f:
            return f.read()


def ocr_image(data: bytes) -> str:
    """OCR a single image file and return plain text."""
    with Image.open(io.BytesIO(data)) as img:
        return pytesseract.image_to_string(img).strip()
