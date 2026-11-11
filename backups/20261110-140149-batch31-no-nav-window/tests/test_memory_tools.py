import tempfile
import unittest
from pathlib import Path

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "memory.sqlite3"
        self.store = MemoryStore(database=database)
        self.registry = ToolRegistry(memory_store=self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_remember_and_recall(self):
        saved = self.registry.execute("remember", {
            "content": "Prefers concise answers in Polish.",
            "category": "preference",
        })
        self.assertTrue(saved["success"])
        self.assertGreater(saved["memory_id"], 0)

        found = self.registry.execute("recall", {
            "query": "Polish concise",
        })
        self.assertTrue(found["success"])
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["memories"][0]["content"],
                         "Prefers concise answers in Polish.")

    def test_list_memories(self):
        self.registry.execute("remember", {
            "content": "Project Misty uses local models.",
            "category": "project",
        })
        result = self.registry.execute("list_memories", {
            "category": "project",
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)

    def test_forget_requires_confirmation(self):
        saved = self.registry.execute("remember", {
            "content": "Temporary test memory.",
            "category": "fact",
        })
        memory_id = saved["memory_id"]

        self.assertTrue(self.registry.requires_confirmation("forget_memory"))
        denied = self.registry.execute(
            "forget_memory", {"memory_id": memory_id}
        )
        self.assertFalse(denied["success"])
        self.assertIsNotNone(self.store.get(memory_id))

        deleted = self.registry.execute(
            "forget_memory", {"memory_id": memory_id}, confirmed=True
        )
        self.assertTrue(deleted["success"])
        self.assertIsNone(self.store.get(memory_id))

    def test_rejects_obvious_credentials(self):
        result = self.registry.execute("remember", {
            "content": "My API key is abc123secret",
            "category": "fact",
        })
        self.assertFalse(result["success"])
        self.assertEqual(self.store.count(), 0)

    def test_rejects_invalid_arguments(self):
        result = self.registry.execute("remember", {
            "content": "A fact",
            "category": "unknown",
        })
        self.assertFalse(result["success"])

        result = self.registry.execute("recall", {
            "query": "test",
            "unexpected": True,
        })
        self.assertFalse(result["success"])

    def test_unknown_memory_id(self):
        result = self.registry.execute(
            "forget_memory", {"memory_id": 999}, confirmed=True
        )
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
