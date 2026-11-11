"""On-demand local visual intelligence services."""

from app.desktop.vision.capture import CaptureError, CaptureResult, CaptureService
from app.desktop.vision.coordinator import DesktopVision, DesktopVisionResult
from app.desktop.vision.ocr import OCRResult, OCRService, OCRWord
from app.desktop.vision.vlm import (
    VisionLanguageError,
    VisionLanguageResult,
    VisionLanguageService,
)

__all__ = [
    "CaptureError",
    "CaptureResult",
    "CaptureService",
    "DesktopVision",
    "DesktopVisionResult",
    "OCRResult",
    "OCRService",
    "OCRWord",
    "VisionLanguageError",
    "VisionLanguageResult",
    "VisionLanguageService",
]
