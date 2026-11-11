"""Contract inventory: schemas, policy specs, dispatch and planner agree.

The registry advertises schemas, ``ToolRegistry.execute`` dispatches names,
and ``app.brain.policy`` classifies them with an effect spec. The planner
(TaskPlanner) only accepts a step whose tool is advertised, so any name the
model is told about must travel all three paths.

Nothing here touches live services: every collaborator is a fake, and the
handler inventory is derived by AST-parsing ``tools.py`` instead of a
hand-maintained list.
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.actions.web_launch import SITE_SEARCHES
from app.brain import policy as policy_module
from app.brain.memory import MemoryStore
from app.brain.planner import TaskPlanner
from app.brain import tools as tools_module
from app.brain.tools import (
    INTERNAL_TOOL_NAMES,
    NAVIGATION_TOOL_NAMES,
    NO_ARGUMENT_TOOL_NAMES,
    ToolRegistry,
)

TOOLS_PATH = Path(tools_module.__file__)


# ---------------------------------------------------------------------------
# AST inventory of the dispatch region


def _execute_function():
    tree = ast.parse(TOOLS_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolRegistry":
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "execute"
                ):
                    return item
    raise AssertionError("ToolRegistry.execute not found in tools.py")


def _single_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dispatched_arms():
    """Return ``(direct_names, table_names)`` handled by execute().

    A direct name is a string constant in an ``if name == ...`` or
    ``if name in {...}`` test. A table name is a key of a module-level dict
    literal whose *values* are string action names and which execute() reads
    through ``if name in <module constant>``.
    """
    function = _execute_function()
    direct = set()
    table_calls = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], (ast.Eq, ast.In)):
            continue
        sides = [node.left, node.comparators[0]]
        is_name = [
            isinstance(side, ast.Name) and side.id == "name"
            for side in sides
        ]
        if not any(is_name):
            continue
        # Pick whichever side is not the bare ``name`` identifier.
        other = sides[1] if is_name[0] else sides[0]
        if isinstance(other, ast.Constant) and isinstance(other.value, str):
            direct.add(other.value)
        elif isinstance(other, (ast.Set, ast.List, ast.Tuple)):
            for element in other.elts:
                value = _single_str(element)
                if value is not None:
                    direct.add(value)
        elif isinstance(other, ast.Name):
            # ``name`` itself is a bare Name; skip self-comparison noise.
            if other.id != "name":
                table_calls.add(other.id)
    return direct, table_calls


def _table_value(identifier):
    """The assigned value node of ``identifier`` (module or execute local)."""
    tree = ast.parse(TOOLS_PATH.read_text(encoding="utf-8"))
    candidates = list(tree.body)
    candidates.extend(_execute_function().body)
    for node in candidates:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == identifier
            for target in targets
        ):
            return value
    raise AssertionError(f"{identifier} not found in tools.py")


def _dict_items(identifier):
    """``(keys, values)`` of a dict literal read by ``name in <table>``."""
    value = _table_value(identifier)
    if not isinstance(value, ast.Dict):
        raise AssertionError(f"{identifier} is no longer a dict literal.")
    keys = set()
    for key in value.keys:
        name = _single_str(key)
        if name is None:
            # A computed key could hide a tool name; fail loudly rather than
            # under-report the inventory.
            raise AssertionError(
                f"{identifier} uses non-literal keys; update the test."
            )
        keys.add(name)
    values = [_single_str(item) for item in value.values]
    if any(item is None for item in values):
        raise AssertionError(
            f"{identifier} has non-literal values; the table shape changed."
        )
    return keys, values


_ALWAYS_QUERIED_TABLES = frozenset({"no_argument_tools"})


def _table_names(identifier):
    """Tool names in a lookup table read by an ``if name in <table>`` test.

    ``{tool_name: executor_action}`` maps keys to tool names;
    ``{tool_name: executor_method}`` maps names to methods. A frozenset (or
    other non-dict) of names is a sup- or subset of another table, never a
    dispatch table, so it contributes no names here; see
    ``constraint_tables`` for the consistency check on those.
    """
    if not isinstance(_table_value(identifier), ast.Dict):
        return set()
    keys, values = _dict_items(identifier)
    if not keys:
        return set()
    if identifier in _ALWAYS_QUERIED_TABLES:
        return keys
    # A subset like {"knowledge_forget", "knowledge_clear"} guards a branch
    # inside an earlier arm: the query is answered by that wider dispatch,
    # so its keys are not dispatch arms themselves.
    if set(keys) <= subset_guard_names():
        return set()
    return keys


def subset_guard_names():
    """Names already dispatched elsewhere; subset tables add nothing new."""
    names, _ = _dispatched_arms()
    return names


def dispatched_tool_names():
    """Tool names execute() handles; excludes constraint tables."""
    direct, tables = _dispatched_arms()
    if not tables:
        raise AssertionError(
            "execute() no longer dispatches through a name table."
        )
    for identifier in tables:
        names = _table_names(identifier)
        if names is not None:
            direct |= names
    return direct


def constraint_tables():
    """``identifier -> (keys, values)`` for non-dispatch membership tables."""
    _, tables = _dispatched_arms()
    return {
        identifier: _dict_items(identifier)
        for identifier in tables
        if isinstance(_table_value(identifier), ast.Dict)
        and _table_names(identifier) is None
    }


# ---------------------------------------------------------------------------
# Fakes


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def _ok(self, message, **data):
        return {"success": True, "message": message, **data}

    def open_url(self, url):
        # Validate like the real executor so argument drift is visible.
        from app.actions.web_launch import validate_open_url

        try:
            normalized = validate_open_url(url)
        except ValueError as error:
            return {"success": False, "message": str(error)}
        self.calls.append(("open_url", normalized))
        return self._ok(f"Opening {normalized}.", url=normalized)


class FakeWebFetcher:
    """Offline fetch: the real registry path expects a ``FetchResult``."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, **kwargs):
        from app.web.fetch import FetchResult

        self.calls.append(url)
        return FetchResult(
            final_url=url,
            status=200,
            content_type="text/html",
            title="Example",
            text="Example page text.",
            links=[],
            truncated=False,
            text_truncated=False,
            links_truncated=False,
            bytes_read=128,
            charset="utf-8",
        )


