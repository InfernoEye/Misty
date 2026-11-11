"""Local OCR using the installed Tesseract executable."""

from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass
from typing import Any

from PIL import Image


@dataclass(frozen=True)
class OCRWord:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class OCRResult:
    text: str
    words: tuple[OCRWord, ...]
    language: str
    image_size: tuple[int, int]


class OCRService:
    """Runs Tesseract locally; never uploads or persists screenshot content."""

    def __init__(
        self,
        tesseract_path: str = "/usr/bin/tesseract",
        language: str = "eng+pol",
        page_segmentation_mode: int = 6,
        timeout: float = 30.0,
        max_pixels: int = 50_000_000,
        max_output_chars: int = 2_000_000,
    ) -> None:
        if not language or any(char.isspace() for char in language):
            raise ValueError("Invalid Tesseract language specification")
        if not 0 <= page_segmentation_mode <= 13:
            raise ValueError("Invalid Tesseract page segmentation mode")

        self.tesseract_path = tesseract_path
        self.language = language
        self.page_segmentation_mode = page_segmentation_mode
        self.timeout = timeout
        self.max_pixels = max_pixels
        self.max_output_chars = max_output_chars

    def recognize(self, image: Image.Image) -> OCRResult:
        if not isinstance(image, Image.Image):
            raise TypeError("recognize() expects a Pillow image")

        width, height = image.size
        if width <= 0 or height <= 0 or width * height > self.max_pixels:
            raise ValueError("Image dimensions are invalid or exceed the pixel limit")

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        image_bytes = buffer.getvalue()

        command = [
            self.tesseract_path,
            "stdin",
            "stdout",
            "-l",
            self.language,
            "--psm",
            str(self.page_segmentation_mode),
            "tsv",
        ]

        try:
            result = subprocess.run(
                command,
                input=image_bytes,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Local OCR timed out") from exc
        except OSError as exc:
            raise RuntimeError("Could not start Tesseract") from exc

        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:1000]
            raise RuntimeError(f"Tesseract failed: {detail}")

        output = result.stdout.decode("utf-8", errors="replace")
        if len(output) > self.max_output_chars:
            raise RuntimeError("OCR output exceeds the configured size limit")

        words: list[OCRWord] = []
        lines: dict[tuple[str, str, str, str], list[str]] = {}

        reader = csv.DictReader(io.StringIO(output), delimiter="\t")
        for row in reader:
            if row.get("level") != "5":
                continue

            text = (row.get("text") or "").strip()
            if not text:
                continue

            try:
                confidence = float(row["conf"])
                left = int(row["left"])
                top = int(row["top"])
                word_width = int(row["width"])
                word_height = int(row["height"])
            except (KeyError, TypeError, ValueError):
                continue

            words.append(
                OCRWord(
                    text=text,
                    confidence=confidence,
                    left=left,
                    top=top,
                    width=word_width,
                    height=word_height,
                )
            )

            line_key = (
                row.get("page_num", ""),
                row.get("block_num", ""),
                row.get("par_num", ""),
                row.get("line_num", ""),
            )
            lines.setdefault(line_key, []).append(text)

        text = "\n".join(" ".join(parts) for parts in lines.values())

        return OCRResult(
            text=text,
            words=tuple(words),
            language=self.language,
            image_size=(width, height),
        )
