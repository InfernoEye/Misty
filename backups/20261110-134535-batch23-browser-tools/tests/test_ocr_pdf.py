import shutil
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

from app.documents.ocr_pdf import OcrmypdfConverter
from app.documents.reader import DocumentReader


HAS_REPORTLAB = find_spec("reportlab") is not None
HAS_TESSERACT = shutil.which("tesseract") is not None
OCR_BIN = Path(__file__).resolve().parents[1] / "data" / "bin" / "ocrmypdf"
HAS_OCRMYPDF = OCR_BIN.is_file()


def make_scanned_pdf(path):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    image = Image.new("RGB", (1600, 400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=64)
    draw.text((60, 150), "Misty OCR marker 42", fill="black", font=font)
    png = Path(path).with_suffix(".png")
    image.save(png)

    pdf = canvas.Canvas(str(path), pagesize=(800, 400))
    pdf.drawImage(ImageReader(str(png)), 0, 0, width=800, height=400)
    pdf.save()


@unittest.skipUnless(
    HAS_REPORTLAB and HAS_TESSERACT and HAS_OCRMYPDF,
    "OCRmyPDF/Tesseract/reportlab unavailable",
)
class OcrFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_image_only_pdf_uses_local_ocr(self):
        target = self.root / "scan.pdf"
        make_scanned_pdf(target)

        reader = DocumentReader(
            self.root,
            ocr_converter=OcrmypdfConverter(executable=str(OCR_BIN)),
        )
        result = reader.read("scan.pdf")

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.format, "pdf")
        self.assertIn("Misty", result.text)
        self.assertIn("42", result.text)

    def test_unavailable_ocr_is_not_an_error(self):
        target = self.root / "scan.pdf"
        make_scanned_pdf(target)

        reader = DocumentReader(
            self.root,
            ocr_converter=OcrmypdfConverter(executable=""),
        )
        result = reader.read("scan.pdf")

        self.assertTrue(result.ok, result.message)
        self.assertNotIn("Misty", result.text)


if __name__ == "__main__":
    unittest.main()
