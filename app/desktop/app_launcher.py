"""Launch any installed application, not a hardcoded shortlist.

The original ``APPLICATIONS`` mapping named eleven apps, so any other
installed program was unreachable no matter how clearly the user asked for
it. This module discovers what is actually installed from the freedesktop
``.desktop`` entries and answers "launch this" for anything a normal desktop
session can start.

Discovery is read-only and bounded: it never executes an entry, never trusts
a field it does not use, and never launches anything itself — the caller
passes the resolved desktop ID to the existing ``gtk-launch`` path.
"""

import configparser
import os
import pathlib
import shutil


#: Directories a session reads desktop entries from, in priority order.
def _entry_directories():
    home = pathlib.Path(os.path.expanduser("~"))
    data_home = pathlib.Path(
        os.environ.get("XDG_DATA_HOME") or (home / ".local/share")
    )
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    directories = [data_home / "applications"]
    for part in data_dirs.split(":"):
        part = part.strip()
        if part:
            directories.append(pathlib.Path(part) / "applications")
    return directories


#: Entry fields that make an app unsuitable for a plain launch.
_SKIP_TYPES = frozenset({"Link", "Directory"})

#: How many entries to read at most, so a huge menu cannot stall a request.
MAX_ENTRIES = 4000


class InstalledApp:
    """One launchable desktop application."""

    __slots__ = ("desktop_id", "name", "generic_name", "keywords",
                 "categories", "exec")

    def __init__(self, desktop_id, name, generic_name="", keywords=(),
                 categories=(), exec_line=""):
        self.desktop_id = desktop_id
        self.name = name
        self.generic_name = generic_name
        self.keywords = tuple(keywords)
        self.categories = tuple(categories)
        self.exec = exec_line

    @property
    def search_text(self):
        """Everything a user might call this app, lowercased."""
        parts = [
            self.desktop_id.rsplit(".desktop", 1)[0],
            self.name,
            self.generic_name,
            " ".join(self.keywords),
            " ".join(self.categories),
        ]
        return " ".join(part for part in parts if part).casefold()

    def to_dict(self):
        return {
            "desktop_id": self.desktop_id,
            "name": self.name,
            "generic_name": self.generic_name,
        }

    def __repr__(self):
        return f"InstalledApp({self.desktop_id!r}, {self.name!r})"


def _read_entry(path):
    """Parse one ``.desktop`` file, or return None when unusable."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            # ``read_only`` belongs to read_string, not read_file; passing it
            # here raised TypeError, which the handler below swallowed — the
            # reason the first version discovered zero apps.
            parser.read_string(handle.read())
    except Exception:  # noqa: BLE001 - a broken entry is simply skipped
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    section = parser["Desktop Entry"]
    if (section.get("Type") or "Application") in _SKIP_TYPES:
        return None
    if (section.get("Hidden") or "").strip().casefold() == "true":
        return None
    if (section.get("NoDisplay") or "").strip().casefold() == "true":
        return None
    name = (section.get("Name") or "").strip()
    if not name:
        return None
    exec_line = (section.get("Exec") or "").strip()
    if not exec_line:
        return None

    def split_list(value):
        return [part.strip() for part in (value or "").split(";") if part.strip()]

    return InstalledApp(
        desktop_id=path.name,
        name=name,
        generic_name=(section.get("GenericName") or "").strip(),
        keywords=split_list(section.get("Keywords")),
        categories=split_list(section.get("Categories")),
        exec_line=exec_line,
    )


def discover(limit=MAX_ENTRIES):
    """Return every launchable app, earlier directories winning ties."""
    seen = {}
    read = 0
    for directory in _entry_directories():
        try:
            entries = sorted(directory.iterdir())
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        for path in entries:
            if read >= limit:
                break
            if path.suffix != ".desktop" or not path.is_file():
                continue
            if path.name in seen:
                # A user-level override shadows the system entry.
                continue
            read += 1
            app = _read_entry(path)
            if app is not None:
                seen[path.name] = app
    return list(seen.values())


def _tokens(text):
    return [part for part in str(text or "").casefold().split() if part]


def resolve(request_name, apps=None):
    """Resolve what the user called an app to one installed entry.

    Returns ``(app, candidates)``. ``app`` is None when nothing matched, and
    ``candidates`` lists the plausible ones so the caller can ask instead of
    guessing. Exact name and desktop-ID matches win; a prefix or a keyword
    match is accepted only when it is unique.
    """
    name = str(request_name or "").strip()
    if not name:
        return None, []
    installed = discover() if apps is None else list(apps)
    wanted = name.casefold()
    wanted_tokens = _tokens(name)

    def score(app):
        haystack = app.search_text
        if wanted == app.desktop_id.casefold() or wanted == app.name.casefold():
            return 100
        if wanted.replace(" ", "") == app.name.casefold().replace(" ", ""):
            return 95
        tokens = _tokens(app.search_text)
        if wanted_tokens and all(word in tokens for word in wanted_tokens):
            return 80
        if wanted in haystack:
            return 60
        return 0

    ranked = [(score(app), app) for app in installed]
    ranked = [(value, app) for value, app in ranked if value > 0]
    if not ranked:
        return None, []
    ranked.sort(key=lambda pair: (-pair[0], pair[1].name.casefold()))
    best = ranked[0][0]
    winners = [app for value, app in ranked if value == best]
    if len(winners) == 1:
        return winners[0], []
    return None, winners[:8]


def launcher():
    """The command that starts a desktop ID, or None when unavailable."""
    return shutil.which("gtk-launch")
