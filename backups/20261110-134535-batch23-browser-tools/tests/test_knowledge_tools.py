import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.actions.file_scope import UserFolderResolver
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeKnowledgeIndex:
    def __init__(self):
        self.indexed = []
        self.queries = []
        self.stats_calls = 0

    def index_root(self, root):
        self.indexed.append(str(root))
        return {"success": True, "indexed": 2, "message": "ok"}

    def search(self, query, limit=10):
        self.queries.append({"query": query, "limit": limit})
        return {
            "success": True,
            "query": query,
            "results": [{
                "path": "/home/user/Documents/note.txt",
                "title": "note.txt",
                "snippet": "[[Misty]] note",
                "rank": 1.0,
            }],
            "count": 1,
            "limit": limit,
            "message": "ok",
        }

    def stats(self):
        self.stats_calls += 1
        return {"success": True, "count": 2, "indexed": 2}


class KnowledgeToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "Documents").mkdir()
        self.resolver = UserFolderResolver(
            home=self.home, xdg_lookup=lambda _name: None
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(
            Path(directory.name) / "memory.sqlite3"
        )
        self.index = FakeKnowledgeIndex()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            folder_resolver=self.resolver,
            knowledge_index=self.index,
        )

    def test_tools_are_advertised_only_with_index(self):
        without = ToolRegistry(
            executor=Mock(), memory_store=self.memory
        )
        names_without = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        names_with = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertNotIn("knowledge_search", names_without)
        for name in (
            "index_documents", "knowledge_search", "knowledge_stats"
        ):
            self.assertIn(name, names_with)

    def test_index_uses_resolved_folder_root(self):
        result = self.registry.execute(
            "index_documents", {"folder": "Documents"}
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            self.index.indexed,
            [str((self.home / "Documents").resolve())],
        )

    def test_search_forwards_bounded_query(self):
        result = self.registry.execute(
            "knowledge_search",
            {"query": "  misty  ", "limit": 3},
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            self.index.queries,
            [{"query": "misty", "limit": 3}],
        )

    def test_stats_and_invalid_arguments(self):
        stats = self.registry.execute("knowledge_stats", {})
        bad_search = self.registry.execute(
            "knowledge_search", {"query": ""}
        )
        bad_limit = self.registry.execute(
            "knowledge_search", {"query": "misty", "limit": 21}
        )
        bad_index = self.registry.execute(
            "index_documents", {"folder": "Secret"}
        )

        self.assertTrue(stats["success"])
        self.assertEqual(self.index.stats_calls, 1)
        self.assertFalse(bad_search["success"])
        self.assertFalse(bad_limit["success"])
        self.assertFalse(bad_index["success"])
        self.assertEqual(self.index.queries, [])


if __name__ == "__main__":
    unittest.main()
