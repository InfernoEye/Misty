"""Read-only KDE KWin window discovery through a private session D-Bus bridge."""

import asyncio
import concurrent.futures
import json
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method


BUS_NAME = "org.misty.MistyWindows"
OBJECT_PATH = "/org/misty/MistyWindows"
INTERFACE = "org.misty.MistyWindows"


class _WindowBridge(ServiceInterface):
    def __init__(self, service):
        super().__init__(INTERFACE)
        self.service = service

    @method()
    def Receive(self, request_id: "s", payload: "s") -> "s":
        self.service._receive(request_id, payload)
        return "received"


class KWinWindowService:
    """Runs the D-Bus receiver on a dedicated asyncio thread."""

    def __init__(self):
        self._loop = None
        self._bus = None
        self._thread = None
        self._ready = threading.Event()
        self._startup_error = None
        self._pending = {}
        self._pending_lock = threading.Lock()

        self._snapshot_lock = threading.Lock()
        self._captured_active_window = None
        self._has_active_snapshot = False

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            if self._startup_error:
                raise RuntimeError(str(self._startup_error))
            return

        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="misty-kwin-dbus",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(5):
            raise RuntimeError("Timed out starting the KDE window D-Bus service.")
        if self._startup_error:
            raise RuntimeError(
                f"Could not start KDE window D-Bus service: {self._startup_error}"
            )

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def connect():
            self._bus = await MessageBus().connect()
            await self._bus.request_name(BUS_NAME)
            self._bus.export(OBJECT_PATH, _WindowBridge(self))

        try:
            loop.run_until_complete(connect())
        except Exception as error:
            self._startup_error = error
            self._ready.set()
            loop.close()
            return

        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                if self._bus is not None:
                    self._bus.disconnect()
            finally:
                loop.close()

    def close(self):
        if self._loop is not None and self._thread is not None:
            if self._thread.is_alive():
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=3)
        self._loop = None
        self._thread = None

    def _receive(self, request_id, payload):
        with self._pending_lock:
            future = self._pending.get(request_id)
        if future is not None and not future.done():
            try:
                future.set_result(payload)
            except concurrent.futures.InvalidStateError:
                pass

    @staticmethod
    def _qdbus(*arguments, timeout=5):
        executable = shutil.which("qdbus6")
        if not executable:
            raise RuntimeError("qdbus6 is not installed.")

        completed = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(detail or "KWin D-Bus command failed.")
        return completed.stdout.strip()

    def _query(self):
        if self._startup_error:
            raise RuntimeError(
                f"KDE window service is unavailable: {self._startup_error}"
            )
        if self._thread is None or not self._thread.is_alive():
            self.start()

        request_id = uuid.uuid4().hex
        plugin_name = f"misty-window-query-{request_id}"
        future = concurrent.futures.Future()

        script = f"""
var windows = workspace.stackingOrder;
var result = [];
var overlays = [];
var active = null;

for (var i = 0; i < windows.length; i++) {{
    var w = windows[i];
    if (!w.normalWindow || w.deleted || !w.caption || !String(w.caption).trim()) {{
        continue;
    }}

    var appId = String(w.desktopFileName || "");
    var resourceClass = String(w.resourceClass || "").toLowerCase();
    var resourceName = String(w.resourceName || "").toLowerCase();
    var isMistyPopup = (
        appId === "misty-popup" ||
        resourceClass === "misty-popup" ||
        resourceName === "misty-popup"
    );
    var isMisty = (
        appId === "misty" ||
        resourceClass === "misty" ||
        resourceName === "misty"
    );
    if (isMisty || isMistyPopup) {{
        if (isMistyPopup) {{
            overlays.push({{
                id: String(w.internalId),
                title: String(w.caption),
                app_id: appId,
                minimized: Boolean(w.minimized),
                screen: w.output ? String(w.output.name || "") : "",
                on_current_desktop: (function() {{
                    try {{
                        if (typeof w.onCurrentDesktop === "boolean") {{
                            return w.onCurrentDesktop;
                        }}
                        if (w.onAllDesktops === true) {{
                            return true;
                        }}
                        var current = workspace.currentDesktop;
                        var desktops = w.desktops;
                        if (!current || !desktops) {{
                            return null;
                        }}
                        var currentId = current.id;
                        if (currentId === undefined || currentId === null) {{
                            return null;
                        }}
                        if (desktops.length === 0) {{
                            return true;
                        }}
                        var compared = 0;
                        for (var d = 0; d < desktops.length; d++) {{
                            var desktop = desktops[d];
                            if (!desktop || desktop.id === undefined ||
                                desktop.id === null) {{
                                continue;
                            }}
                            compared += 1;
                            if (String(desktop.id) === String(currentId)) {{
                                return true;
                            }}
                        }}
                        return compared > 0 ? false : null;
                    }} catch (error) {{
                        return null;
                    }}
                }})(),
                geometry: (function() {{
                    var g = w.frameGeometry;
                    return {{
                        x: Math.round(g.x),
                        y: Math.round(g.y),
                        width: Math.round(g.width),
                        height: Math.round(g.height)
                    }};
                }})()
            }});
        }}
        continue;
    }}

    var item = {{
        id: String(w.internalId),
        title: String(w.caption),
        app_id: appId,
        resource_name: String(w.resourceName || ""),
        resource_class: String(w.resourceClass || ""),
        pid: Number(w.pid || 0),
        minimized: Boolean(w.minimized),
        active: Boolean(w.active),
        screen: w.output ? String(w.output.name || "") : "",
        on_current_desktop: (function() {{
            try {{
                if (typeof w.onCurrentDesktop === "boolean") {{
                    return w.onCurrentDesktop;
                }}
                if (w.onAllDesktops === true) {{
                    return true;
                }}
                var current = workspace.currentDesktop;
                var desktops = w.desktops;
                if (!current || !desktops) {{
                    return null;
                }}
                var currentId = current.id;
                if (currentId === undefined || currentId === null) {{
                    return null;
                }}
                if (desktops.length === 0) {{
                    return true;
                }}
                var compared = 0;
                for (var d = 0; d < desktops.length; d++) {{
                    var desktop = desktops[d];
                    if (!desktop || desktop.id === undefined ||
                        desktop.id === null) {{
                        continue;
                    }}
                    compared += 1;
                    if (String(desktop.id) === String(currentId)) {{
                        return true;
                    }}
                }}
                return compared > 0 ? false : null;
            }} catch (error) {{
                return null;
            }}
        }})(),
        geometry: (function() {{
            var g = w.frameGeometry;
            return {{
                x: Math.round(g.x),
                y: Math.round(g.y),
                width: Math.round(g.width),
                height: Math.round(g.height)
            }};
        }})()
    }};
    result.push(item);
    if (item.active) {{
        active = item;
    }}
}}

var screenList = workspace.screens || [];
var screens = [];

for (var j = 0; j < screenList.length; j++) {{
    var screen = screenList[j];
    var sg = screen.geometry;
    screens.push({{
        name: String(screen.name || ""),
        manufacturer: String(screen.manufacturer || ""),
        model: String(screen.model || ""),
        serial_number: String(screen.serialNumber || ""),
        geometry: {{
            x: Math.round(sg.x),
            y: Math.round(sg.y),
            width: Math.round(sg.width),
            height: Math.round(sg.height)
        }}
    }});
}}

var payload = JSON.stringify({{
    windows: result,
    overlays: overlays,
    active_window: active,
    screens: screens
}});
callDBus(
    "{BUS_NAME}",
    "{OBJECT_PATH}",
    "{INTERFACE}",
    "Receive",
    "{request_id}",
    payload,
    function(reply) {{}}
);
"""

        with self._pending_lock:
            self._pending[request_id] = future

        script_path = None
        loaded = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".js",
                prefix="misty-kwin-",
                delete=False,
            ) as file:
                file.write(script)
                script_path = Path(file.name)

            script_id = self._qdbus(
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting.loadScript",
                str(script_path),
                plugin_name,
            )
            if not script_id:
                raise RuntimeError("KWin did not return a script ID.")
            loaded = True

            self._qdbus(
                "org.kde.KWin",
                f"/Scripting/Script{script_id}",
                "org.kde.kwin.Script.run",
            )

            raw = future.result(timeout=8)
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
                raise RuntimeError("KWin returned an invalid window list.")
            return data
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

            if loaded:
                try:
                    self._qdbus(
                        "org.kde.KWin",
                        "/Scripting",
                        "org.kde.kwin.Scripting.unloadScript",
                        plugin_name,
                    )
                except Exception:
                    pass

            if script_path is not None:
                script_path.unlink(missing_ok=True)

    def control_window(self, action, window_id):
        """Perform an allowlisted operation on one exact KWin window ID."""
        allowed = {
            "focus", "minimize", "maximize", "restore", "close"
        }
        if action not in allowed:
            return {"success": False, "message": "Unsupported window action."}

        if not isinstance(window_id, str) or not window_id or len(window_id) > 128:
            return {"success": False, "message": "Invalid window ID."}

        try:
            if self._startup_error:
                raise RuntimeError(str(self._startup_error))
            if self._thread is None or not self._thread.is_alive():
                self.start()

            request_id = uuid.uuid4().hex
            plugin_name = f"misty-window-control-{request_id}"
            future = concurrent.futures.Future()

            script = f"""
var targetId = {json.dumps(window_id)};
var action = {json.dumps(action)};
var target = null;

var windows = workspace.stackingOrder;
for (var i = 0; i < windows.length; i++) {{
    var w = windows[i];
    if (String(w.internalId) === targetId &&
        w.normalWindow && !w.deleted) {{
        var appId = String(w.desktopFileName || "");
        if (appId !== "misty" && appId !== "misty-popup") {{
            target = w;
            break;
        }}
    }}
}}

var result;
if (target === null) {{
    result = {{
        success: false,
        message: "That window no longer exists or is not controllable."
    }};
}} else {{
    var title = String(target.caption || "Untitled window");
    if (action === "focus") {{
        target.minimized = false;
        workspace.activeWindow = target;
        workspace.raiseWindow(target);
        result = {{success: true, message: "Focused: " + title, title: title}};
    }} else if (action === "minimize") {{
        target.minimized = true;
        result = {{success: true, message: "Minimized: " + title, title: title}};
    }} else if (action === "maximize") {{
        if (!target.maximizable) {{
            result = {{success: false, message: "This window cannot be maximized."}};
        }} else {{
            target.setMaximize(true, true);
            result = {{success: true, message: "Maximized: " + title, title: title}};
        }}
    }} else if (action === "restore") {{
        target.minimized = false;
        target.setMaximize(false, false);
        result = {{success: true, message: "Restored: " + title, title: title}};
    }} else if (action === "close") {{
        if (!target.closeable) {{
            result = {{success: false, message: "KWin reports that this window cannot be closed."}};
        }} else {{
            target.closeWindow();
            result = {{success: true, message: "Close requested for: " + title, title: title}};
        }}
    }} else {{
        result = {{success: false, message: "Unsupported window action."}};
    }}
}}

callDBus(
    "{BUS_NAME}",
    "{OBJECT_PATH}",
    "{INTERFACE}",
    "Receive",
    "{request_id}",
    JSON.stringify(result),
    function(reply) {{}}
);
"""

            with self._pending_lock:
                self._pending[request_id] = future

            script_path = None
            loaded = False
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".js",
                    prefix="misty-kwin-control-",
                    delete=False,
                ) as file:
                    file.write(script)
                    script_path = Path(file.name)

                script_id = self._qdbus(
                    "org.kde.KWin",
                    "/Scripting",
                    "org.kde.kwin.Scripting.loadScript",
                    str(script_path),
                    plugin_name,
                )
                if not script_id:
                    raise RuntimeError("KWin did not return a script ID.")
                loaded = True

                self._qdbus(
                    "org.kde.KWin",
                    f"/Scripting/Script{script_id}",
                    "org.kde.kwin.Script.run",
                )

                result = json.loads(future.result(timeout=8))
                if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                    raise RuntimeError("KWin returned an invalid operation result.")
                return result
            finally:
                with self._pending_lock:
                    self._pending.pop(request_id, None)

                if loaded:
                    try:
                        self._qdbus(
                            "org.kde.KWin",
                            "/Scripting",
                            "org.kde.kwin.Scripting.unloadScript",
                            plugin_name,
                        )
                    except Exception:
                        pass

                if script_path is not None:
                    script_path.unlink(missing_ok=True)

        except Exception as error:
            return {
                "success": False,
                "message": f"Window operation failed: {error}",
            }


    def move_window_to_screen(self, window_id, screen_name):
        """Move one exact window to one unambiguously identified KWin output."""
        if not isinstance(window_id, str) or not window_id or len(window_id) > 128:
            return {"success": False, "message": "Invalid window ID."}

        if not isinstance(screen_name, str) or not screen_name.strip() or len(screen_name) > 256:
            return {"success": False, "message": "A valid destination display name is required."}

        try:
            if self._startup_error:
                raise RuntimeError(str(self._startup_error))
            if self._thread is None or not self._thread.is_alive():
                self.start()

            request_id = uuid.uuid4().hex
            plugin_name = f"misty-window-move-{request_id}"
            future = concurrent.futures.Future()

            script = """
var targetId = __TARGET_ID__;
var screenQuery = __SCREEN_QUERY__.trim().toLowerCase();
var target = null;

var windows = workspace.stackingOrder;
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (String(w.internalId) === targetId &&
        w.normalWindow && !w.deleted) {
        var appId = String(w.desktopFileName || "");
        if (appId !== "misty" && appId !== "misty-popup") {
            target = w;
            break;
        }
    }
}

var outputs = workspace.screens || [];
var matches = outputs.filter(function(output) {
    return String(output.name || "").toLowerCase() === screenQuery;
});

if (matches.length === 0 && screenQuery) {
    matches = outputs.filter(function(output) {
        var fields = [
            output.name || "",
            output.model || "",
            output.manufacturer || ""
        ];
        return fields.some(function(value) {
            return String(value).toLowerCase().indexOf(screenQuery) !== -1;
        });
    });
}

var result;
if (target === null) {
    result = {
        success: false,
        message: "That window no longer exists or is not controllable."
    };
} else if (!screenQuery) {
    result = {
        success: false,
        message: "A destination display is required."
    };
} else if (matches.length === 0) {
    result = {
        success: false,
        message: "No connected display matches the requested destination."
    };
} else if (matches.length > 1) {
    result = {
        success: false,
        message: "More than one display matches. Please specify DP-1, DP-2, or DP-3."
    };
} else if (!target.moveableAcrossScreens) {
    result = {
        success: false,
        message: "This window cannot be moved to another display."
    };
} else {
    var destination = matches[0];
    var title = String(target.caption || "Untitled window");

    if (target.output && target.output.name === destination.name) {
        result = {
            success: true,
            message: title + " is already on " + destination.name + ".",
            title: title,
            screen: destination.name
        };
    } else {
        workspace.sendClientToScreen(target, destination);
        result = {
            success: true,
            message: "Moved " + title + " to " + destination.name + ".",
            title: title,
            screen: destination.name
        };
    }
}

callDBus(
    __BUS_NAME__,
    __OBJECT_PATH__,
    __INTERFACE__,
    "Receive",
    __REQUEST_ID__,
    JSON.stringify(result),
    function(reply) {}
);
"""

            script = (
                script
                .replace("__TARGET_ID__", json.dumps(window_id))
                .replace("__SCREEN_QUERY__", json.dumps(screen_name))
                .replace("__BUS_NAME__", json.dumps(BUS_NAME))
                .replace("__OBJECT_PATH__", json.dumps(OBJECT_PATH))
                .replace("__INTERFACE__", json.dumps(INTERFACE))
                .replace("__REQUEST_ID__", json.dumps(request_id))
            )

            with self._pending_lock:
                self._pending[request_id] = future

            script_path = None
            loaded = False
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".js",
                    prefix="misty-kwin-move-",
                    delete=False,
                ) as file:
                    file.write(script)
                    script_path = Path(file.name)

                script_id = self._qdbus(
                    "org.kde.KWin",
                    "/Scripting",
                    "org.kde.kwin.Scripting.loadScript",
                    str(script_path),
                    plugin_name,
                )
                if not script_id:
                    raise RuntimeError("KWin did not return a script ID.")
                loaded = True

                self._qdbus(
                    "org.kde.KWin",
                    f"/Scripting/Script{script_id}",
                    "org.kde.kwin.Script.run",
                )

                result = json.loads(future.result(timeout=8))
                if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                    raise RuntimeError("KWin returned an invalid monitor-move result.")
                return result
            finally:
                with self._pending_lock:
                    self._pending.pop(request_id, None)

                if loaded:
                    try:
                        self._qdbus(
                            "org.kde.KWin",
                            "/Scripting",
                            "org.kde.kwin.Scripting.unloadScript",
                            plugin_name,
                        )
                    except Exception:
                        pass

                if script_path is not None:
                    script_path.unlink(missing_ok=True)

        except Exception as error:
            return {
                "success": False,
                "message": f"Window monitor move failed: {error}",
            }

    def capture_active_window(self):
        """Remember the active non-Misty window before showing the popup."""
        try:
            data = self._query()
            active = data.get("active_window")

            with self._snapshot_lock:
                self._captured_active_window = active
                self._has_active_snapshot = True

            if active is None:
                return {
                    "success": True,
                    "message": "No active external application window was found.",
                    "active_window": None,
                }

            return {
                "success": True,
                "message": f'Captured active window: {active["title"]}.',
                "active_window": active,
            }
        except Exception as error:
            with self._snapshot_lock:
                self._captured_active_window = None
                self._has_active_snapshot = False

            return {
                "success": False,
                "message": f"Could not capture active window: {error}",
            }

    def clear_active_window(self):
        """Forget the previous request's active-window snapshot."""
        with self._snapshot_lock:
            self._captured_active_window = None
            self._has_active_snapshot = False

    def list_windows(self):
        try:
            data = self._query()
            windows = data["windows"]
            overlays = data.get("overlays", [])
            if not isinstance(overlays, list):
                overlays = []
            return {
                "success": True,
                "message": f"Found {len(windows)} open application window(s).",
                "windows": windows,
                "overlays": overlays,
            }
        except Exception as error:
            return {
                "success": False,
                "message": f"Could not list KDE windows: {error}",
            }

    def list_screens(self):
        """Return the currently connected KWin outputs and their geometries."""
        try:
            data = self._query()
            screens = data.get("screens", [])

            # KWin's screen-list order is not KDE's display-priority order.
            # Read the explicit priority reported by KScreen.
            try:
                output = subprocess.run(
                    ["kscreen-doctor", "-o"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if output.returncode == 0:
                    priorities = {}
                    current_output = None

                    # kscreen-doctor colours its output with ANSI escape codes.
                    # Strip them before parsing output names and priorities.
                    clean_output = re.sub(
                        r"\x1b\[[0-?]*[ -/]*[@-~]",
                        "",
                        output.stdout,
                    )

                    for line in clean_output.splitlines():
                        parts = line.strip().split()

                        if len(parts) >= 3 and parts[0] == "Output:":
                            current_output = parts[2]
                            continue

                        if (
                            len(parts) >= 2
                            and parts[0] == "priority"
                            and current_output
                        ):
                            try:
                                priorities[current_output] = int(parts[1])
                            except ValueError:
                                pass
                            current_output = None

                    for screen in screens:
                        priority = priorities.get(screen.get("name", ""))
                        if priority is not None:
                            screen["priority"] = priority
            except (OSError, subprocess.SubprocessError):
                pass

            return {
                "success": True,
                "message": f"Found {len(screens)} active display(s).",
                "screens": screens,
            }
        except Exception as error:
            return {
                "success": False,
                "message": f"Could not list KDE displays: {error}",
            }

    def get_captured_active_window(self):
        """Return only the window snapshot captured when listening began."""
        with self._snapshot_lock:
            if not self._has_active_snapshot:
                return {
                    "success": False,
                    "message": "No active-window snapshot exists for this request.",
                    "active_window": None,
                }
            active = self._captured_active_window

        if active is None:
            return {
                "success": False,
                "message": "No external application was active when listening began.",
                "active_window": None,
            }

        return {
            "success": True,
            "message": "Previous active application snapshot retrieved.",
            "active_window": dict(active),
        }

    def get_active_window(self):
        try:
            with self._snapshot_lock:
                has_snapshot = self._has_active_snapshot
                active = self._captured_active_window

            if not has_snapshot:
                data = self._query()
                active = data.get("active_window")

            if active is None:
                return {
                    "success": True,
                    "message": "There was no active external application window when listening began.",
                    "active_window": None,
                }

            return {
                "success": True,
                "message": f'Active application when listening began: {active["title"]}.',
                "active_window": active,
            }
        except Exception as error:
            return {
                "success": False,
                "message": f"Could not get the active KDE window: {error}",
            }
