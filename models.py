"""
Hugging Face model integration for OCR and content structuring.
Uses small models suitable for Cloud Run (CPU or T4 GPU).
"""
import base64
import io
import logging
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

# Global model cache
_model_cache = {}


def get_ocr_model():
    """
    Donut OCR model — small enough for CPU inference on Cloud Run.
    Cached globally to avoid reloading on every request.
    """
    import torch
    from transformers import AutoProcessor, VisionEncoderDecoderModel

    model_id = "naver-clova-ix/donut-base-finetuned-docvqa"
    if "donut_processor" not in _model_cache:
        logger.info("Loading Donut OCR model...")
        _model_cache["donut_processor"] = AutoProcessor.from_pretrained(model_id)
        _model_cache["donut_model"] = VisionEncoderDecoderModel.from_pretrained(
            model_id,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        )
        if torch.cuda.is_available():
            _model_cache["donut_model"] = _model_cache["donut_model"].to("cuda")
        logger.info("Donut OCR model loaded")
    return _model_cache["donut_processor"], _model_cache["donut_model"]


def ocr_image(image_bytes: bytes) -> str:
    """OCR a single image using Donut."""
    processor, model = get_ocr_model()
    import torch

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    pixel_values = processor(images=img, return_tensors="pt").pixel_values
    if torch.cuda.is_available():
        pixel_values = pixel_values.to("cuda")
    outputs = model.generate(pixel_values, max_length=512)
    return processor.batch_decode(outputs, skip_special_tokens=True)[0]


def get_summarization_model():
    """
    Small BART model for summarizing/structuring extracted text.
    Falls back to no model if memory is constrained.
    """
    try:
        from transformers import pipeline
        if "summarizer" not in _model_cache:
            model_id = "facebook/bart-large-cnn"
            _model_cache["summarizer"] = pipeline(
                "summarization",
                model=model_id,
                device=-1,  # CPU
            )
        return _model_cache["summarizer"]
    except Exception as e:
        logger.warning(f"Could not load summarization model: {e}")
        return None


def structure_article(text: str, title: str = "") -> dict:
    """Optionally clean and structure raw extracted text."""
    return {
        "title": title,
        "body": text,
        "length": len(text),
        "structured": False,
    }
