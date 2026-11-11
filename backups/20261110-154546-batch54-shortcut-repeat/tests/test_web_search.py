"""Tests for the isolated, loopback-only SearXNG JSON search client.

Every test replaces the opener's network call and installs DNS/socket guards.
No test resolves a host, opens a socket, contacts a server, or loads a model.
"""

import email.message
import importlib
import io
import json
import socket
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

search_module = importlib.import_module("app.web.search")
SearxngClient = search_module.SearxngClient
InvalidBaseURLError = search_module.InvalidBaseURLError


def _json_body(payload):
    return json.dumps(payload).encode("utf-8")


class FakeResponse:
    """Minimal stand-in for an http.client response."""

    def __init__(self, body=b"", status=200, headers=None, read_error=None):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)
        self.read_error = read_error
        self.closed = False
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        remaining = len(self._body) - self._offset
        if size is None or size < 0:
            size = remaining
        else:
            size = min(size, remaining)
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True


class SearxngClientTestCase(unittest.TestCase):
    def setUp(self):
        dns_guard = mock.patch(
            "socket.getaddrinfo",
            side_effect=AssertionError("DNS is disabled in tests"),
        )
        dns_guard.start()
        self.addCleanup(dns_guard.stop)

        socket_guard = mock.patch(
            "socket.create_connection",
            side_effect=AssertionError("network is disabled in tests"),
        )
        socket_guard.start()
        self.addCleanup(socket_guard.stop)

        opener_patch = mock.patch.object(search_module._OPENER, "open")
        self.open_mock = opener_patch.start()
        self.addCleanup(opener_patch.stop)
        self.open_mock.return_value = FakeResponse(
            body=_json_body({"results": []})
        )

    def respond(self, payload=None, *, body=None, status=200, headers=None,
                read_error=None):
        self.open_mock.side_effect = None
        if body is None:
            body = _json_body({"results": []} if payload is None else payload)
        response = FakeResponse(
            body=body,
            status=status,
            headers=headers,
            read_error=read_error,
        )
        self.open_mock.return_value = response
        return response

    def assert_failure(self, result, code):
        self.assertIsInstance(result, dict)
        self.assertEqual(set(result), {"success", "message", "error"})
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], code)
        self.assertIsInstance(result["message"], str)
        self.assertTrue(result["message"])

    def assert_success(self, result):
        self.assertEqual(
            set(result),
            {
                "success",
                "query",
                "results",
                "answers",
                "count",
                "limit",
                "message",
            },
        )
        self.assertTrue(result["success"])
        self.assertIsInstance(result["message"], str)
        self.assertEqual(result["count"], len(result["results"]))

    # -- base URL validation -------------------------------------------------

    def test_valid_loopback_base_urls_are_normalized(self):
        cases = [
            ("http://127.0.0.1:8888", "http://127.0.0.1:8888"),
            ("http://127.0.0.1:8888/", "http://127.0.0.1:8888"),
            ("http://localhost:8888/", "http://localhost:8888"),
            ("http://[::1]:8888", "http://[::1]:8888"),
            ("HTTP://LOCALHOST:8888/", "http://localhost:8888"),
            ("https://localhost:443", "https://localhost:443"),
            ("http://[::1]", "http://[::1]"),
            ("http://localhost", "http://localhost"),
            ("http://127.0.0.1:1", "http://127.0.0.1:1"),
            ("http://localhost:65535/", "http://localhost:65535"),
            ("http://LOCALHOST/", "http://localhost"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                client = SearxngClient(raw)
                self.assertEqual(client.base_url, expected)
                self.assertFalse(client.base_url.endswith("/"))

    def test_invalid_base_urls_are_rejected(self):
        bad_urls = [
            "",
            "   ",
            None,
            123,
            b"http://localhost:8888",
            "http://example.com",
            "https://example.net:8888",
            "http://192.168.1.10:8888",
            "http://127.0.0.2:8888",
            "http://[::2]:8888",
            "http://localhost.localdomain:8888",
            "http://user:pass@localhost:8888",
            "http://user@localhost:8888",
            "http://@localhost:8888",
            "http://localhost:8888/search",
            "http://localhost:8888/searxng/",
            "http://localhost:8888//",
            "http://localhost:8888?x=1",
            "http://localhost:8888?",
            "http://localhost:8888#frag",
            "http://localhost:8888#",
            "ftp://localhost:8888",
            "file:///tmp/searxng",
            "localhost:8888",
            "//localhost:8888",
            "http://localhost:0",
            "http://localhost:65536",
            "http://localhost:-1",
            "http://localhost:abc",
            "http://localhost:/",
            "http://",
            "http:///",
            "http://localhost:8888 extra",
            "http://localhost:8888/\x00",
        ]
        for raw in bad_urls:
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidBaseURLError):
                    SearxngClient(raw)
                with self.assertRaises(ValueError):
                    SearxngClient(raw)

    def test_invalid_base_url_is_rechecked_before_network(self):
        client = SearxngClient()
        client.base_url = "http://example.com:8888"
        result = client.search("hello")
        self.assert_failure(result, "invalid_base_url")
        self.open_mock.assert_not_called()

    # -- request validation --------------------------------------------------

    def test_query_validation_happens_before_network(self):
        client = SearxngClient()
        bad_queries = [
            None,
            "",
            "   ",
            "x" * 301,
            "bad\x00query",
            42,
            b"bytes",
            ["list"],
            "\ud800",
        ]
        for query in bad_queries:
            with self.subTest(query=query):
                result = client.search(query)
                self.assert_failure(result, "invalid_query")
        self.open_mock.assert_not_called()

    def test_query_length_boundary_is_accepted(self):
        client = SearxngClient()
        response = self.respond()
        result = client.search("x" * 300)
        self.assert_success(result)
        self.assertEqual(len(result["query"]), 300)
        self.assertTrue(response.closed)

    def test_limit_validation_happens_before_network(self):
        client = SearxngClient()
        bad_limits = [0, -1, 21, 100, "5", 5.0, True, False, 5.5, [5]]
        for limit in bad_limits:
            with self.subTest(limit=limit):
                result = client.search("query", limit=limit)
                self.assert_failure(result, "invalid_limit")
        self.open_mock.assert_not_called()

    def test_default_limit_uses_max_results_capped_at_ten(self):
        for max_results, expected in [(3, 3), (10, 10), (20, 10)]:
            with self.subTest(max_results=max_results):
                client = SearxngClient(max_results=max_results)
                self.respond()
                result = client.search("query")
                self.assert_success(result)
                self.assertEqual(result["limit"], expected)

    def test_explicit_limit_is_not_capped_by_max_results(self):
        client = SearxngClient(max_results=3)
        self.respond()
        result = client.search("query", limit=5)
        self.assert_success(result)
        self.assertEqual(result["limit"], 5)

    def test_categories_and_language_validation_before_network(self):
        client = SearxngClient()
        bad_options = [
            {"categories": "general;rm"},
            {"categories": "gen eral"},
            {"categories": "general,,news"},
            {"categories": "x" * 65},
            {"categories": 123},
            {"language": "en US"},
            {"language": "en;fr"},
            {"language": "x" * 65},
            {"language": 5},
        ]
        for options in bad_options:
            with self.subTest(options=options):
                result = client.search("query", **options)
                self.assert_failure(result, "invalid_query")
        self.open_mock.assert_not_called()

    def test_valid_optional_parameters_are_sent(self):
        client = SearxngClient()
        self.respond()
        result = client.search(
            "misty search",
            categories="general,news",
            language="en-US",
        )
        self.assert_success(result)
        request = self.open_mock.call_args.args[0]
        parsed = urllib.parse.urlsplit(request.full_url)
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(params["q"], ["misty search"])
        self.assertEqual(params["format"], ["json"])
        self.assertEqual(params["safesearch"], ["1"])
        self.assertEqual(params["categories"], ["general,news"])
        self.assertEqual(params["language"], ["en-US"])
        self.assertEqual(self.open_mock.call_args.kwargs["timeout"], 10.0)

    # -- successful parsing --------------------------------------------------

    def test_successful_parse_dedupes_and_parses_answers(self):
        payload = {
            "results": [
                {
                    "url": "https://example.com/a",
                    "title": "  Alpha  ",
                    "content": "  First snippet  ",
                },
                {
                    "url": "https://example.com/a",
                    "title": "Duplicate",
                    "content": "Duplicate",
                },
                {
                    "url": "http://example.org/b",
                    "title": "Beta",
                    "snippet": "Second snippet",
                },
                {"url": "ftp://example.net/c", "title": "FTP", "content": "x"},
                {
                    "url": "javascript:alert(1)",
                    "title": "JavaScript",
                    "content": "x",
                },
                {
                    "url": "http://user:secret@example.com/d",
                    "title": "Credentials",
                    "content": "x",
                },
                {"url": "/relative", "title": "Relative", "content": "x"},
                "not a dict",
                {
                    "url": "https://example.com/e",
                    "title": "Epsilon",
                    "content": "Third snippet",
                },
            ],
            "answers": ["Answer one", "Answer two", 5, "   "],
        }
        self.respond(payload)
        result = SearxngClient().search("  misty search  ")

        self.assert_success(result)
        self.assertEqual(result["query"], "misty search")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["limit"], 10)
        self.assertEqual(result["answers"], ["Answer one", "Answer two"])
        self.assertEqual(
            [item["url"] for item in result["results"]],
            [
                "https://example.com/a",
                "http://example.org/b",
                "https://example.com/e",
            ],
        )
        self.assertEqual(result["results"][0]["title"], "Alpha")
        self.assertEqual(result["results"][0]["snippet"], "First snippet")
        self.assertEqual(result["results"][1]["snippet"], "Second snippet")
        for item in result["results"]:
            self.assertEqual(set(item), {"title", "url", "snippet"})
        self.assertIsInstance(json.dumps(result), str)

    def test_empty_results_is_success(self):
        self.respond({"results": []})
        result = SearxngClient().search("query")
        self.assert_success(result)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["answers"], [])
        self.assertEqual(result["count"], 0)

    def test_limit_truncates_after_deduping(self):
        items = [
            {
                "url": f"https://example.com/{index}",
                "title": f"Title {index}",
                "content": f"Snippet {index}",
            }
            for index in range(10)
        ]
        items.insert(
            1,
            {
                "url": "https://example.com/0",
                "title": "Duplicate",
                "content": "Duplicate",
            },
        )
        self.respond({"results": items})
        result = SearxngClient().search("query", limit=5)
        self.assert_success(result)
        self.assertEqual(result["count"], 5)
        self.assertEqual(
            [item["url"] for item in result["results"]],
            [f"https://example.com/{index}" for index in range(5)],
        )

    def test_result_fields_are_bounded(self):
        prefix = "http://example.com/"
        accepted_url = prefix + "a" * (
            search_module.MAX_URL_LENGTH - len(prefix)
        )
        rejected_url = prefix + "b" * (
            search_module.MAX_URL_LENGTH - len(prefix) + 1
        )
        self.assertEqual(len(accepted_url), search_module.MAX_URL_LENGTH)
        self.assertGreater(len(rejected_url), search_module.MAX_URL_LENGTH)
        self.respond(
            {
                "results": [
                    {
                        "url": accepted_url,
                        "title": "T" * 350,
                        "content": "S" * 1200,
                    },
                    {
                        "url": rejected_url,
                        "title": "Too long URL",
                        "content": "Dropped",
                    },
                ]
            }
        )
        result = SearxngClient().search("query")
        self.assert_success(result)
        self.assertEqual(result["count"], 1)
        first = result["results"][0]
        self.assertEqual(len(first["title"]), search_module.MAX_TITLE_LENGTH)
        self.assertEqual(
            len(first["snippet"]), search_module.MAX_SNIPPET_LENGTH
        )
        self.assertEqual(len(first["url"]), search_module.MAX_URL_LENGTH)

    def test_results_missing_required_text_fields_are_dropped(self):
        self.respond(
            {
                "results": [
                    {"url": "https://example.com/only-url"},
                    {"url": "https://example.com/no-content", "title": "T"},
                    {"url": "https://example.com/no-title", "content": "C"},
                    {
                        "url": "https://example.com/empty-text",
                        "title": "",
                        "content": "",
                    },
                    {
                        "url": "https://example.com/valid",
                        "title": "T",
                        "content": "C",
                    },
                ]
            }
        )
        result = SearxngClient().search("query")
        self.assert_success(result)
        self.assertEqual(
            [item["url"] for item in result["results"]],
            [
                "https://example.com/empty-text",
                "https://example.com/valid",
            ],
        )

    def test_answers_are_bounded(self):
        answers = ["A" * 2000]
        answers.extend(f"answer-{index}" for index in range(10))
        answers.extend([123, None])
        self.respond({"results": [], "answers": answers})
        result = SearxngClient().search("query")
        self.assert_success(result)
        self.assertEqual(len(result["answers"]), search_module.MAX_ANSWERS)
        self.assertEqual(
            len(result["answers"][0]), search_module.MAX_ANSWER_LENGTH
        )
        self.assertEqual(result["answers"][1], "answer-0")
        self.assertNotIn("answer-4", result["answers"])

    def test_non_list_answers_are_ignored(self):
        self.respond({"results": [], "answers": "not a list"})
        result = SearxngClient().search("query")
        self.assert_success(result)
        self.assertEqual(result["answers"], [])

    def test_result_urls_are_filtered(self):
        payload = {
            "results": [
                {"url": "https://good.example/a", "title": "A", "content": "A"},
                {
                    "url": "HTTP://UPPER.EXAMPLE/b",
                    "title": "B",
                    "content": "B",
                },
                {"url": "ftp://good.example/c", "title": "C", "content": "C"},
                {
                    "url": "javascript:alert(1)",
                    "title": "D",
                    "content": "D",
                },
                {
                    "url": "data:text/html,hello",
                    "title": "E",
                    "content": "E",
                },
                {
                    "url": "http://user:pass@good.example/f",
                    "title": "F",
                    "content": "F",
                },
                {
                    "url": "http://user@good.example/g",
                    "title": "G",
                    "content": "G",
                },
                {"url": "//good.example/h", "title": "H", "content": "H"},
                {"url": "http://", "title": "I", "content": "I"},
                {
                    "url": "http://good.example:99999/j",
                    "title": "J",
                    "content": "J",
                },
                {
                    "url": "http://good.example:0/j0",
                    "title": "J0",
                    "content": "J0",
                },
                {
                    "url": "http://good.example/\x00",
                    "title": "K",
                    "content": "K",
                },
                {"url": "/relative", "title": "L", "content": "L"},
                {"url": 123, "title": "M", "content": "M"},
                {"url": None, "title": "N", "content": "N"},
                {
                    "url": "http://good.example/" + "a" * 2049,
                    "title": "O",
                    "content": "O",
                },
            ]
        }
        self.respond(payload)
        result = SearxngClient().search("query", limit=20)
        self.assert_success(result)
        self.assertEqual(
            [item["url"] for item in result["results"]],
            ["https://good.example/a", "HTTP://UPPER.EXAMPLE/b"],
        )

    # -- malformed responses -------------------------------------------------

    def test_malformed_json_and_shapes_are_invalid_responses(self):
        bodies = [
            b"{not json",
            b"",
            b"[]",
            b"null",
            b'"text"',
            b"{}",
            b'{"results": null}',
            b'{"results": {}}',
            b'{"results": "x"}',
            b"\xff",
        ]
        client = SearxngClient()
        for body in bodies:
            with self.subTest(body=body):
                response = self.respond(body=body)
                result = client.search("query")
                self.assert_failure(result, "invalid_response")
                self.assertTrue(response.closed)

    def test_unusable_result_data_is_invalid_response(self):
        self.respond({"results": [{"url": "https://example.com"}]})
        self.open_mock.return_value.status = object()
        self.open_mock.return_value.getcode = lambda: object()
        result = SearxngClient().search("query")
        self.assert_failure(result, "invalid_response")

    # -- HTTP failures -------------------------------------------------------

    def test_non_200_status_is_http_error(self):
        client = SearxngClient()
        for status in (204, 301, 400, 403, 500, 503):
            with self.subTest(status=status):
                response = self.respond(
                    {"results": []}, status=status
                )
                result = client.search("query")
                self.assert_failure(result, "http_error")
                self.assertIn(str(status), result["message"])
                self.assertTrue(response.closed)

    def test_http_error_exception_is_mapped_without_body_leak(self):
        headers = email.message.Message()
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8888/search",
            502,
            "Bad Gateway",
            headers,
            io.BytesIO(b"secret upstream text"),
        )
        self.open_mock.side_effect = error
        result = SearxngClient().search("query")
        self.assert_failure(result, "http_error")
        self.assertIn("502", result["message"])
        self.assertNotIn("secret upstream text", json.dumps(result))

    # -- size limits ---------------------------------------------------------

    def test_declared_oversized_content_length_is_rejected_unread(self):
        response = self.respond(
            {"results": []},
            headers={
                "Content-Length": str(search_module.DEFAULT_MAX_BYTES + 1)
            },
        )
        result = SearxngClient().search("query")
        self.assert_failure(result, "response_too_large")
        self.assertEqual(response.read_sizes, [])
        self.assertTrue(response.closed)

    def test_streamed_oversized_body_is_rejected(self):
        body = b"x" * (search_module.DEFAULT_MAX_BYTES + 1)
        response = self.respond(body=body)
        result = SearxngClient().search("query")
        self.assert_failure(result, "response_too_large")
        self.assertTrue(response.closed)
        self.assertTrue(response.read_sizes)
        self.assertTrue(all(size <= 64 * 1024 for size in response.read_sizes))

    def test_malformed_content_length_is_ignored(self):
        self.respond(
            {"results": []},
            headers={"Content-Length": "not-a-number"},
        )
        result = SearxngClient().search("query")
        self.assert_success(result)

    # -- network and read failures ------------------------------------------

    def test_network_failures_are_sanitized(self):
        failures = [
            urllib.error.URLError("secret upstream reason"),
            TimeoutError("secret timeout"),
            socket.timeout("secret socket timeout"),
            OSError("secret os error"),
            RuntimeError("secret unexpected error"),
        ]
        client = SearxngClient()
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.open_mock.side_effect = failure
                result = client.search("private query")
                self.assert_failure(result, "network_error")
                rendered = json.dumps(result)
                self.assertNotIn("secret", rendered)
                self.assertNotIn("private query", rendered)

    def test_read_transport_failure_maps_to_network_error(self):
        response = self.respond(
            {"results": []}, read_error=OSError("secret read failure")
        )
        result = SearxngClient().search("query")
        self.assert_failure(result, "network_error")
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(response.closed)

    def test_non_byte_read_is_invalid_response(self):
        response = self.respond(body="not bytes")
        result = SearxngClient().search("query")
        self.assert_failure(result, "invalid_response")
        self.assertTrue(response.closed)

    # -- transport configuration --------------------------------------------

    def test_opener_uses_empty_proxy_handler_and_no_redirects(self):
        proxy_handlers = [
            handler
            for handler in search_module._OPENER.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertTrue(proxy_handlers)
        for handler in proxy_handlers:
            self.assertEqual(handler.proxies, {})
        self.assertTrue(
            any(
                isinstance(handler, search_module._NoRedirectHandler)
                for handler in search_module._OPENER.handlers
            )
        )

    def test_request_is_get_json_without_cookies_or_auth(self):
        self.respond()
        result = SearxngClient().search("hello")
        self.assert_success(result)
        request = self.open_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.headers.get("Accept-encoding"), "identity")
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Proxy-Authorization"))
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("Authorization", request.headers)

    # -- constructor validation ---------------------------------------------

    def test_timeout_configuration_is_validated(self):
        for timeout in [0, -1, "10", None, True, float("inf"), float("nan")]:
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    SearxngClient(timeout=timeout)
        self.assertEqual(SearxngClient(timeout=2.5).timeout, 2.5)

    def test_max_results_configuration_is_validated(self):
        for max_results in [0, 21, 1.5, "10", True, None]:
            with self.subTest(max_results=max_results):
                with self.assertRaises(ValueError):
                    SearxngClient(max_results=max_results)

    # -- documentation contract ---------------------------------------------

    def test_results_are_documented_as_untrusted(self):
        module_doc = (search_module.__doc__ or "").lower()
        search_doc = (SearxngClient.search.__doc__ or "").lower()
        self.assertIn("untrusted", module_doc)
        self.assertIn("never", module_doc)
        self.assertIn("untrusted", search_doc)
        self.assertIn("never", search_doc)


if __name__ == "__main__":
    unittest.main()
