"""Read-only Cua readiness probe; never admits input or emits desktop contents.

Run in the desktop session being tested. By default only discovers the catalog
and visible-window metadata. An exact --pid/--window-id pair opts into one
bounded window snapshot. Neither mode establishes autonomous-input readiness.
"""
from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
import sys
import threading
import time
import warnings

from PIL import Image

# Support direct invocation from anywhere without installing Misty as a package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.desktop.providers.base import DesktopProviderError
from app.desktop.providers.cua_mcp import CuaMcpProvider, NONCAPTURE_HEALTH_CHECKS


MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGES = 4
IMAGE_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


def _exact_identifier(value, expected):
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _validate_images(content, structured):
    """Decode bounded raster images without publishing pixels or native prose.

    Presence of a nonempty base64 string is not visual readiness. Reject an
    invalid image anywhere in the batch and require reported dimensions to agree
    with the single screenshot when the driver supplies that metadata.
    """
    if not isinstance(content, list):
        return False, False
    blocks = [block for block in content
              if isinstance(block, dict) and block.get("type") == "image"]
    if not blocks:
        return False, False
    if len(blocks) > MAX_IMAGES:
        return True, False
    total_bytes = 0
    sizes = []
    try:
        for block in blocks:
            media_type, encoded = block.get("mimeType"), block.get("data")
            if media_type not in IMAGE_FORMATS or not isinstance(encoded, str) or not encoded:
                return True, False
            if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                return True, False
            data = base64.b64decode(encoded, validate=True)
            total_bytes += len(data)
            if not data or total_bytes > MAX_IMAGE_BYTES:
                return True, False
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    width, height = image.size
                    if (width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS
                            or image.format != IMAGE_FORMATS[media_type]
                            or getattr(image, "n_frames", 1) != 1):
                        return True, False
                    image.verify()
                # verify() checks file structure; load() also validates decoding.
                with Image.open(BytesIO(data)) as image:
                    image.load()
            sizes.append((width, height))
    except Exception:
        # Raster parser errors are untrusted input failures; never echo them.
        return True, False
    reported = (structured.get("screenshot_width"), structured.get("screenshot_height"))
    if any(value is not None for value in reported):
        if (len(sizes) != 1 or any(isinstance(value, bool) or not isinstance(value, int)
                                  or value <= 0 for value in reported)
                or tuple(reported) != sizes[0]):
            return True, False
    return True, True


