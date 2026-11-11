"""Non-capture health is narrower than the driver's general health tool."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.desktop.providers.base import DesktopProviderError
from app.desktop.providers.cua_mcp import CuaMcpProvider, NONCAPTURE_HEALTH_CHECKS
from scripts.check_desktop_v2 import probe, environment_health
from tests.test_cua_mcp_provider import FakeTransport
from tests.test_check_desktop_v2 import FakeProvider, outcome


class HealthTransport(FakeTransport):
    def request(self, name, args, **kwargs):
        result = super().request(name, args, **kwargs)
        if name == "tools/list":
            result = {"tools": [*result["tools"], {"name": "health_report", "inputSchema": {
                "type": "object", "properties": {"include": {"type": "array", "items": {"type": "string"}},
                "skip": {"type": "array"}}, "additionalProperties": False}}]}
        return result


class ProviderHealthTests(unittest.TestCase):
    def test_capture_checks_empty_include_and_skip_cannot_reach_driver(self):
        p = CuaMcpProvider(["fake"], transport_factory=HealthTransport)
        p.start()
        self.addCleanup(p.close)
        for arguments in ({}, {"include": []}, {"include": ["screen_capture_capability"]},
                          {"skip": ["screen_capture_capability"]}, {"include": ["session_active", "session_active"]},
                          {"include": [True]}, {"include": list(NONCAPTURE_HEALTH_CHECKS), "skip": []}):
            with self.subTest(arguments=arguments):
                before = len(p._transport.calls)
                with self.assertRaises(DesktopProviderError):
                    p.call("health_report", arguments)
                self.assertEqual(len(p._transport.calls), before)
        p.call("health_report", {"include": list(NONCAPTURE_HEALTH_CHECKS)})
        self.assertEqual(p._transport.calls[-1][1]["arguments"], {"include": list(NONCAPTURE_HEALTH_CHECKS)})

    def test_optional_health_tool_does_not_break_older_catalog(self):
        p = CuaMcpProvider(["fake"], transport_factory=FakeTransport)
        p.start()
        self.addCleanup(p.close)
        with self.assertRaises(DesktopProviderError) as error:
            p.call("health_report", {"include": list(NONCAPTURE_HEALTH_CHECKS)})
        self.assertEqual(error.exception.code, "tool_unavailable")


class DiagnosticHealthTests(unittest.TestCase):
    def test_health_has_only_safe_statuses_not_private_messages(self):
        class HealthProvider(FakeProvider):
            def catalog(self):
                return [*super().catalog(), {"name": "health_report"}]
            def call(self, name, arguments, **kwargs):
                if name != "health_report":
                    return super().call(name, arguments, **kwargs)
                self.calls.append((name, arguments, kwargs))
                return outcome(name, {"schema_version": "1", "checks": [
                    {"name": check, "status": "pass", "message": "PRIVATE-HEALTH-DATA", "hint": "/private/path"}
                    for check in NONCAPTURE_HEALTH_CHECKS]})
        provider = HealthProvider()
        summary, code = probe(provider_factory=lambda: provider, health=True,
                              environment_probe=lambda **kwargs: {"desktop_family": "kde"})
        self.assertEqual(code, 0)
        self.assertFalse(summary["input_ready"])
        self.assertEqual(summary["noncapture_health"]["status"], "reported")
        self.assertNotIn("PRIVATE-HEALTH-DATA", json.dumps(summary))
        self.assertNotIn("/private/path", json.dumps(summary))
        self.assertTrue(provider.closed)
        names = [name for name, _, _ in provider.calls]
        self.assertEqual(names, ["health_report", "list_windows"])

    def test_health_unavailable_does_not_invent_a_pass(self):
        summary, _ = probe(provider_factory=FakeProvider, health=True,
                           environment_probe=lambda **kwargs: {})
        self.assertEqual(summary["noncapture_health"]["status"], "unavailable")

    def test_session_helpers_are_queried_read_only_and_optin_is_not_enabled(self):
        before = {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland"}
        with patch("scripts.check_desktop_v2.os.environ", before), \
             patch("scripts.check_desktop_v2.Path.read_text", return_value="bwrap\n"), \
             patch("scripts.check_desktop_v2.shutil.which", return_value="/usr/bin/gdbus"), \
             patch("scripts.check_desktop_v2.subprocess.run", side_effect=[
                 SimpleNamespace(returncode=0, stdout=b"(true,)\n"),
                 SimpleNamespace(returncode=0, stdout=b"(false,)\n")]) as run:
            result = environment_health()
        self.assertFalse(result["native_wayland_opt_in"])
        self.assertEqual(result["process_namespace_hint"], "bwrap")
        self.assertTrue(result["kwin_service_present"])
        self.assertFalse(result["cua_kwin_helper_present"])
        self.assertNotIn("CUA_DRIVER_RS_ENABLE_WAYLAND", before)
        for call in run.call_args_list:
            self.assertIn("org.freedesktop.DBus.NameHasOwner", call.args[0])
            self.assertLessEqual(call.kwargs["timeout"], 2)


if __name__ == "__main__":
    unittest.main()
