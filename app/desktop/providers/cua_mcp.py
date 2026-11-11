"""Owned, bounded Cua MCP stdio transport and a deliberately read-only adapter.

No daemon, auto-installation, permission prompt or input fallback is launched.
The native catalog is preserved; only explicitly reviewed observation calls are
admitted by this first-stage adapter. Importing the module performs no I/O.
"""
from __future__ import annotations

import copy
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from app.desktop.providers.base import (
    CapabilityReport, CleanupOutcome, DesktopProviderError, ToolOutcome,
)

PINNED_VERSION = "0.28.0"
MAX_MESSAGE_BYTES = 24 * 1024 * 1024
READ_ONLY_ARGUMENTS = {
    "list_apps": frozenset(),
    "list_windows": frozenset({"pid", "on_screen_only"}),
    "get_window_state": frozenset({"pid", "window_id", "session",
        "include_accessibility_tree", "include_screenshot", "query",
        "max_elements", "max_depth", "max_dimension"}),
}
READ_ONLY_TOOLS = frozenset(READ_ONLY_ARGUMENTS)


def _parse_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate MCP key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def _check(cancel, deadline):
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise DesktopProviderError("invalid_deadline", "A finite desktop deadline is required.")
    if cancel is not None and cancel.is_set():
        raise DesktopProviderError("canceled", "Desktop operation canceled.")
    if time.monotonic() >= deadline:
        raise DesktopProviderError("timeout", "Desktop operation timed out.")


