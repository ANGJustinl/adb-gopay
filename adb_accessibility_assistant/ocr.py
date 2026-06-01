from __future__ import annotations

import io
from typing import Protocol

from PIL import Image

from .models import OCRTextBlock


class OCRUnavailableError(RuntimeError):
    """Raised when no OCR backend can be loaded."""


class OCREngine(Protocol):
    def recognize(self, image_bytes: bytes) -> list[OCRTextBlock]:
        ...


class NullOCREngine:
    """Fallback OCR engine that returns no text blocks."""

    def recognize(self, image_bytes: bytes) -> list[OCRTextBlock]:
        return []


class RapidOCREngine:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ModuleNotFoundError as exc:
            raise OCRUnavailableError("rapidocr-onnxruntime is not installed") from exc

        self._rapid_ocr = RapidOCR()

    def recognize(self, image_bytes: bytes) -> list[OCRTextBlock]:
        import numpy as np

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        result, _ = self._rapid_ocr(np.array(image))
        blocks: list[OCRTextBlock] = []
        for box, text, score in result or []:
            normalized_box = [(int(point[0]), int(point[1])) for point in box]
            blocks.append(OCRTextBlock(text=str(text), confidence=float(score), box=normalized_box))
        return blocks


class TesseractEngine:
    def __init__(self) -> None:
        try:
            import pytesseract
        except ModuleNotFoundError as exc:
            raise OCRUnavailableError("pytesseract is not installed") from exc
        self._pytesseract = pytesseract

    def recognize(self, image_bytes: bytes) -> list[OCRTextBlock]:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        data = self._pytesseract.image_to_data(image, output_type=self._pytesseract.Output.DICT)
        blocks: list[OCRTextBlock] = []
        count = len(data["text"])
        for index in range(count):
            text = str(data["text"][index]).strip()
            if not text:
                continue
            try:
                confidence = max(0.0, float(data["conf"][index])) / 100.0
            except (TypeError, ValueError):
                confidence = 0.0
            left = int(data["left"][index])
            top = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            box = [
                (left, top),
                (left + width, top),
                (left + width, top + height),
                (left, top + height),
            ]
            blocks.append(OCRTextBlock(text=text, confidence=confidence, box=box))
        return blocks


def available_backends() -> dict[str, bool]:
    results: dict[str, bool] = {}
    try:
        import rapidocr_onnxruntime  # noqa: F401

        results["rapidocr"] = True
    except ModuleNotFoundError:
        results["rapidocr"] = False

    try:
        import pytesseract  # noqa: F401

        results["tesseract"] = True
    except ModuleNotFoundError:
        results["tesseract"] = False
    return results


def create_ocr_engine(preferred: str = "rapidocr") -> OCREngine:
    preferred = preferred.strip().casefold()
    last_error: Exception | None = None

    candidates = ["rapidocr", "tesseract"]
    if preferred in candidates:
        candidates.remove(preferred)
        candidates.insert(0, preferred)

    for candidate in candidates:
        try:
            if candidate == "rapidocr":
                return RapidOCREngine()
            if candidate == "tesseract":
                return TesseractEngine()
        except OCRUnavailableError as exc:
            last_error = exc

    raise OCRUnavailableError("No OCR backend is available. Install rapidocr-onnxruntime or pytesseract.") from last_error
