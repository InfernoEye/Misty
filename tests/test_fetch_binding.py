"""Connection-binding (DNS-rebinding) tests for :mod:`app.web.fetch`.

No test performs real DNS, opens a real socket, or speaks HTTP.  ``socket``,
``getaddrinfo`` and the TLS context are replaced with recorders, and the
*real* pinned connection classes, opener and :func:`fetch` loop are
exercised so the tests cover the production code path rather than a mock.
"""

import email.message
import io
import socket
import unittest
import urllib.request
from unittest import mock

import app.web.fetch as fetch_module
from app.web.fetch import (
    BlockedAddressError,
    NetworkError,
    TooManyRedirectsError,
    fetch,
)

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "1.1.1.1"
PRIVATE_IP = "127.0.0.1"


def _addr(ip, port=80):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def _ok(body=b"hello"):
    return _raw_response(
        "HTTP/1.1 200 OK",
        [("Content-Type", "text/plain; charset=utf-8")],
        body,
    )


def _redirect(location):
    return _raw_response("HTTP/1.1 302 Found", [("Location", location)])


def _raw_response(status_line, headers, body=b""):
    lines = [status_line] + [f"{k}: {v}" for k, v in headers]
    head = "\r\n".join(lines).encode("latin-1")
    return head + b"\r\nContent-Length: " + str(len(body)).encode() + (
        b"\r\nConnection: close\r\n\r\n" + body
    )


class RecordingSocket:
    """Fake socket pool recording every connect target and sent request."""

    connects = []
    requests = []
    raw_response = _ok()

    def __init__(self, family, kind):
        self.family = family
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def connect(self, target):
        RecordingSocket.connects.append(target)

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        RecordingSocket.requests.append(data)

    def close(self):
        pass

    def makefile(self, *args):
        return io.BytesIO(RecordingSocket.raw_response)


class RecordingTLSContext:
    """Stand-in for ``ssl.create_default_context()``."""

    instances = []

    def __init__(self):
        self.server_hostnames = []
        RecordingTLSContext.instances.append(self)

    def wrap_socket(self, sock, server_hostname=None):
        self.server_hostnames.append(server_hostname)
        return sock


_MAKEFILE = RecordingSocket.makefile


class BindingTestCase(unittest.TestCase):
    def setUp(self):
        RecordingSocket.connects = []
        RecordingSocket.requests = []
        RecordingTLSContext.instances = []
        self.dns = mock.patch.object(
            fetch_module.socket,
            "getaddrinfo",
            return_value=[_addr(PUBLIC_IP)],
        )
        self.getaddrinfo = self.dns.start()
        self.addCleanup(self.dns.stop)

        sock_patch = mock.patch.object(
            fetch_module.socket, "socket", RecordingSocket
        )
        sock_patch.start()
        self.addCleanup(sock_patch.stop)

        tls_patch = mock.patch.object(
            fetch_module,
            "_default_ssl_context",
            RecordingTLSContext,
        )
        tls_patch.start()
        self.addCleanup(tls_patch.stop)

    def use_dns(self, *answers):
        self.getaddrinfo.side_effect = None
        self.getaddrinfo.return_value = None
        self.getaddrinfo.side_effect = list(answers)
        return self.getaddrinfo

    def script_responses(self, *raw_responses):
        """Serve one raw HTTP response per connection, in order."""
        queue = list(raw_responses)

        def makefile(sock, *args):
            if not queue:
                raise AssertionError("unexpected extra connection")
            return io.BytesIO(queue.pop(0))

        RecordingSocket.makefile = makefile
        self.addCleanup(setattr, RecordingSocket, "makefile", _MAKEFILE)
        return queue

    def host_header(self, index=0):
        for line in RecordingSocket.requests[index].split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                return line.split(b":", 1)[1].strip().decode("latin-1")
        return None


