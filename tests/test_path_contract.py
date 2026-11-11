"""Cross-engine contract tests for ``app.actions.path_contract``.

The lexical helpers used to be copied into file_ops, file_transfer,
archive_zip and directory_transfer.  These tests pin (a) one shared
accept/reject decision plus error code per corpus row across the three
in-scope engines through their public APIs, and (b) that no engine carries an
independent copy any more.
"""

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

from app.actions import path_contract
from app.actions.archive_zip import ZipEngine
from app.actions.directory_transfer import DirectoryTransferEngine
from app.actions.file_ops import FileOpsEngine
from app.actions.file_scope import UserFolderResolver
from app.actions.file_transfer import FileTransferEngine

ENGINE_FILES = (
    "app/actions/file_ops.py",
    "app/actions/file_transfer.py",
    "app/actions/archive_zip.py",
    "app/actions/directory_transfer.py",
)
CORE_HELPERS = (
    "_relative_path",
    "_base_for",
    "_has_trailing_separator",
    "_parent_chain",
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

OK = None  # engines accept the lexical form
# Engines disagree only on these rows (see the policy table below); every
# other row must yield the identical code in all four engines.
DIRECTORY_DOT_REJECT = ("double slash", "dot component", "leading dot")
ARCHIVE_BACKSLASH_REJECT = ("backslash",)
# Rows where "." / "" components collapse onto the same file; only
# directory_transfer rejects them (POLICY_DIRECTORY_TRANSFER).
DOT_COLLAPSING = ("double slash", "dot component")

# (label, value, expected code).  OK means the four engines agree on a valid
# lexical form.  The probes resolve the value inside Downloads, whose source
# tree is built in EngineFixture; the layout never decides a row.
OK = None
CORPUS = (
    ("nested", "c/d", OK),
    ("double slash", "c//x", OK),
    ("dot component", "c/./x", OK),
    ("leading dot", "./c/d", OK),
    ("parent escape", "../x", "traversal"),
    ("embedded escape", "a/../../x", "traversal"),
    ("absolute", "/abs", "absolute_path"),
    ("trailing dotdot", "a/..", "traversal"),
    ("empty", "", "empty_relative_path"),
    ("dot only", ".", "invalid_relative_path"),
    ("trailing slash", "c/", "invalid_relative_path"),
    ("nested trailing slash", "c/x/", "invalid_relative_path"),
    ("backslash", "a\\b", OK),
    ("NUL", "a\x00b", "invalid_relative_path"),
    ("200 chars", "x" * 197 + "/f", OK),
    ("char length limit", "a" * 1024 + "b" * 5, "path_too_long"),
    ("depth limit", "/".join(["d"] * 33), "path_too_deep"),
    ("depth at limit", "/".join(["d"] * 32), OK),
    ("polish unicode", "c/zażółć", OK),
    ("encoded parent", "..%2Fx", OK),
    ("encoded separator", "a%2Fb", OK),
    ("reserved CON", "CON", OK),
    ("reserved aux.txt", "aux.txt", OK),
    ("double trailing separator", "c//", "invalid_relative_path"),
    ("not a string", 123, "invalid_relative_path"),
    ("none", None, "invalid_relative_path"),
)


def _code(result):
    """Normalise the result shape of the four engines."""
    if result.get("success"):
        return None
    if "error_code" in result:
        return result["error_code"]
    return result.get("code")


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        for name in (
            "Documents",
            "Pobrane",
            "Desktop",
            "quarantine",
            "source_dir",
        ):
            (self.home / name).mkdir()
        self.resolver = UserFolderResolver(
            home=self.home, xdg_lookup=lambda _name: None
        )
        self.ops = FileOpsEngine(
            resolver=self.resolver, quarantine_root=self.home / "quarantine"
        )
        self.transfer = FileTransferEngine(resolver=self.resolver)
        self.zip = ZipEngine(resolver=self.resolver)
        self.directory = DirectoryTransferEngine(resolver=self.resolver)
        # Downloads is the only key the resolver relocates; it is the source
        # folder for file_transfer, archive_zip.create and directory_transfer.
        # Accepted corpus values resolve to a real object whose kind matches
        # the probe ("a/" -> directory "a", "a/./b" -> file "b"), so the
        # lexical stage is the only one that can decide.
        # file_ops previews a delete of "a/" (needs directory "a"); the other
        # engines preview copies/create and need a file at the same place.
        # Building both layouts per test row keeps the source kind explicit.
        self.downloads = self.home / "Pobrane"

    # The Downloads tree: every corpus value (a file unless the value is
    # also a parent of another value, and "c", which probe_ops deletes).
    TREES = {
        "file": (
            ("c", "dir"), ("a", "dir"), ("a\\b", "dir"),
            ("c/d", "dir"), ("c/x", "file"), ("a/b", "file"),
            ("x" * 197, "dir"), ("x" * 197 + "/f", "file"),
            ("/".join(["d"] * 31), "dir"), ("/".join(["d"] * 32), "file"),
            ("c/zażółć", "dir"), ("..%2Fx", "dir"), ("a%2Fb", "dir"),
            ("CON", "dir"), ("aux.txt", "dir"),
        ),
        "directory": (
            ("c", "dir"), ("a", "dir"), ("c/d", "dir"), ("c/x", "dir"),
            ("a/b", "dir"), ("a\\b", "dir"), ("c/zażółć", "dir"),
            ("..%2Fx", "dir"), ("a%2Fb", "dir"), ("CON", "dir"),
            ("aux.txt", "dir"),
        ),
    }

    def _build_tree(self, kind):
        """(Re)build Downloads; ``kind`` is the lexical target under test."""
        root = self.downloads
        # "Downloads" resolves to Pobrane, which is a symlink to the real
        # external Downloads directory: clear the contents, keep the link.
        if root.is_symlink():
            for child in list(root.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        else:
            if root.is_dir():
                shutil.rmtree(root)
            root.mkdir()
        for relative, entry in self.TREES[kind]:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry == "dir":
                target.mkdir()
            else:
                target.write_bytes(b"x")

    # Each probe returns the engine's error code (None = accepted).
    def probe_ops(self, value):
        # mkdir accepts missing parents, so an accepted value never trips a
        # missing-parent branch; preview_mkdir is read-only.
        return _code(self.ops.preview_mkdir("Documents", value, True))

    def probe_transfer(self, value):
        return _code(
            self.transfer.preview_copy(
                "Downloads", value, "Documents", "out.txt"
            )
        )

    def probe_zip(self, value):
        # "a\\b" also exists under Downloads so the create call-site
        # backslash rule, not a source lookup, decides that row.
        return _code(self.zip.preview_create("Downloads", [value], "b.zip"))

    def probe_directory(self, value):
        return _code(
            self.directory.preview_copy(
                "Downloads", value, "Desktop", "out"
            )
        )

    def probes(self):
        return {
            "file_ops": self.probe_ops,
            "file_transfer": self.probe_transfer,
            "archive_zip": self.probe_zip,
            "directory_transfer": self.probe_directory,
        }


class CrossEngineCorpusTests(EngineFixture):
    LEXICAL_CODES = {
        "traversal", "absolute_path", "empty_relative_path",
        "invalid_relative_path", "path_too_long", "path_too_deep",
    }

    def _expected(self, engine, label, expected):
        if label in ARCHIVE_BACKSLASH_REJECT and engine == "archive_zip":
            # archive_zip refuses "\\" at its create call site, not in the
            # shared lexical contract.
            return "invalid_relative_path"
        if label in DOT_COLLAPSING and engine == "directory_transfer":
            # <policy>: "." and "" components are rejected by
            # directory_transfer (POLICY_DIRECTORY_TRANSFER).
            return "invalid_relative_path"
        return expected

    def test_engines_agree_per_corpus_row(self):
        self.assertGreaterEqual(len(CORPUS), 20)
        self._build_tree("file")
        for label, value, expected in CORPUS:
            with self.subTest(case=label, value=value):
                decisions = {
                    name: probe(value)
                    for name, probe in self.probes().items()
                }
                for name, code in decisions.items():
                    wanted = self._expected(name, label, expected)
                    if wanted is None:
                        # Lexically accepted: whatever happens next must not
                        # be a lexical rejection.
                        self.assertFalse(
                            code in self.LEXICAL_CODES - {"invalid_relative_path"},
                            f"{name}: {label!r} rejected as {code!r}",
                        )
                    else:
                        self.assertEqual(
                            code, wanted, f"{name}: {label!r} -> {code!r}"
                        )
                if label in DOT_COLLAPSING:
                    # Identical accept decision in the three baselines; the
                    # directory policy is the only difference and it is
                    # asserted through _expected above.
                    for name in ("file_ops", "file_transfer", "archive_zip"):
                        self.assertIsNone(decisions[name])

    def test_trailing_separator_is_rejected_everywhere(self):
        self._build_tree("directory")
        for value in ("c/", "c//", "c/.", "c/x/"):
            for name, probe in self.probes().items():
                with self.subTest(engine=name, value=value):
                    self.assertEqual(probe(value), "invalid_relative_path")

    def test_shared_policy_axes_are_explicit(self):
        policy = path_contract.POLICY_DIRECTORY_TRANSFER
        self.assertTrue(policy.reject_dot_components)
        self.assertTrue(policy.backslash_is_separator)
        for other in (
            path_contract.POLICY_FILE_OPS,
            path_contract.POLICY_FILE_TRANSFER,
            path_contract.POLICY_ARCHIVE_ZIP,
        ):
            self.assertEqual(other, path_contract.POLICY_BASELINE)


class PathContractUnitTests(unittest.TestCase):
    def test_codes_and_messages_are_stable(self):
        error = path_contract.PathContractError("traversal")
        self.assertEqual(error.code, "traversal")
        self.assertEqual(error.message, "The path must not contain '..'.")
        self.assertIn("traversal", repr(error))
        self.assertIsInstance(error, ValueError)

    def test_relative_path_parts_returns_components(self):
        self.assertEqual(
            path_contract.relative_path_parts("a/./b//c"), ("a", "b", "c")
        )
        self.assertEqual(
            path_contract.relative_path_parts("a\\b"), ("a\\b",)
        )
        self.assertEqual(
            path_contract.relative_path_parts(
                "a\\b", policy=path_contract.POLICY_DIRECTORY_TRANSFER
            ),
            ("a\\b",),
        )

    def test_has_trailing_separator(self):
        for value in ("a/", "a//", "/"):
            with self.subTest(value=value):
                self.assertTrue(path_contract.has_trailing_separator(value))
        for value in ("a", "", None, 3, "a/b"):
            with self.subTest(value=value):
                self.assertFalse(path_contract.has_trailing_separator(value))

    def test_approved_base_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "Documents").mkdir()
            outside = home.parent / (home.name + "_outside")
            outside.mkdir()
            self.addCleanup(outside.rmdir)

            class Resolver:
                def __init__(self):
                    self.home = home

                def base_for_key(self, key):
                    if key == "Documents":
                        return home / "Documents"
                    if key == "Outside":
                        return outside
                    raise RuntimeError(key)

            resolver = Resolver()
            self.assertEqual(
                path_contract.approved_base(resolver, "Documents"),
                home / "Documents",
            )
            # file_ops keeps its historic behaviour: no home check.
            self.assertEqual(
                path_contract.approved_base(resolver, "Outside"), outside
            )
            with self.assertRaises(path_contract.PathContractError) as ctx:
                path_contract.approved_base(
                    resolver, "Outside", enforce_home=True
                )
            self.assertEqual(ctx.exception.code, "invalid_folder")
            for bad in ("", None, "Missing", "/etc"):
                with self.subTest(value=bad):
                    with self.assertRaises(
                        path_contract.PathContractError
                    ) as ctx:
                        path_contract.approved_base(resolver, bad)
                    self.assertEqual(ctx.exception.code, "invalid_folder")

    def test_parent_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "a").mkdir()
            (base / "file").write_text("x", encoding="utf-8")
            (base / "link").symlink_to(base / "a")

            parent, missing = path_contract.parent_chain(base, ("a", "b"))
            self.assertEqual(parent, base / "a")
            self.assertEqual(missing, [])

            cases = {
                ("file", "x"): "parent_not_directory",
                ("link", "x"): "symlink_parent",
                ("gone", "x"): "missing_parent",
            }
            for parts, code in cases.items():
                with self.subTest(parts=parts):
                    with self.assertRaises(
                        path_contract.PathContractError
                    ) as ctx:
                        path_contract.parent_chain(base, parts)
                    self.assertEqual(ctx.exception.code, code)

            parent, missing = path_contract.parent_chain(
                base, ("p", "q", "r"), allow_missing=True, collect_missing=True
            )
            self.assertEqual(parent, base / "p")
            self.assertEqual(missing, ["p", "q"])

            # without allow_missing the ENOENT path raises missing_parent
            with self.assertRaises(path_contract.PathContractError) as ctx:
                path_contract.parent_chain(base, ("p", "q", "r"))
            self.assertEqual(ctx.exception.code, "missing_parent")


