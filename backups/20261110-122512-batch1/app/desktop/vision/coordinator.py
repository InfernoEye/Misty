"""Read-only coordination of screenshot capture, OCR, and visual reasoning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.desktop.vision.capture import CaptureResult, CaptureService
from app.desktop.vision.ocr import OCRResult, OCRService
from app.desktop.vision.vlm import VisionLanguageResult, VisionLanguageService


DEFAULT_PROMPT = (
    "Describe the visible desktop interface. Identify the main application, "
    "important visible controls, dialogs, and any apparent errors. "
    "Do not assume that anything not clearly visible is present."
)


@dataclass(frozen=True)
class DesktopVisionResult:
    capture: CaptureResult
    ocr: OCRResult
    visual: VisionLanguageResult | None

    @property
    def image_size(self) -> tuple[int, int]:
        return self.capture.width, self.capture.height

    @property
    def captured_at(self) -> str:
        return self.capture.captured_at


class DesktopVision:
    """Coordinates read-only visual inspection; never interacts with the UI."""

    def __init__(
        self,
        capture_service: CaptureService,
        ocr_service: OCRService,
        vlm_service: VisionLanguageService,
    ) -> None:
        self.capture_service = capture_service
        self.ocr_service = ocr_service
        self.vlm_service = vlm_service

    def inspect(
        self,
        prompt: str | None = None,
        mode: str = "desktop",
        screen_name: str | None = None,
        include_visual: bool = True,
    ) -> DesktopVisionResult:
        capture = self.capture_service.capture(
            mode=mode,
            screen_name=screen_name,
        )

        ocr = self.ocr_service.recognize(capture.image)

        visual: VisionLanguageResult | None = None
        if include_visual:
            question = prompt if prompt is not None else DEFAULT_PROMPT
            visual = self.vlm_service.analyze(capture.image, question)

        return DesktopVisionResult(
            capture=capture,
            ocr=ocr,
            visual=visual,
        )