class FakeWebSearch:
    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append(query)
        # Shape of SearxngClient.search; ``web_search`` passes it through.
        return {
            "success": True,
            "query": query,
            "results": [
                {"title": "Hit", "url": "https://example.org/hit",
                 "content": "Example page text."}
            ],
            "answers": [],
            "count": 1,
            "limit": kwargs.get("limit"),
            "message": "ok",
        }


# ---------------------------------------------------------------------------



class _StubInputBackend:
    """Minimal input backend: satisfies the constructor, runs nothing."""

    def execute(self, action):
        return {"success": False, "code": "unsupported_action",
                "message": "stub backend"}


class ContractInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._directory.cleanup)
        cls.memory = MemoryStore(
            Path(cls._directory.name) / "memory.sqlite3"
        )
        # Every optional service is present so the widest inventory is built.
        cls.registry = ToolRegistry(
            executor=FakeExecutor(),
            memory_store=cls.memory,
            web_fetcher=FakeWebFetcher(),
            web_search_client=FakeWebSearch(),
            knowledge_index=Mock(),
            vision_service=Mock(),
            browser_session=Mock(),
            reminder_store=Mock(),
            file_transfer_engine=Mock(),
            control_activator=Mock(),
            media_controller=Mock(),
            zip_engine=Mock(),
            text_search_service=Mock(),
            directory_transfer_engine=Mock(),
            calendar_engine=Mock(),
            duplicate_finder=Mock(),
            file_transcriber=Mock(),
            # Desktop control is opt-in; this registry models a fully
            # configured install, so the pointer/keyboard tools exist here.
            input_backend=_StubInputBackend(),
        )

    def advertised(self):
        definitions = self.registry.available_tools()
        names = [item["function"]["name"] for item in definitions]
        self.assertEqual(len(names), len(set(names)), "duplicate schema names")
        return {item["function"]["name"]: item["function"] for item in definitions}

    def test_dispatch_inventory_parses(self):
        handled = dispatched_tool_names()
        self.assertGreater(len(handled), 50, "dispatch parse found too few arms")
        advertised = set(self.advertised())
        orphans = sorted(
            name for name in handled
            if name not in advertised and name not in INTERNAL_TOOL_NAMES
        )
        self.assertEqual(
            orphans, [],
            "dispatched tools with no schema and no INTERNAL_TOOL_NAMES entry",
        )

    # Dispatched tools that are advertised and runnable through the
    # registry but still have no policy spec. Today these are the knowledge
    # maintenance tools exported mid-change by the concurrent knowledge
    # work; unknown tools always require approval, so they fail closed.
    # Shrink this list (never grow it) once their specs land.
    KNOWN_UNSPECIFIED = frozenset()

    def test_every_dispatched_schema_has_a_policy_spec(self):
        # Only tools execute() actually handles are held to this rule: a
        # schema that is advertised but not yet dispatchable cannot run.
        handled = dispatched_tool_names()
        missing = sorted(
            name for name in self.advertised()
            if name in handled and policy_module.policy_for(name) is None
        )
        self.assertEqual(
            sorted(set(missing) - self.KNOWN_UNSPECIFIED), [],
            "dispatched tools without policy",
        )
        # The allowlist must not outlive the defect: drop names that gained
        # a spec so a regression elsewhere is never masked.
        self.assertEqual(
            sorted(self.KNOWN_UNSPECIFIED - set(missing)), [],
            "KNOWN_UNSPECIFIED is stale; remove the resolved names",
        )

    def test_no_dispatched_tool_is_invisible_to_the_planner(self):
        """Navigation contract: handlers must be reachable by the planner."""
        advertised = self.advertised()
        hidden = sorted(
            name for name in dispatched_tool_names()
            if name not in advertised and name not in INTERNAL_TOOL_NAMES
        )
        self.assertEqual(
            hidden, [], "dispatchable tools the planner cannot propose"
        )

    def test_every_policy_spec_is_advertised_or_internal(self):
        advertised = self.advertised()
        orphans = sorted(
            name for name in policy_module.DEFAULT_POLICY.names()
            if name not in advertised and name not in INTERNAL_TOOL_NAMES
        )
        self.assertEqual(
            orphans, [], "policy entries with no schema and not internal"
        )

    def test_internal_names_are_not_advertised(self):
        advertised = self.advertised()
        leaked = sorted(set(advertised) & set(INTERNAL_TOOL_NAMES))
        self.assertEqual(leaked, [])

    def test_internal_names_are_documentable(self):
        # The escape hatch stays empty here on purpose: an internal tool is
        # expected to appear only when someone deliberately adds one.
        handled = dispatched_tool_names()
        for name in INTERNAL_TOOL_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, handled, "became dispatchable")
        self.assertNotIn(
            "export_tool_policy", handled | set(self.advertised())
        )

    def test_navigation_tools_are_advertised(self):
        advertised = self.advertised()
        for name in NAVIGATION_TOOL_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, advertised)

    def test_open_url_schema(self):
        parameters = self.advertised()["open_url"]["parameters"]
        self.assertEqual(parameters["required"], ["url"])
        self.assertIs(parameters["additionalProperties"], False)
        url = parameters["properties"]["url"]
        self.assertEqual(url["type"], "string")
        self.assertEqual(url["minLength"], 1)
        self.assertEqual(url["maxLength"], 2048)

    def test_search_site_schema_matches_site_table(self):
        parameters = self.advertised()["search_site"]["parameters"]
        self.assertEqual(parameters["required"], ["site", "query"])
        self.assertIs(parameters["additionalProperties"], False)
        site = parameters["properties"]["site"]
        self.assertEqual(set(site["enum"]), set(SITE_SEARCHES))
        query = parameters["properties"]["query"]
        self.assertEqual(query["type"], "string")
        self.assertEqual(query["minLength"], 1)
        self.assertEqual(query["maxLength"], 300)

    def test_no_argument_table_stays_in_sync(self):
        tree = _execute_function()
        tables = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "no_argument_tools"
                and isinstance(node.value, ast.Dict)
            ):
                tables = {
                    key.value for key in node.value.keys
                    if isinstance(key, ast.Constant)
                }
        self.assertEqual(tables, set(NO_ARGUMENT_TOOL_NAMES))

    def test_contract_tables_are_exact_bidirectionally(self):
        """Constraint tables (e.g. forget/clear or a subset of a dispatch
        table) must not mix keys with values that execute() handles as tool
        names, otherwise an AST inventory cannot tell them apart."""
        handled = dispatched_tool_names()
        for identifier, (keys, values) in constraint_tables().items():
            with self.subTest(table=identifier):
                self.assertEqual(
                    sorted(keys & handled), [],
                    f"{identifier} keys look like handled tool names",
                )
                overlap = sorted(set(values) & handled)
                self.assertLessEqual(
                    len(set(values)), 2,
                    f"{identifier} duplicates a dispatch table",
                )
                for name in overlap:
                    self.assertIn(name, set(values))

    def test_handlers_keep_exact_argument_sets(self):
        registry = self.registry
        result = registry.execute(
            "open_url", {"url": "https://example.org/"}
        )
        self.assertTrue(result["success"], result)

        result = registry.execute(
            "open_url", {"url": "https://example.org/", "extra": 1}
        )
        self.assertFalse(result["success"])
        self.assertIn("exactly one url", result["message"])

        result = registry.execute(
            "open_url", {"url": "http://127.0.0.1/"}
        )
        self.assertFalse(result["success"])

        result = registry.execute(
            "search_site", {"site": "google", "query": "x", "extra": 1}
        )
        self.assertFalse(result["success"])
        self.assertIn("exactly site and query", result["message"])

        result = registry.execute(
            "search_site", {"site": "not-a-site", "query": "x"}
        )
        self.assertFalse(result["success"])

        result = registry.execute(
            "search_site", {"site": "wikipedia", "query": "Turing"}
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(
            self.registry.executor.calls[-1][1],
            "https://en.wikipedia.org/w/index.php?search=Turing",
        )


class AgentVisibilityTests(unittest.TestCase):
    """The agent's per-request filter must not hide navigation tools."""

    @staticmethod
    def agent_class():
        from app.brain.agent import MistyAgent

        return MistyAgent

    def definitions(self):
        return [
            {"type": "function", "function": {"name": name}}
            for name in (
                "open_browser", "open_app", "launch_app",
                "open_url", "search_site", "inspect_screen",
            )
        ]

    def visible(self, transcript):
        names = {
            item["function"]["name"]
            for item in self.agent_class()._tools_for_request(
                transcript, self.definitions()
            )
        }
        return names

    def test_visual_request_keeps_navigation_and_hides_launchers(self):
        visible = self.visible("look at this screen")
        self.assertIn("open_url", visible)
        self.assertIn("search_site", visible)
        self.assertNotIn("open_browser", visible)
        self.assertNotIn("open_app", visible)
        self.assertNotIn("launch_app", visible)

    def test_plain_navigation_request_keeps_everything(self):
        visible = self.visible("search wikipedia for Turing")
        self.assertTrue(
            {"open_url", "search_site", "open_browser"} <= visible
        )


class PlannerNavigationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.executor = FakeExecutor()
        self.fetcher = FakeWebFetcher()
        self.search = FakeWebSearch()
        self.registry = ToolRegistry(
            executor=self.executor,
            memory_store=self.memory,
            web_fetcher=self.fetcher,
            web_search_client=self.search,
        )
        self.definitions = self.registry.available_tools()

    def test_prepare_rejects_unadvertised_tool(self):
        planner = TaskPlanner()
        with self.assertRaises(Exception):
            planner.prepare_structured_plan(
                {
                    "steps": [
                        {
                            "id": "step-1",
                            "tool": "not_a_tool",
                            "arguments": {},
                            "depends_on": [],
                        }
                    ],
                },
                self.definitions,
            )

    def test_compound_navigation_plan_is_accepted_and_dispatched(self):
        planner = TaskPlanner()
        plan = planner.prepare_structured_plan(
            {
                "steps": [
                    {
                        "id": "step-1",
                        "tool": "web_search",
                        "arguments": {"query": "python asyncio tutorial"},
                        "depends_on": [],
                    },
                    {
                        "id": "step-2",
                        "tool": "fetch_web_page",
                                        # Result references address named fields only;
                        # indexes are not part of the planner's grammar.
                        "arguments": {
                            "url": "$steps.step-1.result.url"
                        },
                        "depends_on": ["step-1"],
                    },
                ],
                "response": "",
                "intent_confidence": 0.9,
                "clarification": "",
            },
            self.definitions,
        )
        self.assertEqual([step.name for step in plan.steps],
                         ["web_search", "fetch_web_page"])
        self.assertEqual(plan.steps[1].depends_on, ["step-1"])

        step = planner.start_step(plan, 0)
        arguments = planner.substitute_references(plan, 0)
        first = self.registry.execute(step.name, arguments, confirmed=True)
        planner.finish_step(plan, 0, first)
        self.assertTrue(first["success"], first)
        self.assertEqual(self.search.calls, ["python asyncio tutorial"])

        step = planner.start_step(plan, 1)
        arguments = planner.substitute_references(plan, 1)
        self.assertEqual(arguments["url"], "https://example.org/hit")
        # Scalars only: page text is never lifted into a URL argument.
        self.assertNotIn(
            "Example page text.", json.dumps(arguments)
        )
        second = self.registry.execute(step.name, arguments, confirmed=True)
        planner.finish_step(plan, 1, second)
        self.assertTrue(second["success"], second)
        self.assertEqual(self.fetcher.calls, ["https://example.org/hit"])

    def test_web_search_exposes_first_hit_url_for_compound_plans(self):
        # Result references only fill one text argument, so web_search must
        # provide a scalar URL for a dependent fetch step.
        result = self.registry.execute(
            "web_search", {"query": "python asyncio tutorial"}, confirmed=True
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["url"], "https://example.org/hit")
        self.assertEqual(result["results"][0]["url"], "https://example.org/hit")

    def test_search_site_plan_opens_site_in_browser(self):
        planner = TaskPlanner()
        plan = planner.prepare_structured_plan(
            {
                "steps": [
                    {
                        "id": "step-1",
                        "tool": "search_site",
                        "arguments": {"site": "allegro", "query": "rower"},
                        "depends_on": [],
                    },
                    {
                        "id": "step-2",
                        "tool": "open_url",
                        "arguments": {
                            "url": "$steps.step-1.result.url"
                        },
                        "depends_on": ["step-1"],
                    },
                ],
                "response": "",
                "intent_confidence": 0.9,
                "clarification": "",
            },
            self.definitions,
        )

        planner.start_step(plan, 0)
        first = self.registry.execute(
            "search_site", plan.steps[0].arguments, confirmed=True
        )
        planner.finish_step(plan, 0, first)
        self.assertTrue(first["success"], first)

        step = planner.start_step(plan, 1)
        arguments = planner.substitute_references(plan, 1)
        second = self.registry.execute(step.name, arguments, confirmed=True)
        planner.finish_step(plan, 1, second)
        self.assertTrue(second["success"], second)
        self.assertEqual(
            self.executor.calls,
            [
                (
                    "open_url",
                    "https://allegro.pl/listing?string=rower",
                ),
                (
                    "open_url",
                    "https://allegro.pl/listing?string=rower",
                ),
            ],
        )


class PlannerSignatureTests(unittest.TestCase):
    def test_registry_dispatches_every_advertised_tool_or_is_documented(self):
        """Advertised tools must reach a branch, not the unknown-tool tail."""
        source = TOOLS_PATH.read_text(encoding="utf-8")
        handled = dispatched_tool_names()
        advertised = set()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry = ToolRegistry(
            executor=FakeExecutor(),
            memory_store=MemoryStore(Path(directory.name) / "memory.sqlite3"),
        )
        advertised.update(
            item["function"]["name"] for item in registry.available_tools()
        )
        unhandled = sorted(name for name in advertised if name not in handled)
        self.assertEqual(unhandled, [])
        self.assertTrue(handled)
        self.assertIn("Unknown tool", source)


if __name__ == "__main__":
    unittest.main()