def _delegating_node(node):
    """A helper body delegates when it calls path_contract and is short."""
    body = [
        stmt
        for stmt in node.body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]
    return body


def _calls_path_contract(node):
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "path_contract"
        ):
            return True
    return False


class NoIndependentCopiesTests(unittest.TestCase):
    def test_engines_delegate_to_path_contract(self):
        for relative in ENGINE_FILES:
            source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            found = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and (
                    node.name in CORE_HELPERS
                ):
                    found.setdefault(node.name, []).append(node)
            with self.subTest(engine=relative):
                expected = set(CORE_HELPERS)
                if relative == "app/actions/file_ops.py":
                    # file_ops never had this helper: it rejected a trailing
                    # separator with an inline endswith("/") check.
                    expected.discard("_has_trailing_separator")
                self.assertEqual(set(found), expected)
                for name, nodes in found.items():
                    self.assertEqual(len(nodes), 1, name)
                    node = nodes[0]
                    statements = _delegating_node(node)
                    self.assertLessEqual(
                        len(statements), 2, f"{relative}:{name} body"
                    )
                    self.assertTrue(
                        _calls_path_contract(node),
                        f"{relative}:{name} does not call path_contract",
                    )

    def test_path_contract_is_the_only_definition(self):
        source = (
            PROJECT_ROOT / "app/actions/path_contract.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertTrue(
            {
                "relative_path_parts",
                "approved_base",
                "has_trailing_separator",
                "parent_chain",
            }.issubset(defined)
        )


if __name__ == "__main__":
    unittest.main()
