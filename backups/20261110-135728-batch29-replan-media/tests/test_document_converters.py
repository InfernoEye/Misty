import shutil
import subprocess
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

from app.documents.converters import ExternalConverter
from app.documents.reader import DocumentReader


HAS_REPORTLAB = find_spec("reportlab") is not None
HAS_PDFTOTEXT = shutil.which("pdftotext") is not None
HAS_PANDOC = shutil.which("pandoc") is not None


def make_pdf(path, text="Misty PDF test"):
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, text)
    pdf.save()


class ExternalDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @unittest.skipUnless(
        HAS_REPORTLAB and HAS_PDFTOTEXT, "reportlab/pdftotext unavailable"
    )
    def test_reads_pdf_through_pdftotext(self):
        target = self.root / "sample.pdf"
        make_pdf(target, "Misty PDF marker")

        result = DocumentReader(self.root).read("sample.pdf")

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.format, "pdf")
        self.assertIn("Misty PDF marker", result.text)

    @unittest.skipUnless(HAS_PANDOC, "pandoc unavailable")
    def test_reads_docx_through_pandoc(self):
        source = self.root / "source.md"
        source.write_text("# Heading\n\nDOCX marker text.\n", encoding="utf-8")
        target = self.root / "sample.docx"
        subprocess.run(
            ["pandoc", "-f", "markdown", "-t", "docx", "-o",
             str(target), str(source)],
            check=True,
            timeout=20,
        )

        result = DocumentReader(self.root).read("sample.docx")

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.format, "docx")
        self.assertIn("DOCX marker text.", result.text)

    @unittest.skipUnless(HAS_PANDOC, "pandoc unavailable")
    def test_reads_odt_through_pandoc(self):
        source = self.root / "source.md"
        source.write_text("ODT marker text.\n", encoding="utf-8")
        target = self.root / "sample.odt"
        subprocess.run(
            ["pandoc", "-f", "markdown", "-t", "odt", "-o",
             str(target), str(source)],
            check=True,
            timeout=20,
        )

        result = DocumentReader(self.root).read("sample.odt")

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.format, "odt")
        self.assertIn("ODT marker text.", result.text)

    @unittest.skipUnless(
        HAS_REPORTLAB and HAS_PDFTOTEXT, "reportlab/pdftotext unavailable"
    )
    def test_pdf_output_is_truncated_to_reader_limit(self):
        target = self.root / "sample.pdf"
        make_pdf(target, "0123456789")

        result = DocumentReader(self.root, max_chars=5).read("sample.pdf")

        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(result.text), 5)
        self.assertTrue(result.text_truncated)

    @unittest.skipUnless(
        HAS_REPORTLAB and HAS_PDFTOTEXT, "reportlab/pdftotext unavailable"
    )
    def test_pdf_larger_than_limit_is_refused(self):
        target = self.root / "sample.pdf"
        make_pdf(target, "oversized marker")

        result = DocumentReader(self.root, max_bytes=32).read("sample.pdf")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "file_too_large")

    def test_missing_converter_is_reported_safely(self):
        target = self.root / "sample.pdf"
        target.write_bytes(b"%PDF-1.4\n")
        reader = DocumentReader(
            self.root,
            converter=ExternalConverter(pdftotext="", pandoc=""),
        )

        result = reader.read("sample.pdf")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "conversion_failed")
        self.assertNotIn(str(self.root), result.message)

    @unittest.skipUnless(
        HAS_REPORTLAB and HAS_PDFTOTEXT, "reportlab/pdftotext unavailable"
    )
    def test_dash_prefixed_filename_is_not_an_option(self):
        target = self.root / "-dash.pdf"
        make_pdf(target, "Dash marker")

        result = DocumentReader(self.root).read(target.name)

        self.assertTrue(result.ok, result.message)
        self.assertIn("Dash marker", result.text)

    @unittest.skipUnless(
        HAS_REPORTLAB and HAS_PDFTOTEXT, "reportlab/pdftotext unavailable"
    )
    def test_metacharacters_in_filename_are_passed_as_one_argument(self):
        target = self.root / "evil; touch pwned.pdf"
        make_pdf(target, "Safe argument marker")

        result = DocumentReader(self.root).read(target.name)

        self.assertTrue(result.ok, result.message)
        self.assertIn("Safe argument marker", result.text)
        self.assertFalse((self.root / "pwned").exists())


if __name__ == "__main__":
    unittest.main()
