"""
PDF extraction pipeline with multiple backends.
Handles both text-native PDFs and scanned/image-based PDFs.
"""
import base64
import io
import logging
from io import BytesIO
from typing import Any

from pypdf import PdfReader
from PIL import Image

logger = logging.getLogger(__name__)


def extract_text_pypdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF using pypdf (fast, native text)."""
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text_pdfplumber(pdf_bytes: bytes) -> str:
    """Fallback PDF text extraction with better table support."""
    import pdfplumber
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        return "\n\n".join(p.extract_text() or "" for p in pdf.pages)


def render_pdf_to_images(pdf_bytes: bytes, max_pages: int = 30,
                         dpi: int = 150) -> list[bytes]:
    """Render PDF pages to PNG bytes (for OCR) via poppler/pdf2image."""
    from pdf2image import convert_from_bytes
    pages = convert_from_bytes(pdf_bytes, dpi=dpi, fmt="png",
                               first_page=1, last_page=max_pages)
    out = []
    for page in pages:
        buf = BytesIO()
        page.save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


def ocr_images(images: list[bytes], max_pages: int = 10) -> str:
    """OCR rendered page images with Tesseract (installed via apt in the
    Dockerfile specifically for this)."""
    import pytesseract
    texts = []
    for img_bytes in images[:max_pages]:
        try:
            texts.append(pytesseract.image_to_string(Image.open(BytesIO(img_bytes))))
        except Exception as e:
            logger.warning(f"OCR failed on one page: {e}")
    return "\n\n".join(t for t in texts if t and t.strip())


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
        result["pages"] = len(PdfReader(BytesIO(pdf_bytes)).pages)
    except Exception:
        pass

    # Strategy 1: Native text extraction
    try:
        text = extract_text_pypdf(pdf_bytes).strip()
    except Exception as e:
        logger.warning(f"pypdf extraction failed: {e}")
        text = ""

    if len(text) > 100:
        result["text"] = text
        result["method"] = "pypdf"
        return result

    # Strategy 2: pdfplumber fallback
    try:
        text = extract_text_pdfplumber(pdf_bytes).strip()
    except Exception as e:
        logger.warning(f"pdfplumber extraction failed: {e}")
        text = ""
    if len(text) > 100:
        result["text"] = text
        result["method"] = "pdfplumber"
        return result

    # Strategy 3: Scanned PDF — render pages and OCR them
    result["scanned"] = True
    try:
        images = render_pdf_to_images(pdf_bytes)
        result["images"] = [base64.b64encode(img).decode() for img in images]
        text = ocr_images(images).strip()
        result["text"] = text
        result["method"] = "ocr" if text else "scanned-no-text"
        logger.info(f"Scanned PDF: {len(images)} pages rendered, "
                    f"{'OCR succeeded' if text else 'OCR produced no text'}")
    except Exception as e:
        logger.error(f"Scanned-PDF rendering/OCR failed: {e}")
        result["method"] = "scanned-failed"

    return result


async def ocr_images_with_hf(images_b64: list[str]) -> str:
    """
    Run OCR on scanned PDF pages using Hugging Face model.
    Heavy — runs on GPU if available, else CPU inference. Not called
    anywhere in the current pipeline (extract_pdf uses Tesseract above
    instead); left here in case you want a swap-in later. Requires
    torch/transformers, which are NOT in requirements.txt — don't call
    this without adding them first.
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
