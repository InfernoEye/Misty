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
    def frame(self) -> dict[str, Any]:
        """Capture coordinate contract; use frame_id for image-local actions."""
        return self.capture.frame

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
        before_analyze=None,
    ) -> None:
        self.capture_service = capture_service
        self.ocr_service = ocr_service
        self.vlm_service = vlm_service
        #: Optional zero-argument hook run immediately before the VLM starts
        #: (after capture/OCR, right before the model loads), so the app can
        #: release the reasoning model first instead of running both models.
        self.before_analyze = before_analyze

    def set_before_analyze(self, callback) -> None:
        """Install (or clear, with None) the pre-VLM release hook."""
        self.before_analyze = callback
        # Keep a service-level hook (e.g. VisionLanguageService) in step.
        setter = getattr(self.vlm_service, "set_before_analyze", None)
        if callable(setter):
            try:
                setter(callback)
            except Exception:
                pass

    def _release_reasoning_model(self) -> None:
        callback = self.before_analyze
        if callback is not None:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - never break inspection
                print(f"[Misty] Vision pre-hook failed: {exc}", flush=True)
            return
        # Fall back to the VLM's own hook when only that one was configured.
        releaser = getattr(self.vlm_service, "_release_reasoning_model", None)
        if callable(releaser):
            try:
                releaser()
            except Exception:
                pass

    def inspect(
        self,
        prompt: str | None = None,
        mode: str = "desktop",
        screen_name: str | None = None,
        include_visual: bool = True,
        window_id: str | None = None,
    ) -> DesktopVisionResult:
        capture = self.capture_service.capture(
            mode=mode,
            screen_name=screen_name,
            window_id=window_id,
        )

        if include_visual:
            # Release the reasoning model before OCR: it is about to be used
            # by a different model for the next seconds.
            self._release_reasoning_model()

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