class RebindingTests(BindingTestCase):
    def test_public_then_private_answer_never_connects_to_private(self):
        # Simulates the old race: if a second lookup happened at connect time
        # it would see the private answer.  The pinned connection must not
        # ask DNS again.
        self.use_dns([_addr(PUBLIC_IP)], [_addr(PRIVATE_IP)])
        result = fetch("http://example.com/")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.text, "hello")
        self.assertEqual(
            self.getaddrinfo.call_args_list,
            [mock.call("example.com", 80, type=socket.SOCK_STREAM)],
        )
        self.assertEqual(RecordingSocket.connects, [(PUBLIC_IP, 80)])
        self.assertNotIn((PRIVATE_IP, 80), RecordingSocket.connects)

    def test_private_first_public_second_is_refused_before_connect(self):
        self.use_dns([_addr(PRIVATE_IP)], [_addr(PUBLIC_IP)])
        with self.assertRaises(BlockedAddressError):
            fetch("http://example.com/")
        self.assertEqual(RecordingSocket.connects, [])
        self.assertEqual(len(self.getaddrinfo.call_args_list), 1)

    def test_mixed_answers_refuse_whole_host(self):
        self.use_dns([_addr(PUBLIC_IP), _addr(PRIVATE_IP)])
        with self.assertRaises(BlockedAddressError):
            fetch("http://example.com/")
        self.assertEqual(RecordingSocket.connects, [])

    def test_only_validated_address_is_used_when_several_are_public(self):
        self.use_dns([_addr(PUBLIC_IP), _addr(OTHER_PUBLIC_IP)])
        fetch("http://example.com/")
        self.assertEqual(len(RecordingSocket.connects), 1)
        self.assertIn(RecordingSocket.connects[0], {
            (PUBLIC_IP, 80),
            (OTHER_PUBLIC_IP, 80),
        })

    def test_resolution_happens_once_per_hop(self):
        self.use_dns([_addr(PUBLIC_IP)], [_addr(OTHER_PUBLIC_IP)])
        self.script_responses(
            _redirect("http://other.example/next"),
            _ok(b"landed"),
        )
        result = fetch("http://example.com/start")
        self.assertEqual(result.final_url, "http://other.example/next")
        self.assertEqual(result.text, "landed")
        self.assertEqual(
            self.getaddrinfo.call_args_list,
            [
                mock.call("example.com", 80, type=socket.SOCK_STREAM),
                mock.call("other.example", 80, type=socket.SOCK_STREAM),
            ],
        )
        self.assertEqual(
            RecordingSocket.connects,
            [(PUBLIC_IP, 80), (OTHER_PUBLIC_IP, 80)],
        )
        self.assertEqual(len(self.getaddrinfo.call_args_list), 2)

    def test_redirect_to_private_literal_makes_no_connection(self):
        self.script_responses(_redirect("http://127.0.0.1/secret"))
        with self.assertRaises(BlockedAddressError):
            fetch("http://example.com/start")
        self.assertEqual(RecordingSocket.connects, [(PUBLIC_IP, 80)])

    def test_redirect_to_rebound_hostname_is_refused(self):
        # The redirect target resolves to a private address only on its own
        # (second) lookup; the first hop must never be retried and the
        # private answer must never be connected to.
        self.use_dns([_addr(PUBLIC_IP)], [_addr(PRIVATE_IP)])
        self.script_responses(_redirect("http://internal.example/x"))
        with self.assertRaises(BlockedAddressError):
            fetch("http://example.com/start")
        self.assertEqual(RecordingSocket.connects, [(PUBLIC_IP, 80)])
        self.assertEqual(len(self.getaddrinfo.call_args_list), 2)

    def test_redirect_limit_still_applies(self):
        self.script_responses(*([_redirect("/next")] * 4))
        with self.assertRaises(TooManyRedirectsError):
            fetch("http://example.com/start", max_redirects=2)
        self.assertEqual(len(RecordingSocket.connects), 3)


class HostAndTlsTests(BindingTestCase):
    def test_http_host_header_uses_original_hostname(self):
        fetch("http://example.com:80/page")
        self.assertEqual(RecordingSocket.connects, [(PUBLIC_IP, 80)])
        # urllib keeps an explicit ``:80`` in the Host header.
        self.assertEqual(self.host_header(), "example.com:80")
        self.assertEqual(RecordingTLSContext.instances, [])

    def test_https_wraps_socket_with_original_server_hostname(self):
        result = fetch("https://example.org/secure")
        self.assertEqual(result.status, 200)
        self.assertEqual(RecordingSocket.connects, [(PUBLIC_IP, 443)])
        self.assertEqual(len(RecordingTLSContext.instances), 2)  # + opener
        context = RecordingTLSContext.instances[-1]
        self.assertEqual(context.server_hostnames, ["example.org"])
        self.assertNotIn(PUBLIC_IP, context.server_hostnames)
        self.assertEqual(self.host_header(), "example.org")

    def test_default_tls_context_is_create_default_context(self):
        # The handler's context comes from the same factory, which is
        # ssl.create_default_context (verifying, SNI-capable).
        with mock.patch.object(
            fetch_module,
            "_default_ssl_context",
            RecordingTLSContext,
        ):
            handler = fetch_module._PinnedHTTPSHandler()
            context = handler._context_factory()
        self.assertIsInstance(context, RecordingTLSContext)

    def test_no_getaddrinfo_after_connect(self):
        # A second lookup through socket.create_connection would show up in
        # the call list; the fake pool records the only connect target.
        with mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("create_connection used"),
        ):
            fetch("https://example.org/")
        self.assertEqual(len(self.getaddrinfo.call_args_list), 1)
        self.assertEqual(RecordingSocket.connects, [(PUBLIC_IP, 443)])

    def test_ip_literal_url_uses_literal_for_host_and_sni(self):
        fetch(f"https://{PUBLIC_IP}/")
        self.getaddrinfo.assert_not_called()
        self.assertEqual(RecordingSocket.connects, [(PUBLIC_IP, 443)])
        self.assertEqual(
            RecordingTLSContext.instances[-1].server_hostnames, [PUBLIC_IP]
        )
        self.assertEqual(self.host_header(), PUBLIC_IP)


