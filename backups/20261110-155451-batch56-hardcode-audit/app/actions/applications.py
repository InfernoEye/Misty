"""Safe discovery and launching of installed desktop applications."""

import configparser
import os
import shutil
import subprocess
from pathlib import Path


def _application_directories():
    data_home = Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    ).expanduser()

    data_dirs = os.environ.get(
        "XDG_DATA_DIRS", "/usr/local/share:/usr/share"
    ).split(":")

    roots = [data_home / "applications"]
    roots.extend(Path(item).expanduser() / "applications" for item in data_dirs if item)

    # Flatpak exports are included when not already covered by XDG_DATA_DIRS.
    roots.extend([
        Path.home() / ".local/share/flatpak/exports/share/applications",
        Path("/var/lib/flatpak/exports/share/applications"),
    ])

    result = []
    seen = set()

    for root in roots:
        key = str(root)
        if key not in seen and root.is_dir():
            seen.add(key)
            result.append(root)

    return result


def _locale_preferences():
    raw = (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANGUAGE")
        or os.environ.get("LANG")
        or "en"
    )

    preferences = []

    for item in raw.split(":"):
        language = item.split(".", 1)[0].split("@", 1)[0]
        if language:
            preferences.append(language)
            preferences.append(language.split("_", 1)[0])

    return list(dict.fromkeys(preferences))


def _read_entry(path, root):
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=False,
    )

    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            parser.read_file(file)

        entry = parser["Desktop Entry"]

        if entry.get("Type", "Application") != "Application":
            return None

        if entry.getboolean("Hidden", fallback=False):
            return {"hidden": True}

        if entry.getboolean("NoDisplay", fallback=False):
            return None

        if entry.getboolean("Terminal", fallback=False):
            # Terminal applications are not excluded; they can still be
            # launched through their desktop entry.
            pass

        name = None
        for language in _locale_preferences():
            name = entry.get(f"Name[{language}]")
            if name:
                break

        name = name or entry.get("Name")
        if not name:
            return None

        relative = path.relative_to(root).as_posix()
        desktop_id = relative[:-8].replace("/", "-")

        return {
            "id": desktop_id,
            "name": name,
            "generic_name": entry.get("GenericName", ""),
            "comment": entry.get("Comment", ""),
        }

    except (OSError, ValueError, KeyError, configparser.Error):
        return None


def get_installed_applications():
    """Return visible desktop applications, respecting directory priority."""
    applications = {}
    blocked_ids = set()

    for root in _application_directories():
        try:
            files = sorted(root.rglob("*.desktop"))
        except OSError:
            continue

        for path in files:
            try:
                relative = path.relative_to(root).as_posix()
                desktop_id = relative[:-8].replace("/", "-")
            except ValueError:
                continue

            if desktop_id in applications or desktop_id in blocked_ids:
                continue

            entry = _read_entry(path, root)

            if entry and entry.get("hidden"):
                blocked_ids.add(desktop_id)
            elif entry:
                applications[desktop_id] = entry

    return sorted(
        applications.values(),
        key=lambda item: (item["name"].casefold(), item["id"].casefold()),
    )


def list_installed_applications(query="", limit=40):
    if not isinstance(query, str):
        return {"success": False, "message": "Search query must be text."}

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        return {"success": False, "message": "Limit must be between 1 and 100."}

    applications = get_installed_applications()
    needle = query.casefold().strip()

    if needle:
        applications = [
            app for app in applications
            if needle in app["name"].casefold()
            or needle in app["id"].casefold()
            or needle in app["generic_name"].casefold()
            or needle in app["comment"].casefold()
        ]

    shown = applications[:limit]

    return {
        "success": True,
        "message": f"Found {len(applications)} matching applications; showing {len(shown)}.",
        "total": len(applications),
        "truncated": len(applications) > limit,
        "applications": shown,
    }


def launch_application(desktop_id):
    if not isinstance(desktop_id, str) or not desktop_id or len(desktop_id) > 255:
        return {"success": False, "message": "Invalid desktop application ID."}

    # Resolve against the current visible application list. Never pass an
    # arbitrary executable or user-supplied command to the process launcher.
    application = next(
        (item for item in get_installed_applications() if item["id"] == desktop_id),
        None,
    )

    if application is None:
        return {
            "success": False,
            "message": "That application is not in the installed application list.",
        }

    launcher = shutil.which("gtk-launch")
    if not launcher:
        return {"success": False, "message": "gtk-launch is unavailable."}

    try:
        subprocess.Popen(
            [launcher, desktop_id],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        return {"success": False, "message": f"Could not launch application: {error}"}

    return {
        "success": True,
        "message": f"Launching {application['name']}.",
        "application": application,
    }
