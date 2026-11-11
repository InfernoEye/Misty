"""Shared, bounded resolution of approved user folders.

Misty's model never supplies a filesystem path directly. Tools select one of
the approved folder keys below, optionally with a relative path, and this
module returns a canonical location inside the current user's home directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


APPROVED_FOLDERS = {
    "Home": "~",
    "Downloads": "~/Pobrane",
    "Documents": "~/Documents",
    "Desktop": "~/Desktop",
    "Pictures": "~/Pictures",
    "Music": "~/Music",
    "Videos": "~/Videos",
}

XDG_DIRECTORY_NAMES = {
    "Downloads": "DOWNLOAD",
    "Documents": "DOCUMENTS",
    "Desktop": "DESKTOP",
    "Pictures": "PICTURES",
    "Music": "MUSIC",
    "Videos": "VIDEOS",
}


class FileScopeError(ValueError):
    """Raised when a requested folder scope is not allowed."""


class UserFolderResolver:
    """Resolve approved folder keys and descendants under the user's home.

    An XDG user directory may relocate a standard folder. Its canonical path
    is accepted only while it stays inside the current home directory; an
    external relocation is reported instead of silently widening the boundary.
    """

    def __init__(
        self,
        home=None,
        folder_paths=None,
        xdg_lookup=None,
    ):
        self.home = Path(home or Path.home()).expanduser()
        self.folder_paths = dict(folder_paths or APPROVED_FOLDERS)
        self._xdg_lookup = xdg_lookup

    def _home(self) -> Path:
        try:
            home = self.home.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise FileScopeError(
                f"The home directory is unavailable: {error}"
            ) from error
        if not home.is_dir():
            raise FileScopeError("The home directory is not a directory.")
        return home

    def _xdg_directory(self, key) -> Path | None:
        name = XDG_DIRECTORY_NAMES.get(key)
        if name is None:
            return None

        raw = None
        if self._xdg_lookup is not None:
            raw = self._xdg_lookup(name)
        else:
            executable = shutil.which("xdg-user-dir")
            if executable:
                try:
                    completed = subprocess.run(
                        [executable, name],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    completed = None
                if completed is not None and completed.returncode == 0:
                    raw = completed.stdout.strip()

        if not raw:
            return None

        try:
            candidate = Path(os.path.expanduser(raw)).resolve(strict=True)
        except (OSError, RuntimeError):
            return None

        if candidate == self._home() or candidate.is_relative_to(self._home()):
            return candidate if candidate.is_dir() else None

        raise FileScopeError(
            f"The configured {key} directory resolves outside the home "
            "directory. No folder was searched."
        )

    def _configured_path(self, key: str) -> Path:
        value = self.folder_paths[key]
        if value in {"~", "~/"}:
            return self.home
        if value.startswith("~/"):
            return self.home / value[2:]
        return Path(value)

    def base_for_key(self, key: str) -> Path:
        if not isinstance(key, str) or key not in self.folder_paths:
            raise FileScopeError("Folder is not on the filesystem allowlist.")

        home = self._home()
        xdg = self._xdg_directory(key)
        if xdg is not None:
            base = xdg
        else:
            try:
                base = self._configured_path(key).resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise FileScopeError(
                    f"The approved folder is unavailable: {error}"
                ) from error

        if not base.is_dir():
            raise FileScopeError("The approved folder is not a directory.")
        if not base.is_relative_to(home):
            raise FileScopeError(
                "The approved folder resolves outside the home directory."
            )
        return base

    def base_for_path(self, folder_path: str) -> Path:
        """Resolve one of the configured static folder paths.

        Tool schemas still use stable path values internally. This method maps
        a value back to its key so relocated XDG folders resolve consistently
        for opening, listing, and searching.
        """
        if not isinstance(folder_path, str):
            raise FileScopeError("Folder is not on the filesystem allowlist.")

        key = next(
            (
                name for name, value in self.folder_paths.items()
                if value == folder_path
            ),
            None,
        )
        if key is None:
            raise FileScopeError("Folder is not on the filesystem allowlist.")
        return self.base_for_key(key)

    def child(self, base: Path, relative_path: str = "") -> Path:
        if not isinstance(relative_path, str):
            raise FileScopeError("Relative path must be a string.")

        relative = Path(relative_path) if relative_path else Path(".")
        if relative.is_absolute() or ".." in relative.parts:
            raise FileScopeError(
                "Only relative paths inside the approved folder are allowed."
            )

        try:
            target = (base / relative).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise FileScopeError(
                f"The requested path is unavailable: {error}"
            ) from error

        if not target.is_relative_to(base):
            raise FileScopeError(
                "The requested path escapes the approved folder."
            )
        if not target.is_relative_to(self._home()):
            raise FileScopeError(
                "The requested path escapes the home directory."
            )
        return target

    def resolve_key(self, key: str, relative_path: str = "") -> Path:
        return self.child(self.base_for_key(key), relative_path)

    def resolve_path(self, folder_path: str, relative_path: str = "") -> Path:
        return self.child(self.base_for_path(folder_path), relative_path)
