"""Bounded, read-only, home-first filename discovery."""

import os
import time
from collections import deque
from pathlib import Path


class FileSearchService:
    EXCLUDED_NAMES = {
        ".cache", ".git", ".hg", ".svn", ".trash", ".local/share/Trash",
        "__pycache__", "node_modules", ".venv", "venv", "env",
        ".tox", ".nox", "site-packages", "dist-packages",
        "build", "dist", "target", ".npm", ".gradle",
        ".cargo", ".rustup", ".thumbnails",
    }

    PRIORITY_NAMES = (
        "Pobrane", "Downloads", "Documents", "Dokumenty",
        "Desktop", "Pulpit", "Pictures", "Obrazy",
        "Music", "Muzyka", "Videos", "Wideo",
    )

    def __init__(self, home=None, max_seconds=8.0, max_directories=4000):
        self.home = Path(home or Path.home()).expanduser()
        self.max_seconds = max_seconds
        self.max_directories = max_directories

    @classmethod
    def _excluded(cls, relative_path, name):
        if name.casefold() in cls.EXCLUDED_NAMES:
            return True
        normalized = relative_path.replace(os.sep, "/").casefold()
        return normalized == ".local/share/trash" or normalized.startswith(
            ".local/share/trash/"
        )

    def search(self, query, limit=30):
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            return {"success": False, "message": "Search text must contain 1–200 characters."}
        if (
            not isinstance(limit, int) or isinstance(limit, bool)
            or limit < 1 or limit > 50
        ):
            return {"success": False, "message": "Result limit must be between 1 and 50."}

        query = query.strip().casefold()
        try:
            home = self.home.resolve(strict=True)
            if not home.is_dir():
                return {"success": False, "message": "The home directory is unavailable."}
        except (OSError, RuntimeError) as error:
            return {"success": False, "message": f"Cannot access the home directory: {error}"}

        # Search common user folders first, then the rest of home.
        roots = []
        for name in self.PRIORITY_NAMES:
            candidate = home / name
            try:
                if candidate.is_dir() and not candidate.is_symlink():
                    roots.append(candidate)
            except OSError:
                pass
        roots.append(home)

        queue = deque(roots)
        visited = set()
        results = []
        inaccessible = []
        scanned = 0
        started = time.monotonic()
        truncated = False

        while queue:
            if time.monotonic() - started >= self.max_seconds:
                truncated = True
                break
            if scanned >= self.max_directories:
                truncated = True
                break
            if len(results) >= limit:
                truncated = True
                break

            directory = queue.popleft()
            try:
                stat = directory.stat(follow_symlinks=False)
                identity = (stat.st_dev, stat.st_ino)
                if identity in visited:
                    continue
                visited.add(identity)
                if directory.is_symlink():
                    continue
                relative_dir = directory.relative_to(home)
                relative_dir_text = str(relative_dir)
                if relative_dir_text == ".":
                    relative_dir_text = ""
                if relative_dir_text and any(
                    self._excluded("/".join(relative_dir.parts[:i]), part)
                    for i, part in enumerate(relative_dir.parts, 1)
                ):
                    continue
            except (OSError, ValueError, RuntimeError):
                continue

            scanned += 1
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name.casefold())
            except OSError as error:
                inaccessible.append(str(directory))
                continue

            child_directories = []
            for entry in entries:
                if time.monotonic() - started >= self.max_seconds:
                    truncated = True
                    break

                name = entry.name
                relative = (
                    f"{relative_dir_text}/{name}" if relative_dir_text else name
                )

                try:
                    if entry.is_symlink():
                        continue
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    inaccessible.append(str(directory / name))
                    continue

                if query in name.casefold():
                    results.append({
                        "name": name,
                        "path": str(directory / name),
                        "type": "directory" if is_directory else "file",
                    })
                    if len(results) >= limit:
                        truncated = True
                        break

                if is_directory and not self._excluded(relative, name):
                    child_directories.append(directory / name)

            if truncated:
                break
            queue.extend(child_directories)

        return {
            "success": True,
            "message": (
                f"Found {len(results)} matching item(s) in {scanned} directories."
            ),
            "query": query,
            "results": results,
            "truncated": truncated,
            "directories_scanned": scanned,
            "inaccessible_count": len(inaccessible),
            "inaccessible_paths": inaccessible[:5],
        }