class McpStdioTransport:
    """One owned process, one outstanding request, bounded pipe/message waits."""

    def __init__(self, command, *, environment=None, max_message_bytes=MAX_MESSAGE_BYTES):
        if (not isinstance(command, (list, tuple)) or not command
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)):
            raise ValueError("A nonempty argv command is required")
        if isinstance(max_message_bytes, bool) or not isinstance(max_message_bytes, int) or not 1024 <= max_message_bytes <= MAX_MESSAGE_BYTES:
            raise ValueError("Invalid MCP message limit")
        self.command = tuple(command)
        self.environment = environment
        self.max_message_bytes = max_message_bytes
        self.process = None
        self._responses = queue.Queue(maxsize=2)
        self._request_lock = threading.Lock()
        self._next_id = 0
        self._broken = threading.Event()
        self._readers = []
        self._closed = False
        self._close_lock = threading.Lock()
        self._cleanup_outcome = None
        self._owned_pgid = None

    def start(self):
        if self.process is not None or self._closed:
            raise DesktopProviderError("lifecycle", "Transport cannot be started twice.")
        try:
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.environment, bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            raise DesktopProviderError("unavailable", "Cua executable could not be started.") from exc
        # Keep the session leader unreaped until close has stopped its group.
        # An unreaped child reserves this PID even if the driver exits first,
        # preventing group-ID reuse while cleanup is authorized to signal it.
        try:
            if os.getpgid(self.process.pid) == self.process.pid == os.getsid(self.process.pid):
                self._owned_pgid = self.process.pid
        except OSError:
            pass
        for target, name in ((self._read_stdout, "misty-cua-stdout"),
                             (self._drain_stderr, "misty-cua-stderr")):
            worker = threading.Thread(target=target, name=name, daemon=True)
            self._readers.append(worker)
            worker.start()

    def _publish(self, item):
        try:
            self._responses.put_nowait(item)
        except queue.Full:
            self._broken.set()

    def _read_stdout(self):
        try:
            while not self._closed:
                line = self.process.stdout.readline(self.max_message_bytes + 1)
                if not line:
                    break
                if len(line) > self.max_message_bytes or not line.endswith(b"\n"):
                    self._broken.set()
                    break
                try:
                    message = _parse_json(line)
                except (UnicodeDecodeError, ValueError, RecursionError):
                    self._broken.set()
                    break
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    self._broken.set()
                    break
                if "method" in message:
                    # No server-to-client sampling/elicitation or unsolicited
                    # callbacks can expand authority. Even notifications are
                    # bounded by pipe reads and never retained as private logs.
                    if "id" in message:
                        self._broken.set()
                        break
                    continue
                self._publish(message)
        except (OSError, ValueError):
            self._broken.set()
        finally:
            self._publish(None)

    def _drain_stderr(self):
        try:
            while not self._closed and self.process.stderr.read(4096):
                pass  # Do not persist potentially private driver diagnostics.
        except (OSError, ValueError):
            pass

    def _child_exited(self):
        """Observe exit without releasing the PID that anchors group ownership."""
        try:
            return os.waitid(os.P_PID, self.process.pid,
                             os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            # An outside caller already reaped it; never signal that group
            # without ownership proof. close will report unconfirmed cleanup.
            return True

    def _send(self, message, *, cancel, deadline):
        _check(cancel, deadline)
        if self.process is None or self._closed or self._broken.is_set():
            raise DesktopProviderError("transport_closed", "Desktop transport is unavailable.")
        try:
            data = (json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode()
        except (TypeError, ValueError, RecursionError) as exc:
            raise DesktopProviderError("invalid_arguments", "Desktop request is not finite JSON.") from exc
        if len(data) > self.max_message_bytes:
            raise DesktopProviderError("message_limit", "Desktop request exceeds its size limit.")
        # Input arguments are small, but a peer that never reads stdin must
        # not block task cancellation. POSIX pipes are written nonblocking.
        import select
        fd = self.process.stdin.fileno()
        os.set_blocking(fd, False)
        view = memoryview(data)
        while view:
            _check(cancel, deadline)
            if self._child_exited():
                raise DesktopProviderError("transport_closed", "Desktop provider exited.")
            _, writable, _ = select.select([], [fd], [], min(0.05, max(0, deadline - time.monotonic())))
            if not writable:
                continue
            try:
                count = os.write(fd, view)
                view = view[count:]
            except BlockingIOError:
                continue
            except OSError as exc:
                raise DesktopProviderError("transport_closed", "Desktop provider pipe closed.") from exc

    def notify(self, method, params=None, *, cancel=None, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + 10
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message, cancel=cancel, deadline=deadline)

    def request(self, method, params=None, *, cancel=None, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + 20
        while not self._request_lock.acquire(timeout=0.05):
            _check(cancel, deadline)
        try:
            self._next_id += 1
            call_id = self._next_id
            message = {"jsonrpc": "2.0", "id": call_id, "method": method}
            if params is not None:
                message["params"] = params
            self._send(message, cancel=cancel, deadline=deadline)
            while True:
                _check(cancel, deadline)
                if self._broken.is_set():
                    raise DesktopProviderError("protocol_error", "Invalid desktop protocol data.")
                try:
                    response = self._responses.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if response is None:
                    raise DesktopProviderError("transport_closed", "Desktop provider disconnected.")
                if response.get("id") != call_id or ("result" in response) == ("error" in response):
                    raise DesktopProviderError("protocol_error", "Unexpected desktop response.")
                if "error" in response:
                    raise DesktopProviderError("rpc_error", "Desktop provider rejected the request.")
                return response["result"]
        except DesktopProviderError:
            # A canceled/expired response cannot become a later task's result.
            self.close()
            raise
        finally:
            self._request_lock.release()

    def close(self, timeout=2.0):
        failure = CleanupOutcome(False, ("Owned desktop process cleanup could not be confirmed.",))
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            return failure
        deadline = time.monotonic() + timeout
        self._closed = True  # Close admission before waiting for another closer.
        if not self._close_lock.acquire(timeout=timeout):
            return failure
        try:
            if self._cleanup_outcome is not None:
                return self._cleanup_outcome
            process = self.process
            if process is None:
                return CleanupOutcome(True)
            try:
                # waitid(WNOWAIT) proves that the exact child is still ours,
                # alive or zombie. Do not use poll()/wait() before signaling:
                # reaping the leader could release its process-group identity.
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if (self._owned_pgid != process.pid
                        or os.getpgid(process.pid) != self._owned_pgid
                        or os.getsid(process.pid) != process.pid):
                    raise ChildProcessError("Owned process group identity is unavailable")
                try:
                    os.killpg(self._owned_pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                grace = min(deadline, time.monotonic() + timeout / 2)
                for worker in self._readers:
                    if worker is not threading.current_thread():
                        worker.join(timeout=max(0, grace - time.monotonic()))
                # The driver may already have exited while a helper ignored
                # TERM. Its unreaped leader still anchors our group identity.
                try:
                    os.killpg(self._owned_pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
                for worker in self._readers:
                    if worker is not threading.current_thread():
                        worker.join(timeout=max(0, deadline - time.monotonic()))
                readers_stopped = all(not worker.is_alive() for worker in self._readers)
                self._cleanup_outcome = CleanupOutcome(True) if readers_stopped else failure
            except (OSError, ChildProcessError, subprocess.TimeoutExpired):
                # Unknown/reaped group identity must not trigger a blind kill.
                self._cleanup_outcome = failure
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
            return self._cleanup_outcome
        finally:
            self._close_lock.release()


class CuaMcpProvider:
    """Version-checked, read-only first-stage Cua provider.

    Input is intentionally unavailable until host-specific live acceptance and
    policy/target contracts are implemented. Calling an unreviewed tool fails
    before the MCP transport is touched.
    """

    def __init__(self, command=None, *, environment=None, transport_factory=McpStdioTransport):
        if command is None:
            import sys
            binary = Path(sys.executable).resolve().parent / "cua-driver"
            # sys.executable can resolve to system Python for symlink venvs.
            candidate = Path(sys.executable).parent / "cua-driver"
            command = [str(candidate if candidate.is_file() else binary), "mcp", "--direct"]
        self.command = tuple(command)
        self.environment = dict(os.environ if environment is None else environment)
        # Process-local privacy settings, never persistent driver preferences.
        self.environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
        self.environment["CUA_DRIVER_RS_UPDATE_CHECK"] = "0"
        self._factory = transport_factory
        self._transport = None
        self._tools = {}
        self.generation = uuid.uuid4().hex
        self._ready = False

    def start(self, cancel=None, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + 20
        _check(cancel, deadline)
        if self._transport is not None:
            raise DesktopProviderError("lifecycle", "Provider cannot be started twice.")
        self._transport = self._factory(self.command, environment=self.environment)
        try:
            self._transport.start()
            result = self._transport.request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "misty-desktop-probe", "version": "1"},
            }, cancel=cancel, deadline=deadline)
            if not isinstance(result, dict) or result.get("protocolVersion") not in {
                "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
            }:
                raise DesktopProviderError("protocol_version", "Unsupported desktop MCP protocol.")
            server = result.get("serverInfo", {})
            if not isinstance(server, dict) or server.get("version") != PINNED_VERSION:
                raise DesktopProviderError("version_mismatch", "Cua version does not match the reviewed adapter.")
            self._transport.notify("notifications/initialized", cancel=cancel, deadline=deadline)
            tools = []
            cursor = None
            seen = set()
            for _ in range(16):
                page = self._transport.request("tools/list", {"cursor": cursor} if cursor else {},
                                               cancel=cancel, deadline=deadline)
                if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                    raise DesktopProviderError("catalog_invalid", "Desktop catalog is malformed.")
                tools.extend(page["tools"])
                if len(tools) > 256:
                    raise DesktopProviderError("catalog_limit", "Desktop catalog exceeds its limit.")
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise DesktopProviderError("catalog_invalid", "Desktop catalog cursor is invalid.")
                seen.add(cursor)
            else:
                raise DesktopProviderError("catalog_limit", "Desktop catalog pagination exceeded its limit.")
            for tool in tools:
                if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                        or not tool["name"] or tool["name"] in self._tools
                        or not isinstance(tool.get("inputSchema"), dict)):
                    raise DesktopProviderError("catalog_invalid", "Desktop tool schema is malformed.")
                if tool["name"] in READ_ONLY_TOOLS:
                    try:
                        from jsonschema import Draft202012Validator
                        schema = tool["inputSchema"]
                        pending = [(schema, 0)]
                        visited = 0
                        while pending:
                            item, depth = pending.pop()
                            visited += 1
                            if visited > 10000 or depth > 40:
                                raise ValueError("Schema limit")
                            if isinstance(item, dict):
                                for key in ("$ref", "$dynamicRef"):
                                    if key in item and (not isinstance(item[key], str) or not item[key].startswith("#")):
                                        raise ValueError("Remote schema reference")
                                pending.extend((value, depth + 1) for value in item.values())
                            elif isinstance(item, list):
                                pending.extend((value, depth + 1) for value in item)
                        Draft202012Validator.check_schema(schema)
                    except ImportError as exc:
                        raise DesktopProviderError("dependency_missing", "JSON schema validation is unavailable.") from exc
                    except Exception as exc:
                        raise DesktopProviderError("catalog_invalid", "Reviewed desktop schema is invalid or requires remote data.") from exc
                self._tools[tool["name"]] = copy.deepcopy(tool)
            if not READ_ONLY_TOOLS.issubset(self._tools):
                raise DesktopProviderError("catalog_drift", "Required reviewed observation tools are missing.")
            self._ready = True
            return CapabilityReport(True, "cua-mcp", PINNED_VERSION,
                tuple(sorted(self._tools)),
                ("Input disabled: platform and task-policy acceptance have not passed.",), False)
        except Exception:
            self.close()
            raise

    def catalog(self):
        return [copy.deepcopy(self._tools[name]) for name in sorted(self._tools)
                if name in READ_ONLY_TOOLS]

    def call(self, tool_name, arguments, cancel=None, deadline=None):
        if not self._ready:
            raise DesktopProviderError("not_ready", "Desktop provider is not ready.")
        if tool_name not in READ_ONLY_TOOLS:
            raise DesktopProviderError("input_disabled", "This desktop tool is not admitted by the read-only adapter.")
        if not isinstance(arguments, dict):
            raise DesktopProviderError("invalid_arguments", "Tool arguments must be an object.")
        if set(arguments) - READ_ONLY_ARGUMENTS[tool_name]:
            raise DesktopProviderError("invalid_arguments", "Unreviewed desktop argument is not admitted.")
        # get_window_state can optionally write an image to disk; that is not
        # a read-only observation capability and is never admitted here.
        if "screenshot_out_file" in arguments:
            raise DesktopProviderError("invalid_arguments", "Screenshot file output is not admitted.")
        try:
            from jsonschema import Draft202012Validator
            Draft202012Validator(self._tools[tool_name]["inputSchema"]).validate(arguments)
        except ImportError as exc:
            raise DesktopProviderError("dependency_missing", "JSON schema validation is unavailable.") from exc
        except Exception as exc:
            raise DesktopProviderError("invalid_arguments", "Arguments do not match the desktop tool schema.") from exc
        result = self._transport.request("tools/call", {"name": tool_name, "arguments": arguments},
                                         cancel=cancel, deadline=deadline)
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise DesktopProviderError("result_invalid", "Desktop tool result is malformed.")
        # Retain exact ordered image/text blocks and structured fields. Model
        # admission performs image decoding separately; no prose substitution.
        return ToolOutcome(tool_name, copy.deepcopy(result), self.generation,
                           result.get("isError") is True)

    def close(self, deadline=None):
        self._ready = False
        if self._transport is None:
            return CleanupOutcome(True)
        timeout = 2.0 if deadline is None else max(0.01, min(2.0, deadline - time.monotonic()))
        return self._transport.close(timeout=timeout)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