def _positive_integer(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive integer.")
    return number


def environment_health(*, deadline=None):
    """Content-free session hints; service presence is not trusted targeting."""
    session = os.environ.get("XDG_SESSION_TYPE", "").casefold()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").casefold().split(":")
    enabled = os.environ.get("CUA_DRIVER_RS_ENABLE_WAYLAND", "").strip()
    result = {
        "session_type": session if session in {"x11", "wayland"} else "unknown",
        "desktop_family": "kde" if any(value in {"kde", "plasma"} for value in desktop) else "other",
        "native_wayland_opt_in": bool(enabled and enabled != "0" and enabled.casefold() != "false"),
        "process_namespace_hint": "unknown",
        "kwin_service_present": None,
        "cua_kwin_helper_present": None,
    }
    try:
        result["process_namespace_hint"] = (
            "bwrap" if Path("/proc/1/comm").read_text().strip() == "bwrap" else "not_detected")
    except OSError:
        pass
    executable = shutil.which("gdbus") if result["desktop_family"] == "kde" else None
    if executable:
        for name, field in (("org.kde.KWin", "kwin_service_present"),
                            ("org.cua.KWinTarget", "cua_kwin_helper_present")):
            remaining = 2.0 if deadline is None else min(2.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            try:
                call = subprocess.run([executable, "call", "--session", "--dest", "org.freedesktop.DBus",
                    "--object-path", "/org/freedesktop/DBus", "--method",
                    "org.freedesktop.DBus.NameHasOwner", name], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=remaining, check=False)
                if call.returncode == 0:
                    result[field] = {b"(true,)": True, b"(false,)": False}.get(call.stdout.strip())
            except (OSError, subprocess.TimeoutExpired):
                pass
    return result


def _noncapture_health(provider, cancel, deadline):
    if "health_report" not in {tool.get("name") for tool in provider.catalog()}:
        return {"status": "unavailable", "checks": {}}
    outcome = provider.call("health_report", {"include": list(NONCAPTURE_HEALTH_CHECKS)},
                            cancel=cancel, deadline=deadline)
    data = outcome.structured
    if outcome.is_error or not isinstance(data, dict) or data.get("schema_version") != "1":
        return {"status": "invalid", "checks": {}}
    checks = {}
    for item in data.get("checks", []) if isinstance(data.get("checks"), list) else []:
        if not isinstance(item, dict) or item.get("name") not in NONCAPTURE_HEALTH_CHECKS:
            continue
        name, status = item["name"], item.get("status")
        if name in checks or not isinstance(status, str) or status not in {"pass", "fail", "skip"}:
            return {"status": "invalid", "checks": {}}
        checks[name] = status
    if set(checks) != set(NONCAPTURE_HEALTH_CHECKS):
        return {"status": "incomplete", "checks": checks}
    return {"status": "reported", "checks": checks}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=_positive_integer,
                        help="Exact process ID from desktop window discovery.")
    parser.add_argument("--window-id", type=_positive_integer,
                        help="Exact native window ID, not a title or positional index.")
    parser.add_argument("--health", action="store_true",
                        help="Include non-capture driver health and read-only session/helper metadata.")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Total observation admission deadline in seconds (1–120; default 30).")
    return parser


def probe(*, pid=None, window_id=None, timeout=30.0, provider_factory=CuaMcpProvider,
          health=False, environment_probe=environment_health):
    """Return (content-free summary, exit_code), always closing the provider."""
    if not isinstance(health, bool):
        raise ValueError("health must be a boolean")
    if (pid is None) != (window_id is None):
        raise ValueError("pid and window_id must be supplied together")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 1 <= timeout <= 120):
        raise ValueError("timeout must be between 1 and 120 seconds")
    for identifier in (pid, window_id):
        if identifier is not None and (isinstance(identifier, bool)
                                      or not isinstance(identifier, int) or identifier <= 0):
            raise ValueError("Target identifiers must be positive integers")
    summary = {
        "provider": "cua-mcp",
        "available": False,
        "version": None,
        "catalog_tool_count": 0,
        "visible_window_count": 0,
        "snapshot_requested": pid is not None,
        "snapshot_returned": False,
        "image_block_present": False,
        "image_decoded": False,
        "snapshot_target_matches": False,
        "observation_ready": False,
        "screenshot_error": None,
        "platform_verified": False,
        "input_ready": False,
        "phase_gate": "not_checked",
        "cleanup_confirmed": False,
    }
    provider = None
    cancel = threading.Event()
    deadline = time.monotonic() + timeout
    exit_code = 2
    try:
        provider = provider_factory()
        capability = provider.start(cancel=cancel, deadline=deadline)
        summary["available"] = capability.available is True
        # Only the reviewed provider's negotiated version is public metadata.
        summary["version"] = capability.version
        catalog = provider.catalog()
        summary["catalog_tool_count"] = len(catalog)
        if health:
            summary["environment_health"] = environment_probe(deadline=deadline)
            summary["noncapture_health"] = (
                _noncapture_health(provider, cancel, deadline)
                if summary["available"] else {"status": "unavailable", "checks": {}})
        if not summary["available"]:
            summary["phase_gate"] = "provider_unavailable"
        else:
            arguments = {"on_screen_only": True}
            if pid is not None:
                arguments["pid"] = pid
            windows_result = provider.call("list_windows", arguments,
                                           cancel=cancel, deadline=deadline)
            structured = windows_result.structured
            windows = structured.get("windows") if isinstance(structured, dict) else None
            if windows_result.is_error:
                summary["phase_gate"] = "window_discovery_failed"
            elif not isinstance(windows, list) or any(not isinstance(w, dict) for w in windows):
                summary["phase_gate"] = "window_result_invalid"
            else:
                summary["visible_window_count"] = len(windows)
                if not windows:
                    summary["phase_gate"] = "no_visible_windows"
                elif pid is None:
                    summary["phase_gate"] = "discovery_only_snapshot_not_checked"
                    exit_code = 0
                elif len([w for w in windows
                          if _exact_identifier(w.get("pid"), pid)
                          and _exact_identifier(w.get("window_id"), window_id)
                          and w.get("is_on_screen") is not False]) != 1:
                    summary["phase_gate"] = "exact_visible_target_not_found"
                else:
                    snapshot = provider.call("get_window_state", {
                        "pid": pid, "window_id": window_id,
                        "include_accessibility_tree": True,
                        "include_screenshot": True,
                        "max_elements": 100, "max_depth": 8,
                    }, cancel=cancel, deadline=deadline)
                    summary["snapshot_returned"] = True
                    structured = snapshot.structured
                    structured = structured if isinstance(structured, dict) else {}
                    summary["snapshot_target_matches"] = (
                        _exact_identifier(structured.get("pid"), pid)
                        and _exact_identifier(structured.get("window_id"), window_id)
                    )
                    summary["image_block_present"], summary["image_decoded"] = _validate_images(
                        snapshot.content, structured)
                    reported_mime = structured.get("screenshot_mime_type")
                    if reported_mime is not None:
                        image_mimes = [b.get("mimeType") for b in snapshot.content
                                       if isinstance(b, dict) and b.get("type") == "image"]
                        if (not isinstance(reported_mime, str)
                                or not image_mimes or any(mime != reported_mime for mime in image_mimes)):
                            summary["image_decoded"] = False
                    error = structured.get("screenshot_error")
                    if error is not None:
                        # Never echo driver prose, paths, arbitrary codes or titles.
                        summary["screenshot_error"] = (
                            "surface_identity_unproven"
                            if isinstance(error, dict) and error.get("code") == "surface_identity_unproven"
                            else "reported_screenshot_error"
                        )
                    if snapshot.is_error:
                        summary["phase_gate"] = "snapshot_failed"
                    elif not summary["snapshot_target_matches"]:
                        summary["phase_gate"] = "snapshot_target_mismatch"
                    elif (summary["screenshot_error"] or
                          ("screenshot_frame_valid" in structured
                           and structured["screenshot_frame_valid"] is not True)):
                        summary["phase_gate"] = "snapshot_not_grounded"
                    elif not summary["image_block_present"]:
                        summary["phase_gate"] = "snapshot_image_missing"
                    elif not summary["image_decoded"]:
                        summary["phase_gate"] = "snapshot_image_invalid"
                    else:
                        summary["observation_ready"] = True
                        summary["phase_gate"] = "observation_only_input_disabled"
                        exit_code = 0
    except DesktopProviderError:
        summary["phase_gate"] = "provider_error"
    except KeyboardInterrupt:
        cancel.set()
        summary["phase_gate"] = "canceled"
        exit_code = 130
    except Exception:
        # Diagnostics from native libraries can contain private application data.
        summary["phase_gate"] = "probe_error"
    finally:
        if provider is not None:
            try:
                summary["cleanup_confirmed"] = provider.close().stopped is True
            except Exception:
                summary["cleanup_confirmed"] = False
            if not summary["cleanup_confirmed"]:
                summary["observation_ready"] = False
                summary["phase_gate"] = "cleanup_unconfirmed"
                exit_code = 2
    return summary, exit_code


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.pid is None) != (args.window_id is None):
        parser.error("--pid and --window-id must be supplied together")
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout must be between 1 and 120 seconds")
    summary, exit_code = probe(pid=args.pid, window_id=args.window_id, timeout=args.timeout,
                               health=args.health)
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
