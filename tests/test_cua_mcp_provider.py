"""Cua protocol/provider boundaries without native desktop or input access."""
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from app.desktop.providers.cua_mcp import CuaMcpProvider, McpStdioTransport
from app.desktop.providers.base import DesktopProviderError


CATALOG = [
    {"name": "list_apps", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "list_windows", "inputSchema": {"type": "object", "properties": {"pid": {"type": "integer"}}, "additionalProperties": False}},
    {"name": "get_window_state", "inputSchema": {"type": "object", "properties": {"pid": {"type": "integer"}, "window_id": {"type": "integer"}}, "required": ["pid", "window_id"], "additionalProperties": False}},
    {"name": "click", "inputSchema": {"type": "object"}},
]


class FakeTransport:
    version = "0.28.0"
    def __init__(self, command, environment=None):
        self.calls = []
        self.closed = False
        self.environment = environment
    def start(self):
        pass
    def request(self, name, args, **kwargs):
        self.calls.append((name, args))
        if name == "initialize":
            return {"protocolVersion": "2025-06-18", "serverInfo": {"version": self.version}}
        if name == "tools/list":
            return {"tools": CATALOG}
        return {"content": [{"type": "text", "text": "private"},
                            {"type": "image", "mimeType": "image/png", "data": "preserved"}],
                "structuredContent": {"snapshot_id": "opaque"}}
    def notify(self, *args, **kwargs):
        pass
    def close(self, **kwargs):
        from app.desktop.providers.base import CleanupOutcome
        self.closed = True
        return CleanupOutcome(True)


