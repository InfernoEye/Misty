"""Tests for the isolated SQLite FTS5 knowledge index.

Every fixture lives under a :class:`tempfile.TemporaryDirectory`; no test
reads or writes the real user home directory. The index database is created
inside the temporary tree and ``allowed_base`` is injected explicitly, so the
scope checks are exercised without relying on the current environment.
"""

import os
import tempfile
import unittest
from pathlib import Path

from app.knowledge import (
    DEFAULT_MAX_QUERY_CHARS,
    DEFAULT_MAX_SNIPPET_CHARS,
    DEFAULT_MAX_TEXT_CHARS,
    MAX_RESULTS,
    SNIPPET_END,
    SNIPPET_START,
    KnowledgeIndex,
    KnowledgeIndexError,
)


class KnowledgeIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        self.index = self._make_index()

    def _make_index(self, name="index.sqlite3", **options):
        database = self.base / "db" / name
        index = KnowledgeIndex(
            database,
            allowed_base=self.base,
            **options,
        )
        self.addCleanup(index.close)
        return index

    def _write(self, name, text, root=None):
        target = (root or self.root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    # ------------------------------------------------------------------
    # Indexing formats
    # ------------------------------------------------------------------

    def test_indexes_txt_md_and_html_without_script_text(self):
        self._write("notes.txt", "Quarterly budget review is ready.")
        self._write("plan.md", "# Roadmap\n\nHiring plan for autumn.")
        self._write(
            "page.html",
            """<!DOCTYPE html>
<html>
  <head>
    <title>Ignored</title>
    <style>.marker { color: red; }</style>
    <script>var hidden = "SCRIPTTOKEN";</script>
  </head>
  <body>
    <h1>Visible heading</h1>
    <noscript>NOSCRIPTSECRET</noscript>
    <template>TEMPLATESECRET</template>
    <p>Page body text.</p>
  </body>
</html>
""",
        )

        result = self.index.index_root(self.root)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["indexed"], 3)
        self.assertEqual(result["unchanged"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["indexed_total"], 3)

        for query, title in (
            ("budget", "notes.txt"),
            ("roadmap", "plan.md"),
            ("visible", "page.html"),
            ("notes", "notes.txt"),  # filename/title is indexed too
        ):
            found = self.index.search(query)
            self.assertTrue(found["success"], found)
            self.assertEqual(
                [item["title"] for item in found["results"]],
                [title],
                (query, found),
            )

        for stripped in (
            "SCRIPTTOKEN",
            "NOSCRIPTSECRET",
            "TEMPLATESECRET",
            "marker",
        ):
            self.assertEqual(
                self.index.search(stripped)["results"], [], stripped
            )

    # ------------------------------------------------------------------
    # Incremental behaviour
    # ------------------------------------------------------------------

    def test_incremental_skips_unchanged_and_reindexes_changed_text(self):
        document = self._write("note.txt", "alpha token")

        first = self.index.index_root(self.root)
        self.assertEqual((first["indexed"], first["unchanged"]), (1, 0))

        second = self.index.index_root(self.root)
        self.assertEqual(second["indexed"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["deleted"], 0)

        document.write_text(
            "beta token with a different length", encoding="utf-8"
        )
        third = self.index.index_root(self.root)

        self.assertEqual(third["updated"], 1)
        self.assertEqual(third["indexed"], 0)
        self.assertEqual(third["unchanged"], 0)
        self.assertTrue(self.index.search("beta")["results"])
        self.assertEqual(self.index.search("alpha")["results"], [])

    def test_touched_file_with_identical_text_is_not_reindexed(self):
        document = self._write("stable.txt", "unchanged payload")
        self.index.index_root(self.root)

        info = document.stat()
        os.utime(
            document,
            ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000),
        )
        result = self.index.index_root(self.root)

        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["indexed"], 0)

    def test_deleted_files_are_removed_from_index(self):
        document = self._write("gone.txt", "ephemeral phrase")
        self.index.index_root(self.root)
        self.assertEqual(self.index.stats()["indexed"], 1)

        document.unlink()
        result = self.index.index_root(self.root)

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self.index.stats()["indexed"], 0)
        self.assertEqual(self.index.search("ephemeral")["results"], [])

    def test_truncated_scan_does_not_prune_unvisited_rows(self):
        self._write("deep/note.txt", "deep needle")
        full = self._make_index("prune.sqlite3", max_depth=3)
        self.assertEqual(full.index_root(self.root)["indexed"], 1)

        shallow = self._make_index("prune.sqlite3", max_depth=0)
        result = shallow.index_root(self.root)

        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "depth budget")
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(shallow.stats()["indexed"], 1)

    # ------------------------------------------------------------------
    # Search behaviour
    # ------------------------------------------------------------------

    def test_search_returns_bounded_metadata_and_limited_hits(self):
        for position in range(4):
            self._write(
                f"doc{position}.txt",
                f"needle number {position} " + "filler sentence " * 400,
            )
        self.index.index_root(self.root)

        result = self.index.search("needle", limit=2)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["limit"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["results"]), 2)

        item = result["results"][0]
        self.assertTrue(item["path"].startswith(str(self.root)))
        self.assertTrue(item["title"].endswith(".txt"))
        self.assertIsInstance(item["rank"], float)
        self.assertIsInstance(item["mtime"], float)
        self.assertAlmostEqual(
            item["mtime"], os.stat(item["path"]).st_mtime, places=6
        )
        self.assertIn(SNIPPET_START, item["snippet"])
        self.assertIn(SNIPPET_END, item["snippet"])
        self.assertLessEqual(
            len(item["snippet"]), DEFAULT_MAX_SNIPPET_CHARS
        )
        self.assertLessEqual(len(item["text"]), DEFAULT_MAX_TEXT_CHARS)
        self.assertNotIn("content", item)

    def test_search_with_fts_syntax_characters_is_safe(self):
        self._write("safe.txt", "ordinary words only")
        self.index.index_root(self.root)

        for query in (
            '"; DROP TABLE documents; --',
            "NEAR(",
            "*",
            '"',
            "report OR \"quoted\"",
            "AND OR NOT",
            "a-b:c*",
            "((()))",
        ):
            result = self.index.search(query)
            self.assertTrue(result["success"], (query, result))
            self.assertEqual(result["results"], [], query)
            self.assertEqual(result["count"], 0, query)

        # The documents table survived every hostile-looking query.
        self.assertEqual(self.index.stats()["indexed"], 1)
        self.assertTrue(self.index.search("ordinary")["results"])

    def test_search_bounds_query_and_limit(self):
        too_long = "x" * (DEFAULT_MAX_QUERY_CHARS + 1)
        result = self.index.search(too_long)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "query_too_long")
        self.assertEqual(result["results"], [])

        for limit in (0, -1, MAX_RESULTS + 1, True, "3", None):
            result = self.index.search("text", limit=limit)
            self.assertFalse(result["success"], limit)
            self.assertEqual(result["error"], "invalid_limit", limit)

        self.assertFalse(self.index.search(None)["success"])
        self.assertFalse(self.index.search("")["success"])
        self.assertFalse(self.index.search("   ")["success"])
        self.assertEqual(self.index.search("***")["count"], 0)

    def test_search_clips_snippets_and_balances_bold_markers(self):
        self._write(
            "long.txt",
            "prefix " + "word " * 500 + "unique tail",
        )
        self.index.index_root(self.root)
        item = self.index.search("unique")["results"][0]

        self.assertLessEqual(
            len(item["snippet"]), DEFAULT_MAX_SNIPPET_CHARS
        )
        self.assertEqual(
            item["snippet"].count(SNIPPET_START),
            item["snippet"].count(SNIPPET_END),
        )

    # ------------------------------------------------------------------
    # Scope and symlinks
    # ------------------------------------------------------------------

    def test_rejects_root_outside_allowed_base(self):
        with tempfile.TemporaryDirectory() as outside:
            outside_root = Path(outside)
            (outside_root / "secret.txt").write_text(
                "outside secret token", encoding="utf-8"
            )

            result = self.index.index_root(outside_root)

            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "root_outside_scope")
            self.assertIsNone(result["root"])
            self.assertEqual(self.index.stats()["indexed"], 0)
            self.assertEqual(self.index.search("outside")["results"], [])

    def test_rejects_symlinked_root_escaping_base(self):
        with tempfile.TemporaryDirectory() as outside:
            outside_root = Path(outside)
            (outside_root / "hidden.txt").write_text(
                "symlink secret", encoding="utf-8"
            )
            link = self.base / "escape-root"
            link.symlink_to(outside_root, target_is_directory=True)

            result = self.index.index_root(link)

            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "root_outside_scope")
            self.assertEqual(self.index.stats()["indexed"], 0)

    def test_skips_symlinked_files_and_directories_inside_root(self):
        self._write("real.txt", "real content")
        with tempfile.TemporaryDirectory() as outside:
            outside_root = Path(outside)
            secret = outside_root / "secret.txt"
            secret.write_text("escaped secret token", encoding="utf-8")
            (self.root / "linked.txt").symlink_to(secret)
            (self.root / "linked-dir").symlink_to(
                outside_root, target_is_directory=True
            )

            result = self.index.index_root(self.root)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["indexed"], 1)
        self.assertGreaterEqual(result["skipped_symlinks"], 2)
        self.assertEqual(self.index.search("escaped")["results"], [])
        self.assertTrue(self.index.search("real")["results"])

    def test_rejects_missing_root_and_file_root(self):
        missing = self.index.index_root(self.root / "does-not-exist")
        self.assertFalse(missing["success"])
        self.assertEqual(missing["error"], "root_not_found")

        file_root = self._write("not-a-directory.txt", "text")
        result = self.index.index_root(file_root)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "root_not_directory")

    # ------------------------------------------------------------------
    # Exclusions and budgets
    # ------------------------------------------------------------------

    def test_excluded_directories_are_skipped(self):
        self._write(".cache/hidden-token.txt", "cache secret")
        self._write("node_modules/hidden-token.txt", "modules secret")
        self._write("docs/.git/hidden-token.txt", "git secret")
        self._write("docs/keep.txt", "kept document")

        result = self.index.index_root(self.root)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["skipped_excluded"], 3)
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(self.index.search("hidden")["results"], [])
        self.assertTrue(self.index.search("kept")["results"])

    def test_oversized_and_unsupported_files_are_ignored_with_counts(self):
        index = self._make_index("bounded.sqlite3", max_file_bytes=1024)
        self._write("small.txt", "small readable text")
        self._write("big.txt", "B" * 4096)
        self._write("manual.pdf", "%PDF-1.4 not really a PDF")
        self._write("data.bin", "not supported")

        result = index.index_root(self.root)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(result["skipped_oversized"], 1)
        self.assertEqual(result["skipped_unsupported"], 2)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(index.stats()["indexed"], 1)
        self.assertEqual(index.search("readable")["count"], 1)

    def test_respects_depth_budget(self):
        self._write("one/two/three/deep.txt", "deep needle")
        self._write("top.txt", "top needle")
        index = self._make_index("depth.sqlite3", max_depth=1)

        result = index.index_root(self.root)

        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "depth budget")
        self.assertEqual(result["indexed"], 1)
        self.assertTrue(index.search("top")["results"])
        self.assertEqual(index.search("deep")["results"], [])

    def test_respects_total_document_budget(self):
        for number in range(3):
            self._write(f"budget-{number}.txt", f"budget file {number}")
        index = self._make_index("capped.sqlite3", max_total_documents=2)

        result = index.index_root(self.root)

        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["skipped_document_budget"], 1)
        self.assertTrue(result["document_budget_reached"])
        self.assertFalse(result["truncated"])
        self.assertEqual(index.stats()["indexed"], 2)

    # ------------------------------------------------------------------
    # Stats, removal, and clearing
    # ------------------------------------------------------------------

    def test_stats_lists_roots_and_clear_keeps_source_files(self):
        second = self.base / "root-two"
        second.mkdir()
        self._write("a.txt", "first root text")
        self._write("b.md", "second root text", root=second)

        self.index.index_root(self.root)
        self.index.index_root(second)

        stats = self.index.stats()
        self.assertTrue(stats["success"], stats)
        self.assertEqual(stats["indexed"], 2)
        self.assertEqual(stats["documents"], 2)
        self.assertEqual(stats["root_count"], 2)
        self.assertEqual(stats["roots"], sorted([str(self.root), str(second)]))
        self.assertEqual(
            sum(item["documents"] for item in stats["roots_detail"]), 2
        )
        self.assertGreater(stats["indexed_bytes"], 0)

        cleared = self.index.clear()

        self.assertTrue(cleared["success"], cleared)
        self.assertEqual(cleared["deleted"], 2)
        after = self.index.stats()
        self.assertEqual(after["indexed"], 0)
        self.assertEqual(after["roots"], [])
        self.assertEqual(after["root_count"], 0)
        self.assertEqual(self.index.search("root")["results"], [])

        # Deletion is database-only: the source files are untouched.
        self.assertTrue((self.root / "a.txt").is_file())
        self.assertTrue((second / "b.md").is_file())

    def test_remove_root_deletes_only_that_scope(self):
        second = self.base / "root-two"
        second.mkdir()
        self._write("a.txt", "first root text")
        self._write("b.md", "second root text", root=second)
        self.index.index_root(self.root)
        self.index.index_root(second)

        removed = self.index.remove_root(self.root)

        self.assertTrue(removed["success"], removed)
        self.assertEqual(removed["deleted"], 1)
        stats = self.index.stats()
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(stats["roots"], [str(second)])
        self.assertEqual(self.index.search("first")["results"], [])
        self.assertTrue(self.index.search("second")["results"])
        self.assertTrue((self.root / "a.txt").is_file())
        self.assertTrue((second / "b.md").is_file())

        again = self.index.remove_root(self.root)
        self.assertTrue(again["success"])
        self.assertEqual(again["deleted"], 0)

        # A root that no longer exists can still be forgotten.
        second.rename(self.base / "root-two-renamed")
        forgotten = self.index.remove_root(second)
        self.assertTrue(forgotten["success"], forgotten)
        self.assertEqual(forgotten["deleted"], 1)
        self.assertEqual(self.index.stats()["indexed"], 0)

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(KnowledgeIndexError):
            KnowledgeIndex(
                self.base / "db" / "bad.sqlite3",
                allowed_base=self.base,
                max_file_bytes=0,
            )
        with self.assertRaises(KnowledgeIndexError):
            KnowledgeIndex(
                self.base / "db" / "bad.sqlite3",
                allowed_base=self.base,
                max_depth=-1,
            )


if __name__ == "__main__":
    unittest.main()
