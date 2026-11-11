"""Tests for Misty's local persistent memory."""

import tempfile
import unittest
from pathlib import Path

from app.brain.memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "memory.sqlite3"
        self.memory = MemoryStore(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_and_get(self):
        memory_id = self.memory.add(
            "I prefer Polish responses.",
            "preference",
        )

        result = self.memory.get(memory_id)

        self.assertEqual(result["content"], "I prefer Polish responses.")
        self.assertEqual(result["category"], "preference")
        self.assertIn("created_at", result)
        self.assertIn("updated_at", result)

    def test_search(self):
        self.memory.add(
            "My main browser is Firefox.",
            "fact",
        )
        self.memory.add(
            "I prefer dark desktop themes.",
            "preference",
        )

        results = self.memory.search("Firefox browser")

        self.assertEqual(len(results), 1)
        self.assertIn("Firefox", results[0]["content"])

    def test_polish_search(self):
        self.memory.add(
            "Wolę odpowiedzi w języku polskim.",
            "preference",
        )

        results = self.memory.search("języku polskim")

        self.assertEqual(len(results), 1)

    def test_category_filter(self):
        self.memory.add("Use Firefox.", "preference")
        self.memory.add("Misty is a local assistant.", "project")

        results = self.memory.list(category="project")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["category"], "project")

    def test_update(self):
        memory_id = self.memory.add("Old preference.", "preference")

        updated = self.memory.update(
            memory_id,
            "New preference.",
        )

        self.assertTrue(updated)
        self.assertEqual(
            self.memory.get(memory_id)["content"],
            "New preference.",
        )

    def test_update_category(self):
        memory_id = self.memory.add("A fact.", "fact")

        self.memory.update(
            memory_id,
            "An ongoing project.",
            category="project",
        )

        self.assertEqual(
            self.memory.get(memory_id)["category"],
            "project",
        )

    def test_delete(self):
        memory_id = self.memory.add("Temporary memory.", "fact")

        self.assertTrue(self.memory.delete(memory_id))
        self.assertIsNone(self.memory.get(memory_id))
        self.assertEqual(self.memory.count(), 0)

    def test_fts_updates_after_edit_and_delete(self):
        memory_id = self.memory.add("I use Firefox.", "fact")

        self.memory.update(memory_id, "I use Chromium.", "fact")

        self.assertEqual(self.memory.search("Firefox"), [])
        self.assertEqual(len(self.memory.search("Chromium")), 1)

        self.memory.delete(memory_id)

        self.assertEqual(self.memory.search("Chromium"), [])

    def test_persistence_after_reopening(self):
        memory_id = self.memory.add(
            "Misty runs locally.",
            "project",
        )

        reopened = MemoryStore(self.database)

        self.assertEqual(
            reopened.get(memory_id)["content"],
            "Misty runs locally.",
        )

    def test_invalid_category(self):
        with self.assertRaises(ValueError):
            self.memory.add("Invalid category.", "secret")

    def test_empty_content(self):
        with self.assertRaises(ValueError):
            self.memory.add("   ", "fact")

    def test_content_length_limit(self):
        with self.assertRaises(ValueError):
            self.memory.add("x" * 4001, "fact")

    def test_invalid_id(self):
        with self.assertRaises(ValueError):
            self.memory.get(True)

    def test_search_handles_fts_syntax_as_plain_text(self):
        memory_id = self.memory.add(
            "Misty uses local memory.",
            "project",
        )

        results = self.memory.search('" OR * NEAR(')

        self.assertEqual(results, [])
        self.assertIsNotNone(self.memory.get(memory_id))


if __name__ == "__main__":
    unittest.main()
