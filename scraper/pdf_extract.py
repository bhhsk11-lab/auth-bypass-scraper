"""
PDF extraction pipeline with multiple backends.
Handles both text-native PDFs and scanned/image-based PDFs.
"""
import base64
import io
import logging
from io import BytesIO
from typing import Any

import fitz  # PyMuPDF
from PIL import Image

logger = logging.getLogger(__name__)


def extract_text_pymupdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF using PyMuPDF (fast, native text)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        text_parts = []
        for page in doc:
            text = page.get_text("text")
            text_parts.append(text)
        return "\n\n".join(text_parts)
    finally:
        doc.close()


def extract_text_pdfplumber(pdf_bytes: bytes) -> str:
    """Fallback PDF text extraction with better table support."""
    import pdfplumber
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        return "\n\n".join(p.extract_text() or "" for p in pdf.pages)


def render_pdf_to_images(pdf_bytes: bytes, max_pages: int = 30,
                         dpi: int = 150) -> list[bytes]:
    """Render PDF pages to PNG images for OCR or VLM processing."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        images = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("png")
            images.append(img_bytes)
        return images
    finally:
        doc.close()


def extract_pdf(pdf_bytes: bytes) -> dict:
    """
    Multi-strategy PDF extraction.
    
    Returns:
        text: extracted text
        pages: page count
        scanned: True if PDF appears to be scanned images
        images: list of base64 PNGs (for scanned PDFs)
        method: extraction method used
    """
    result = {
        "pages": 0,
        "text": "",
        "scanned": False,
        "images": [],
        "method": "unknown",
        "title": "",
    }

    # Check page count
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        result["pages"] = len(doc)
        doc.close()
    except Exception:
        pass

    # Strategy 1: Native text extraction
    text = extract_text_pymupdf(pdf_bytes)
    text = text.strip()

    if len(text) > 100:
        result["text"] = text
        result["method"] = "pymupdf"
        return result

    # Strategy 2: pdfplumber fallback
    text = extract_text_pdfplumber(pdf_bytes).strip()
    if len(text) > 100:
        result["text"] = text
        result["method"] = "pdfplumber"
        return result

    # Strategy 3: Scanned PDF — render pages for OCR
    result["scanned"] = True
    result["method"] = "scanned"
    images = render_pdf_to_images(pdf_bytes)
    result["images"] = [base64.b64encode(img).decode() for img in images]
    logger.info(f"Scanned PDF detected: {len(images)} pages rendered for OCR")

    return result


async def ocr_images_with_hf(images_b64: list[str]) -> str:
    """
    Run OCR on scanned PDF pages using Hugging Face model.
    Heavy — runs on GPU if available, else CPU inference.
    """
    try:
        from transformers import AutoProcessor, VisionEncoderDecoderModel
        import torch

        model_id = "naver-clova-ix/donut-base-finetuned-docvqa"
        processor = AutoProcessor.from_pretrained(model_id)
        model = VisionEncoderDecoderModel.from_pretrained(model_id)

        if torch.cuda.is_available():
            model = model.to("cuda")

        texts = []
        for b64_img in images_b64[:10]:  # Max 10 pages for speed
            img = Image.open(io.BytesIO(base64.b64decode(b64_img)))
            pixel_values = processor(images=img, return_tensors="pt").pixel_values
            if torch.cuda.is_available():
                pixel_values = pixel_values.to("cuda")
            outputs = model.generate(pixel_values, max_length=512)
            text = processor.batch_decode(outputs, skip_special_tokens=True)[0]
            texts.append(text)

        return "\n\n".join(texts)

    except Exception as e:
        logger.error(f"HF OCR failed: {e}")
        return ""
