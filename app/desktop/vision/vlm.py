"""On-demand, fully local image understanding through llama.cpp."""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class VisionLanguageError(RuntimeError):
    """Raised when local visual inference fails or violates its limits."""


@dataclass(frozen=True)
class VisionLanguageResult:
    text: str
    elapsed_seconds: float
    image_size: tuple[int, int]
    model: str


class VisionLanguageService:
    """Runs Qwen3-VL locally for one image and question at a time."""

    def __init__(
        self,
        cli_path: str | Path = PROJECT_ROOT / "third_party/llama.cpp/build/bin/llama-mtmd-cli",
        model_path: str | Path = PROJECT_ROOT / "models/vision/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf",
        projector_path: str | Path = PROJECT_ROOT / "models/vision/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf",
        device: str = "Vulkan0",
        gpu_layers: int = 99,
        context_size: int = 16384,
        image_min_tokens: int = 1024,
        timeout: float = 180.0,
        max_pixels: int = 50_000_000,
        max_prompt_chars: int = 4000,
        max_output_chars: int = 100_000,
        before_analyze=None,
    ) -> None:
        if not device or len(device) > 100:
            raise ValueError("Invalid inference device")
        if not 0 <= gpu_layers <= 1000:
            raise ValueError("Invalid GPU layer count")
        if not 512 <= context_size <= 131072:
            raise ValueError("Context size is outside the supported safety bounds")
        if not 256 <= image_min_tokens <= 16384:
            raise ValueError("Image token setting is outside the supported bounds")
        if timeout <= 0 or max_pixels <= 0:
            raise ValueError("Timeout and pixel limit must be positive")
        if max_prompt_chars < 1 or max_output_chars < 1:
            raise ValueError("Text limits must be positive")

        self.cli_path = Path(cli_path)
        self.model_path = Path(model_path)
        self.projector_path = Path(projector_path)
        self.device = device
        self.gpu_layers = gpu_layers
        self.context_size = context_size
        self.image_min_tokens = image_min_tokens
        self.timeout = timeout
        self.max_pixels = max_pixels
        self.max_prompt_chars = max_prompt_chars
        self.max_output_chars = max_output_chars
        #: Optional zero-argument callable run just before the model starts.
        #: The app wires it to release the reasoning model (Qwen) so the
        #: vision model does not compete with it for GPU memory.
        self.before_analyze = before_analyze

    def set_before_analyze(self, callback):
        """Install (or clear, with None) the pre-inference release hook."""
        self.before_analyze = callback

    def _release_reasoning_model(self) -> None:
        callback = self.before_analyze
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - hook must never break vision
            print(f"[Misty] vision pre-hook failed: {exc}", flush=True)

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

        for path, label in (
            (self.cli_path, "Multimodal executable"),
            (self.model_path, "Vision model"),
            (self.projector_path, "Vision projector"),
        ):
            if not path.is_file():
                raise VisionLanguageError(f"{label} not found: {path}")

        command = [
            str(self.cli_path),
            "-m", str(self.model_path),
            "--mmproj", str(self.projector_path),
            "--image", "",  # Replaced with the private temporary image path below.
            "--device", self.device,
            "-ngl", str(self.gpu_layers),
            "-c", str(self.context_size),
            "-n", str(max_tokens),
            "--image-min-tokens", str(self.image_min_tokens),
            "-p", prompt.strip(),
        ]

        self._release_reasoning_model()
        started = time.monotonic()

        try:
            with tempfile.TemporaryDirectory(prefix="misty-vision-") as directory:
                image_path = Path(directory) / "input.png"
                image.convert("RGB").save(image_path, format="PNG")
                command[command.index("--image") + 1] = str(image_path)

                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise VisionLanguageError("Visual inference timed out") from exc
        except OSError as exc:
            raise VisionLanguageError("Could not start the local vision model") from exc

        elapsed = time.monotonic() - started

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:1000]
            raise VisionLanguageError(
                f"Visual inference failed with exit code {result.returncode}: {detail}"
            )

        text = result.stdout.strip()
        if len(text) > self.max_output_chars:
            raise VisionLanguageError("Vision model output exceeds the configured limit")
        if not text:
            raise VisionLanguageError("Vision model returned an empty response")

        return VisionLanguageResult(
            text=text,
            elapsed_seconds=elapsed,
            image_size=(width, height),
            model=self.model_path.name,
        )
