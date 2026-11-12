"""On-demand, fully local image understanding through llama.cpp."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

from app.brain.server_lock import SHARED_SERVER_LOCK


class VisionLanguageError(RuntimeError):
    """Raised when local visual inference fails or violates its limits."""


@dataclass(frozen=True)
class VisionLanguageResult:
    text: str
    elapsed_seconds: float
    image_size: tuple[int, int]
    model: str


class VisionLanguageService:
    """Answers questions about one image via the shared local model server."""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        timeout: float = 180.0,
        max_pixels: int = 50_000_000,
        max_prompt_chars: int = 4000,
        max_output_chars: int = 100_000,
    ) -> None:
        if timeout <= 0 or max_pixels <= 0:
            raise ValueError("Timeout and pixel limit must be positive")
        if max_prompt_chars < 1 or max_output_chars < 1:
            raise ValueError("Text limits must be positive")

        self.base_url = base_url
        self.model_name = model_name
        self.timeout = timeout
        self.max_pixels = max_pixels
        self.max_prompt_chars = max_prompt_chars
        self.max_output_chars = max_output_chars

    def analyze(
        self,
        image: Image.Image,
        prompt: str,
        max_tokens: int = 256,
    ) -> VisionLanguageResult:
        if not isinstance(image, Image.Image):
            raise TypeError("analyze() expects a Pillow image")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("A non-empty prompt is required")
        if len(prompt) > self.max_prompt_chars:
            raise ValueError("Prompt exceeds the configured character limit")
        if not 1 <= max_tokens <= 2048:
            raise ValueError("max_tokens must be between 1 and 2048")

        width, height = image.size
        if width <= 0 or height <= 0 or width * height > self.max_pixels:
            raise ValueError("Image dimensions are invalid or exceed the pixel limit")

        started = time.monotonic()
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        payload = {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt.strip()},
                ],
            }],
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "stream": False,
        }

        text = self._post(payload)
        elapsed = time.monotonic() - started

        if len(text) > self.max_output_chars:
            raise VisionLanguageError("Vision model output exceeds the configured limit")
        if not text:
            raise VisionLanguageError("Vision model returned an empty response")

        return VisionLanguageResult(
            text=text,
            elapsed_seconds=elapsed,
            image_size=(width, height),
            model=self.model_name,
        )

    def _post(self, payload) -> str:
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with SHARED_SERVER_LOCK, urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise VisionLanguageError("Visual inference timed out") from exc
        except urllib.error.HTTPError as exc:
            raise VisionLanguageError(f"Local model server rejected the image: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise VisionLanguageError("Could not reach the local model server") from exc

        choices = body.get("choices") or []
        if not choices:
            raise VisionLanguageError("Local model server returned no choices")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise VisionLanguageError("Local model server returned no text")
        return content.strip()
