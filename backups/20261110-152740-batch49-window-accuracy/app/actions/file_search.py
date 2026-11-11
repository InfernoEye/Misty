"""Bounded, read-only, home-first filename discovery."""

import os
import re
import time
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path


class FileSearchService:
    FUZZY_MIN_RATIO = 0.8

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

    def __init__(
        self,
        home=None,
        max_seconds=8.0,
        max_directories=4000,
        max_queue=10000,
    ):
        self.home = Path(home or Path.home()).expanduser()
        self.max_seconds = max_seconds
        self.max_directories = max_directories
        self.max_queue = max_queue

    @staticmethod
    def _tokens(value):
        return re.findall(r"[^\W_]+", str(value).casefold(), flags=re.UNICODE)

    @classmethod
    def _match_score(cls, name, normalized_query, query_tokens):
        """Return a bounded match score, or None when the name does not match.

        Exact substring matches win. Otherwise every query token must match a
        filename token exactly or closely enough to tolerate transcription
        differences such as "misti" for "misty".
        """
        normalized_name = re.sub(
            r"[^\w]+", "", str(name).casefold(), flags=re.UNICODE
        )
        if normalized_query and normalized_query in normalized_name:
            return 1.0

        if not query_tokens:
            return None

        name_tokens = cls._tokens(name)
        if not name_tokens:
            return None

        ratios = []
        for token in query_tokens:
            best = 0.0
            for candidate in name_tokens:
                if token == candidate:
                    best = 1.0
                    break
                if len(token) < 4:
                    continue
                best = max(
                    best,
                    SequenceMatcher(None, token, candidate).ratio(),
                )
            if best < cls.FUZZY_MIN_RATIO:
                return None
            ratios.append(best)
        return sum(ratios) / len(ratios)

    @classmethod
    def _excluded(cls, relative_path, name):
        if name.casefold() in cls.EXCLUDED_NAMES:
            return True
        normalized = relative_path.replace(os.sep, "/").casefold()
        return normalized == ".local/share/trash" or normalized.startswith(
            ".local/share/trash/"
        )

    def search(self, query, limit=30, root=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            return {"success": False, "message": "Search text must contain 1–200 characters."}
        if (
            not isinstance(limit, int) or isinstance(limit, bool)
            or limit < 1 or limit > 50
        ):
            return {"success": False, "message": "Result limit must be between 1 and 50."}

        query = query.strip().casefold()
        query_tokens = self._tokens(query)
        normalized_query = "".join(query_tokens)
        try:
            home = self.home.resolve(strict=True)
            if not home.is_dir():
                return {"success": False, "message": "The home directory is unavailable."}
        except (OSError, RuntimeError) as error:
            return {"success": False, "message": f"Cannot access the home directory: {error}"}

        scope = None
        if root is not None:
            try:
                scope = Path(root).resolve(strict=True)
            except (OSError, RuntimeError) as error:
                return {
                    "success": False,
                    "message": f"Cannot access the requested search folder: {error}",
                }
            if not scope.is_dir():
                return {
                    "success": False,
                    "message": "The requested search folder is not a directory.",
                }
            if not scope.is_relative_to(home):
                return {
                    "success": False,
                    "message": (
                        "The requested search folder is outside the home directory."
                    ),
                }
            roots = [scope]
        else:
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
        truncation_reason = None

        while queue:
            if time.monotonic() - started >= self.max_seconds:
                truncated = True
                truncation_reason = "time budget"
                break
            if scanned >= self.max_directories:
                truncated = True
                truncation_reason = "directory budget"
                break
            if len(results) >= limit:
                truncated = True
                truncation_reason = "result limit"
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
                iterator = os.scandir(directory)
            except OSError:
                inaccessible.append(str(directory))
                continue

            child_directories = []
            try:
                with iterator:
                    for entry in iterator:
                        if time.monotonic() - started >= self.max_seconds:
                            truncated = True
                            truncation_reason = "time budget"
                            break

                        name = entry.name
                        relative = (
                            f"{relative_dir_text}/{name}"
                            if relative_dir_text else name
                        )

                        try:
                            if entry.is_symlink():
                                continue
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            inaccessible.append(str(directory / name))
                            continue

                        score = self._match_score(
                            name, normalized_query, query_tokens
                        )
                        if score is not None:
                            results.append({
                                "name": name,
                                "path": str(directory / name),
                                "type": "directory" if is_directory else "file",
                                "match_score": round(score, 3),
                            })
                            if len(results) >= limit:
                                truncated = True
                                truncation_reason = "result limit"
                                break

                        if is_directory and not self._excluded(relative, name):
                            child_directories.append(directory / name)
            except OSError:
                inaccessible.append(str(directory))
                continue

            if truncated:
                break
            if len(queue) + len(child_directories) > self.max_queue:
                truncated = True
                truncation_reason = "queue budget"
                child_directories = child_directories[
                    : max(0, self.max_queue - len(queue))
                ]
            queue.extend(child_directories)

        results.sort(
            key=lambda item: (-item["match_score"], item["name"].casefold())
        )

        return {
            "success": True,
            "message": (
                f"Found {len(results)} matching item(s) in {scanned} directories."
            ),
            "query": query,
            "scoped": scope is not None,
            "scope": str(scope) if scope is not None else None,
            "results": results,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
            "directories_scanned": scanned,
            "inaccessible_count": len(inaccessible),
            "inaccessible_paths": inaccessible[:5],
        }
