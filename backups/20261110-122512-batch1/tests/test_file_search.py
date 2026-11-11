import tempfile
import unittest
from pathlib import Path

from app.actions.file_search import FileSearchService


class TestFileSearchService(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.service = FileSearchService(
            home=self.home, max_seconds=3, max_directories=500
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_finds_files_recursively_case_insensitively(self):
        folder = self.home / "Documents" / "Projects"
        folder.mkdir(parents=True)
        target = folder / "Quarterly-REPORT.pdf"
        target.write_text("not read by search", encoding="utf-8")

        result = self.service.search("report")
        self.assertTrue(result["success"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["path"], str(target))
        self.assertEqual(result["results"][0]["type"], "file")

    def test_searches_common_folders_and_home(self):
        downloads = self.home / "Pobrane"
        downloads.mkdir()
        (downloads / "report-download.txt").touch()
        (self.home / "other-report.txt").touch()

        result = self.service.search("report")
        paths = [item["path"] for item in result["results"]]
        self.assertIn(str(downloads / "report-download.txt"), paths)
        self.assertIn(str(self.home / "other-report.txt"), paths)

    def test_finds_matching_directory_names(self):
        folder = self.home / "Documents" / "Project-Archive"
        folder.mkdir(parents=True)

        result = self.service.search("archive")
        self.assertTrue(any(
            item["path"] == str(folder) and item["type"] == "directory"
            for item in result["results"]
        ))

    def test_skips_symlinks(self):
        outside = Path(self.temp.name).parent / (
            Path(self.temp.name).name + "-outside-secret.txt"
        )
        outside.write_text("secret", encoding="utf-8")
        try:
            (self.home / "linked-secret.txt").symlink_to(outside)
            result = self.service.search("secret")
            self.assertEqual(result["results"], [])
        finally:
            outside.unlink(missing_ok=True)

    def test_skips_cache_and_dependency_directories(self):
        (self.home / ".cache").mkdir()
        (self.home / ".cache" / "hidden-report.txt").touch()
        (self.home / "node_modules").mkdir()
        (self.home / "node_modules" / "hidden-report.txt").touch()

        result = self.service.search("report")
        self.assertEqual(result["results"], [])

    def test_enforces_result_limit(self):
        for index in range(4):
            (self.home / f"report-{index}.txt").touch()

        result = self.service.search("report", limit=2)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(result["truncated"])

    def test_rejects_invalid_arguments(self):
        self.assertFalse(self.service.search(" ")["success"])
        self.assertFalse(self.service.search("report", limit=0)["success"])
        self.assertFalse(self.service.search("report", limit=51)["success"])
        self.assertFalse(self.service.search("x" * 201)["success"])


if __name__ == "__main__":
    unittest.main()