class ProviderTests(unittest.TestCase):
    def provider(self):
        p = CuaMcpProvider(["fake"], transport_factory=FakeTransport)
        p.start()
        self.addCleanup(p.close)
        return p

    def test_catalog_preserves_schemas_but_withholds_input(self):
        p = self.provider()
        tools = p.catalog()
        self.assertEqual({t["name"] for t in tools}, {"list_apps", "list_windows", "get_window_state"})
        tools[0]["inputSchema"]["properties"]["hacked"] = {}
        self.assertNotIn("hacked", p.catalog()[0]["inputSchema"]["properties"])

    def test_input_refuses_before_transport_and_no_fake_approval_bypass(self):
        p = self.provider()
        before = len(p._transport.calls)
        with self.assertRaises(DesktopProviderError) as error:
            p.call("click", {"x": 1, "y": 2, "confirmed": True})
        self.assertEqual(error.exception.code, "input_disabled")
        self.assertEqual(len(p._transport.calls), before)

    def test_exact_schema_validation_and_file_output_refusal(self):
        p = self.provider()
        for args in ({"pid": "wrong"}, {"pid": 1, "window_id": 2, "screenshot_out_file": "/tmp/private"}):
            with self.assertRaises(DesktopProviderError):
                p.call("get_window_state", args)

    def test_ordered_images_and_structured_result_are_retained(self):
        p = self.provider()
        result = p.call("get_window_state", {"pid": 1, "window_id": 2})
        self.assertEqual([b["type"] for b in result.content], ["text", "image"])
        self.assertEqual(result.structured, {"snapshot_id": "opaque"})
        self.assertFalse(result.verified)

    def test_process_local_telemetry_and_update_optouts(self):
        p = self.provider()
        self.assertEqual(p._transport.environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"], "0")
        self.assertEqual(p._transport.environment["CUA_DRIVER_RS_UPDATE_CHECK"], "0")

    def test_unreviewed_version_closes_transport(self):
        class WrongVersion(FakeTransport):
            version = "99"
        p = CuaMcpProvider(["fake"], transport_factory=WrongVersion)
        with self.assertRaises(DesktopProviderError):
            p.start()
        self.assertTrue(p._transport.closed)

    def test_closed_provider_cannot_call(self):
        p = self.provider()
        p.close()
        with self.assertRaises(DesktopProviderError):
            p.call("list_apps", {})


class RealPipeTests(unittest.TestCase):
    """Exercise actual owned pipes/processes, never the Cua binary."""
    def transport(self, script, **kwargs):
        t = McpStdioTransport([sys.executable, "-u", "-c", script], **kwargs)
        t.start()
        self.addCleanup(t.close)
        return t

    def test_jsonrpc_roundtrip_and_cleanup(self):
        t = self.transport("import sys,json\nfor line in sys.stdin:\n r=json.loads(line); print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'ok':True}}),flush=True)")
        self.assertEqual(t.request("test", {}, deadline=time.monotonic()+2), {"ok": True})
        self.assertTrue(t.close().stopped)
        self.assertIsNotNone(t.process.poll())

    def test_silent_peer_has_bounded_timeout_and_is_reaped(self):
        t = self.transport("import threading; threading.Event().wait()")
        with self.assertRaises(DesktopProviderError) as error:
            t.request("test", {}, deadline=time.monotonic()+0.15)
        self.assertEqual(error.exception.code, "timeout")
        self.assertIsNotNone(t.process.poll())

    def test_cancel_reaps_owned_process(self):
        t = self.transport("import threading; threading.Event().wait()")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DesktopProviderError) as error:
            t.request("test", {}, cancel=cancel)
        self.assertEqual(error.exception.code, "canceled")
        self.assertIsNotNone(t.process.poll())

    def test_malformed_or_oversized_response_is_rejected(self):
        for payload in ("bad\\n", "x" * 2000 + "\\n"):
            with self.subTest(size=len(payload)):
                t = self.transport("import sys;sys.stdin.readline();sys.stdout.write(" + repr(payload.replace("\\n", "\n")) + ");sys.stdout.flush()", max_message_bytes=1024)
                with self.assertRaises(DesktopProviderError):
                    t.request("test", {}, deadline=time.monotonic()+2)
                self.assertIsNotNone(t.process.poll())

    def test_exited_driver_does_not_leave_term_ignoring_helper(self):
        helper = ("import os,signal,threading; "
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "os.write(int(os.environ['READY_FD']), b'r'); "
                  "threading.Event().wait(5)")
        script = (
            "import sys,json,subprocess,os\n"
            "request=json.loads(sys.stdin.readline())\n"
            "read_fd,write_fd=os.pipe()\n"
            "env=dict(os.environ,READY_FD=str(write_fd))\n"
            f"helper=subprocess.Popen([sys.executable,'-c',{helper!r}],env=env,pass_fds=(write_fd,))\n"
            "os.close(write_fd);os.read(read_fd,1);os.close(read_fd)\n"
            "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'helper':helper.pid}}),flush=True)\n"
            "os._exit(0)\n"
        )
        t = self.transport(script)
        t.request("test", {}, deadline=time.monotonic()+2)
        # Establish direct-child exit without reaping its identity anchor.
        os.waitid(os.P_PID, t.process.pid, os.WEXITED | os.WNOWAIT)
        self.assertTrue(t.close(timeout=1).stopped)
        self.assertTrue(all(not reader.is_alive() for reader in t._readers))
        self.assertIsNotNone(t.process.returncode)

    def test_close_is_idempotent_under_concurrent_callers(self):
        t = self.transport("import threading;threading.Event().wait(5)")
        results = []
        barrier = threading.Barrier(3)
        def close():
            barrier.wait(timeout=1)
            results.append(t.close())
        workers = [threading.Thread(target=close) for _ in range(2)]
        with patch("app.desktop.providers.cua_mcp.os.killpg", wraps=os.killpg) as kill:
            for worker in workers:
                worker.start()
            barrier.wait(timeout=1)
            for worker in workers:
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result.stopped for result in results))
            self.assertTrue(t.close().stopped)
            self.assertEqual(kill.call_count, 2)  # TERM and KILL, once only.

    def test_reaped_external_child_identity_refuses_blind_group_signal(self):
        t = self.transport("pass")
        t.process.wait(timeout=2)  # Deliberately break the ownership anchor.
        with patch("app.desktop.providers.cua_mcp.os.killpg") as kill:
            self.assertFalse(t.close().stopped)
            kill.assert_not_called()

    def test_response_id_mismatch_is_rejected(self):
        t = self.transport("import sys;sys.stdin.readline();print('{\"jsonrpc\":\"2.0\",\"id\":999,\"result\":{}}',flush=True)")
        with self.assertRaises(DesktopProviderError):
            t.request("test", {}, deadline=time.monotonic()+2)


if __name__ == "__main__":
    unittest.main()