class OpenerPolicyTests(BindingTestCase):
    def _opener(self, url="http://example.com/"):
        return fetch_module._build_opener(
            fetch_module._resolve_destination(url)
        )

    def test_environment_proxies_are_ignored(self):
        with mock.patch.dict(
            "os.environ",
            {
                "http_proxy": "http://10.0.0.1:3128",
                "https_proxy": "http://10.0.0.1:3128",
                "HTTP_PROXY": "http://10.0.0.1:3128",
                "no_proxy": "",
                "all_proxy": "socks5://10.0.0.1:1080",
            },
        ):
            opener = self._opener()
        handlers = [
            handler
            for handler in opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].proxies, {})

    def test_single_no_redirect_handler(self):
        opener = self._opener()
        redirects = [
            handler
            for handler in opener.handlers
            if isinstance(handler, fetch_module._NoRedirectHandler)
        ]
        self.assertEqual(len(redirects), 1)
        self.assertIsNone(
            redirects[0].redirect_request(
                None, None, 302, "Found", {}, "http://127.0.0.1/"
            )
        )

    def test_opener_uses_pinned_handlers(self):
        http_opener = self._opener("http://example.com/")
        self.assertTrue(
            any(
                isinstance(
                    handler, fetch_module._UpgradeAwareHTTPHandler
                )
                for handler in http_opener.handlers
            )
        )
        https_opener = self._opener("https://example.com/")
        self.assertTrue(
            any(
                isinstance(handler, fetch_module._PinnedHTTPSHandler)
                for handler in https_opener.handlers
            )
        )

    def test_unpinned_or_rewritten_request_is_refused(self):
        opener = self._opener()
        with self.assertRaises(fetch_module.urllib.error.URLError):
            opener.open("http://example.com/", timeout=1.0)
        destination = fetch_module._resolve_destination("http://example.com/")
        other = fetch_module._resolve_destination("http://evil.example/")
        request = fetch_module._pinned_request(other, 1.0)
        opener = fetch_module._build_opener(destination)
        with self.assertRaises(fetch_module.urllib.error.URLError):
            opener.open(request, timeout=1.0)
        self.assertEqual(RecordingSocket.connects, [])

    def test_connection_class_never_calls_create_connection(self):
        destination = fetch_module._resolve_destination("http://example.com/")
        connection = fetch_module._PinnedHTTPConnection(
            "example.com", 80, destination, timeout=1.0
        )
        with mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("DNS-based connect used"),
        ):
            connection.connect()
        self.assertEqual(RecordingSocket.connects, [(PUBLIC_IP, 80)])
        connection.close()

    def test_unadopted_proxy_argument_is_ignored(self):
        # Passing proxy= without opting out of "proxies disabled" must not
        # quietly enable a proxy.
        destination = fetch_module._resolve_destination("http://example.com/")
        opener = fetch_module._build_opener(destination, proxy="proxy.example")
        proxies = [
            handler.proxies
            for handler in opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertEqual(proxies, [{}])

    def test_explicit_proxy_is_still_pinned_and_guarded(self):
        destination = fetch_module._resolve_destination("http://example.com/")
        opener = fetch_module._build_opener(
            destination, proxy="proxy.example", proxy_disabled=False
        )
        proxies = [
            handler.proxies
            for handler in opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertEqual(proxies, [{"proxy.example": "http://example.com/"}])
        # The guard runs first: an unpinned or rewritten request is refused
        # before any handler could send it to the proxy.
        with self.assertRaises(fetch_module.urllib.error.URLError):
            opener.open(
                fetch_module._build_request("http://127.0.0.1/"), timeout=1.0
            )
        self.assertEqual(RecordingSocket.connects, [])


class BlockedLiteralTests(BindingTestCase):
    def test_loopback_and_metadata_literals_are_refused(self):
        for url in (
            "http://127.0.0.1/",
            "https://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[fe80::1]/",
            "http://10.0.0.1/",
            "http://0.0.0.0/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(BlockedAddressError):
                    fetch(url)
        self.assertEqual(RecordingSocket.connects, [])
        self.getaddrinfo.assert_not_called()

    def test_non_canonical_numeric_hosts_are_refused(self):
        for url in (
            "http://127.1/",
            "http://2130706433/",
            "http://0x7f000001/",
            "http://0177.0.0.1/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(fetch_module.InvalidURLError):
                    fetch(url)
        self.getaddrinfo.assert_not_called()
        self.assertEqual(RecordingSocket.connects, [])

    def test_dns_failure_is_network_error_without_connect(self):
        self.use_dns(socket.gaierror(-2, "Name or service not known"))
        with self.assertRaises(NetworkError):
            fetch("http://missing.example/")
        self.assertEqual(RecordingSocket.connects, [])


if __name__ == "__main__":
    unittest.main()
