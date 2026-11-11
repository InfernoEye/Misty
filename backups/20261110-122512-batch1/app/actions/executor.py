import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from app.brain.intent_parser import APPLICATIONS, FOLDERS


class ActionExecutor:
    """Runs only explicitly allowlisted desktop actions."""

    def __init__(self, window_service=None):
        self.allowed_applications = set(APPLICATIONS.values())
        self.window_service = window_service
        self.allowed_folders = set(FOLDERS.values())

    @staticmethod
    def _success(message, **data):
        return {"success": True, "message": message, **data}

    @staticmethod
    def _failure(message):
        return {"success": False, "message": message}

    @staticmethod
    def _run_volume_command(*args):
        executable = shutil.which("wpctl")
        if not executable:
            return None

        try:
            completed = subprocess.run(
                [executable, *args],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return str(error)

        if completed.returncode != 0:
            return completed.stderr.strip() or "Volume command failed."

        return ""

    def execute(self, parsed_intent):
        if not isinstance(parsed_intent, dict):
            return self._failure("Invalid intent.")

        intent = parsed_intent.get("intent")
        arguments = parsed_intent.get("arguments", {})

        if not isinstance(arguments, dict):
            return self._failure("Invalid intent arguments.")

        # Confirmation is always checked before executing an action.
        if parsed_intent.get("requires_confirmation", False):
            return self._failure(
                "This action requires confirmation. It has not been executed."
            )

        if intent == "open_default_browser":
            return self._open_default_browser()

        if intent == "open_application":
            return self._open_application(arguments.get("application"))

        if intent == "open_folder":
            return self._open_folder(arguments.get("path"))

        if intent == "get_time":
            current_time = datetime.now().astimezone()
            return self._success(
                f"It is {current_time.strftime('%H:%M')}.",
                time=current_time.isoformat(),
            )

        if intent == "get_system_status":
            return self._get_system_status()

        if intent == "volume_up":
            return self._change_volume("5%+")

        if intent == "volume_down":
            return self._change_volume("5%-")

        if intent == "volume_mute":
            return self._toggle_mute()

        if intent == "close_active_window":
            return self._failure(
                "Closing windows is not enabled yet. No action was taken."
            )

        return self._failure("I don't know how to perform that action.")

    def _resolve_file_path(self, folder, relative_path=""):
        """Resolve a path strictly inside an approved user folder."""
        if not isinstance(folder, str) or folder not in self.allowed_folders:
            raise ValueError("Folder is not on the filesystem allowlist.")

        if not isinstance(relative_path, str):
            raise ValueError("Relative path must be a string.")

        root = Path(os.path.expanduser(folder)).resolve(strict=True)

        if not root.is_dir():
            raise ValueError("The approved folder is not a directory.")

        relative = Path(relative_path) if relative_path else Path(".")

        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Only relative paths inside the approved folder are allowed.")

        target = (root / relative).resolve(strict=True)

        if not target.is_relative_to(root):
            raise ValueError("The requested path escapes the approved folder.")

        return root, target

    def list_files(self, folder, relative_path=""):
        """List at most 100 entries in an approved directory."""
        try:
            root, target = self._resolve_file_path(folder, relative_path)

            if not target.is_dir():
                return self._failure("The requested path is not a directory.")

            entries = sorted(target.iterdir(), key=lambda item: item.name.casefold())
            result = []

            for item in entries[:100]:
                if item.is_symlink():
                    entry_type = "symlink"
                elif item.is_dir():
                    entry_type = "directory"
                elif item.is_file():
                    entry_type = "file"
                else:
                    entry_type = "other"

                result.append({"name": item.name, "type": entry_type})

            return self._success(
                f"Found {len(entries)} entries; showing {len(result)}.",
                relative_path=str(target.relative_to(root)),
                entries=result,
                truncated=len(entries) > 100,
            )
        except (OSError, RuntimeError, ValueError) as error:
            return self._failure(f"Could not list that folder: {error}")

    def get_file_info(self, folder, relative_path):
        """Inspect basic metadata without reading file contents."""
        try:
            if not relative_path:
                return self._failure("A relative path is required.")

            root, target = self._resolve_file_path(folder, relative_path)
            metadata = target.stat()

            if target.is_dir():
                item_type = "directory"
            elif target.is_file():
                item_type = "file"
            else:
                item_type = "other"

            modified = datetime.fromtimestamp(
                metadata.st_mtime
            ).astimezone()

            return self._success(
                f"{target.name}: {item_type}, {metadata.st_size} bytes.",
                name=target.name,
                type=item_type,
                size_bytes=metadata.st_size,
                modified=modified.isoformat(),
                relative_path=str(target.relative_to(root)),
            )
        except (OSError, RuntimeError, ValueError) as error:
            return self._failure(f"Could not inspect that item: {error}")

    def _open_default_browser(self):
        executable = shutil.which("gtk-launch")
        if not executable:
            return self._failure("Could not find gtk-launch.")

        try:
            subprocess.Popen(
                [executable, "helium"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            return self._failure(f"Could not open Helium: {error}")

        return self._success("Opening Helium Browser.")
    def _open_application(self, application):
        if application not in self.allowed_applications:
            return self._failure("That application is not on the allowlist.")

        executable = shutil.which(application)
        if not executable:
            return self._failure(
                f"I couldn't find the application '{application}'."
            )

        try:
            subprocess.Popen(
                [executable],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            return self._failure(f"Could not launch the application: {error}")

        return self._success(f"Opening {application}.")

    def _open_folder(self, folder):
        if folder not in self.allowed_folders:
            return self._failure("That folder is not on the allowlist.")

        xdg_names = {
            "~/Pobrane": "DOWNLOAD",
            "~/Documents": "DOCUMENTS",
            "~/Desktop": "DESKTOP",
            "~/Pictures": "PICTURES",
            "~/Music": "MUSIC",
            "~/Videos": "VIDEOS",
        }

        try:
            if folder in xdg_names:
                xdg = shutil.which("xdg-user-dir")
                if not xdg:
                    return self._failure("Could not find xdg-user-dir.")

                completed = subprocess.run(
                    [xdg, xdg_names[folder]],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=True,
                )
                path = Path(completed.stdout.strip()).expanduser().resolve()
            else:
                path = Path(os.path.expanduser(folder)).resolve()
        except (OSError, subprocess.SubprocessError) as error:
            return self._failure(f"Could not resolve that folder: {error}")

        if not path.is_dir():
            return self._failure(f"The folder does not exist: {path}")

        executable = shutil.which("xdg-open")
        if not executable:
            return self._failure("Could not find xdg-open.")

        try:
            subprocess.Popen(
                [executable, str(path)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            return self._failure(f"Could not open the folder: {error}")

        return self._success(f"Opening {path.name or 'home folder'}.")
    def _change_volume(self, adjustment):
        error = self._run_volume_command(
            "set-volume",
            "@DEFAULT_AUDIO_SINK@",
            adjustment,
        )

        if error is None:
            return self._failure("wpctl is unavailable.")

        if error:
            return self._failure(f"Volume adjustment failed: {error}")

        return self._success("Volume adjusted.")

    def _toggle_mute(self):
        error = self._run_volume_command(
            "set-mute",
            "@DEFAULT_AUDIO_SINK@",
            "1",
        )

        if error is None:
            return self._failure("wpctl is unavailable.")

        if error:
            return self._failure(f"Could not toggle mute: {error}")

        return self._success("Audio muted.")

    def _get_system_status(self):
        try:
            with open("/proc/meminfo", encoding="utf-8") as file:
                memory = {}

                for line in file:
                    key, value = line.split(":", 1)
                    memory[key] = int(value.strip().split()[0])

            total_mb = memory["MemTotal"] // 1024
            available_mb = memory["MemAvailable"] // 1024
            used_mb = total_mb - available_mb

            load = os.getloadavg()[0]

            return self._success(
                f"Memory usage is {used_mb} of {total_mb} megabytes. "
                f"One-minute CPU load average is {load:.2f}.",
                memory_used_mb=used_mb,
                memory_total_mb=total_mb,
                cpu_load_1m=load,
                cpu_count=os.cpu_count(),
            )
        except (OSError, KeyError, ValueError) as error:
            return self._failure(f"Could not read system status: {error}")

    def list_installed_apps(self, query="", limit=40):
        from app.actions.applications import list_installed_applications
        return list_installed_applications(query, limit)

    def launch_app(self, app_id):
        from app.actions.applications import launch_application
        return launch_application(app_id)

    def control_window(self, action, window_id):
        if self.window_service is None:
            return self._failure("KDE window control is not available.")
        return self.window_service.control_window(action, window_id)

    def move_window_to_screen(self, window_id, screen_name):
        if self.window_service is None:
            return self._failure("KDE window control is not available.")
        return self.window_service.move_window_to_screen(
            window_id, screen_name
        )

    def list_windows(self):
        if self.window_service is None:
            return self._failure("KDE window awareness is not available.")
        return self.window_service.list_windows()

    def get_active_window(self):
        if self.window_service is None:
            return self._failure("KDE window awareness is not available.")
        return self.window_service.get_active_window()

    def list_screens(self):
        if self.window_service is None:
            return self._failure("KDE display awareness is not available.")
        return self.window_service.list_screens()

