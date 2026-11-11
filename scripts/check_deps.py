#!/usr/bin/env python3
"""Provisioning check for Misty.

Verifies, without network access and without installing anything:

a. every requirement in ``requirements.txt`` is importable at the pinned version,
b. every required system executable is on ``PATH``,
c. vendored binaries, model files, tool venvs and config files exist,
d. prints a compact PASS/FAIL report and exits non-zero on failure.

Usage::

    .venv/bin/python -B scripts/check_deps.py              # full report
    .venv/bin/python -B scripts/check_deps.py --quiet      # failures only
    .venv/bin/python -B scripts/check_deps.py --json       # machine-readable

References are injectable (``version_of``, ``which``, ``file_exists``) so the
unit tests never touch the real environment.

This module also regenerates ``MANIFEST.lock`` from the live machine::

    .venv/bin/python -B scripts/check_deps.py --write-lock
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = PROJECT_ROOT / "requirements.txt"
LOCK_PATH = PROJECT_ROOT / "MANIFEST.lock"

# Model files whose sha256 may take longer than this on a cold disk.
HASH_TIMEOUT_SECONDS = 60

# Distribution -> importable top-level module. Only distributions whose import
# name differs from the normalised distribution name need an entry; everything
# else is checked by importing the normalised name.
IMPORT_OVERRIDES = {
    "pillow": "PIL",
    "beautifulsoup4": "bs4",
    "dbus-python": "dbus",
    "dbus-next": "dbus_next",
    "pygobject": "gi",
    "pycairo": "cairo",
}

# Distributions whose import module cannot be derived from the name. Anything
# else falls back to the PEP 503 normalised name with '-' replaced by '_'.
CANONICAL_MODULES = {
    "pyside6": "PySide6",
    "shiboken6": "shiboken6",
}

PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;#]+)\s*(?P<comment>#.*)?$"
)


@dataclass(frozen=True)
class Result:
    kind: str  # "python" | "binary" | "file"
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)

    @property
    def failures(self) -> list[Result]:
        return [item for item in self.results if not item.ok]

    @property
    def passed(self) -> bool:
        return not self.failures

    def counts(self) -> dict[str, list[int]]:
        totals: dict[str, list[int]] = {}
        for item in self.results:
            bucket = totals.setdefault(item.kind, [0, 0])
            bucket[0] += 1
            if item.ok:
                bucket[1] += 1
        return totals


def normalise(name: str) -> str:
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str) -> list[tuple[str, str]]:
    """Return ``[(name, version), ...]`` from ``name==version`` lines."""
    pins: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_RE.match(line)
        if match is None:
            raise ValueError(f"not an exact == pin: {raw!r}")
        pins.append((match.group("name"), match.group("version")))
    return pins


def module_for(distribution: str) -> str:
    key = normalise(distribution)
    if key in IMPORT_OVERRIDES:
        return IMPORT_OVERRIDES[key]
    if key in CANONICAL_MODULES:
        return CANONICAL_MODULES[key]
    return key.replace("-", "_")


def is_directory_label(label: str) -> bool:
    return label in DIRECTORY_LABELS


def default_version_of(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def default_importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def check_python_pins(
    pins: list[tuple[str, str]],
    report: Report,
    version_of=default_version_of,
    importable=default_importable,
) -> None:
    seen: set[str] = set()
    for name, expected in pins:
        key = normalise(name)
        module = module_for(name)
        actual = version_of(key)
        if key in seen:
            report.add(Result(
                "python", f"{name}=={expected}", False,
                "duplicate pin",
            ))
            continue
        seen.add(key)
        if actual is None:
            report.add(Result(
                "python", f"{name}=={expected}", False, "not installed",
            ))
        elif actual != expected:
            report.add(Result(
                "python", f"{name}=={expected}", False, f"installed {actual}",
            ))
        elif not importable(module):
            report.add(Result(
                "python", f"{name}=={expected}", False,
                f"version ok, `import {module}` failed",
            ))
        else:
            report.add(Result(
                "python", f"{name}=={expected}", True, f"import {module}",
            ))


def check_binaries(
    binaries: list[tuple[str, bool]],
    report: Report,
    which=shutil.which,
    version_of=default_version_of,
) -> None:
    for command, required in binaries:
        found = which(command)
        if found:
            report.add(Result("binary", command, True, found))
        elif required:
            report.add(Result("binary", command, False, "missing (required)"))
        else:
            report.add(Result("binary", command, True, "missing (optional)"))


def check_paths(
    paths: list[tuple[str, Path, bool]],
    report: Report,
    file_exists=None,
) -> None:
    exists = file_exists or (lambda path: Path(path).is_file())
    for label, path, required in paths:
        if exists(path):
            report.add(Result("file", label, True, str(path)))
        elif required:
            report.add(Result("file", label, False, f"{path} missing (required)"))
        else:
            report.add(Result("file", label, True, f"{path} missing (optional)"))


# ---------------------------------------------------------------------------
# Manifest inventory shared by the checks and the lock generator
# ---------------------------------------------------------------------------

# (command, required, purpose)
REQUIRED_BINARIES: tuple[tuple[str, bool, str], ...] = (
    ("ffmpeg", True, "audio/video decoding for file transcription and whisper"),
    ("pdftotext", True, "PDF text extraction (poppler-utils)"),
    ("pandoc", True, "document conversion"),
    ("rg", True, "bounded text search (ripgrep)"),
    ("wl-copy", True, "Wayland clipboard copy"),
    ("wl-paste", True, "Wayland clipboard read"),
    ("xdotool", True, "X11 window/input fallback"),
    ("ydotool", True, "injected uinput keyboard input for desktop actions"),
    ("tesseract", True, "OCR of screenshots and scanned PDFs"),
    ("git", True, "vendored llama.cpp/whisper.cpp checkouts"),
    ("cmake", True, "builds kde-shortcut, llama.cpp, whisper.cpp"),
    ("g++", True, "C++ compiler for the native builds"),
    ("ninja", False, "faster CMake generator (optional)"),
    ("qdbus6", True, "KWin window enumeration over D-Bus"),
    ("kwin_wayland", False, "KDE screen capture scripting (desktop sessions only)"),
    ("wpctl", False, "volume control via PipeWire"),
    ("xdg-open", False, "opening folders/URLs in the desktop"),
    ("gtk-launch", False, "launching allowlisted .desktop applications"),
    ("xdg-user-dir", False, "resolving XDG user folders (Downloads etc.)"),
)

# Labels checked with is_dir() instead of is_file().
DIRECTORY_LABELS = frozenset({"playwright browsers"})

# (label, path, required) -- relative to the project root.
REQUIRED_PATHS: tuple[tuple[str, str, bool], ...] = (
    ("kde-shortcut helper", "kde-shortcut/build/misty-shortcut", True),
    ("llama-server", "third_party/llama.cpp/build/bin/llama-server", True),
    ("llama-mtmd-cli", "third_party/llama.cpp/build/bin/llama-mtmd-cli", True),
    ("llama vulkan backend", "third_party/llama.cpp/build/bin/libggml-vulkan.so", True),
    ("whisper-cli", "third_party/whisper.cpp/build/bin/whisper-cli", True),
    ("reasoning model", "models/Qwen3-4B-Q4_K_M.gguf", True),
    ("whisper model", "models/ggml-large-v3-turbo-q5_0.bin", True),
    ("vision model", "models/vision/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf", True),
    ("vision projector", "models/vision/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf", True),
    ("ocrmypdf (pipx venv)", "data/bin/ocrmypdf", True),
    ("playwright browsers", "data/playwright-browsers", True),
    ("SearXNG settings", "config/searxng/settings.yml", True),
    ("shortcut config", "config/shortcut.json", True),
    ("audio config", "config/audio.json", True),
)

MODEL_GLOBS: tuple[str, ...] = (
    "models/*.bin",
    "models/*.gguf",
    "models/vision/*/*.gguf",
)

MODEL_COMPONENTS: dict[str, str] = {
    "models/Qwen3-4B-Q4_K_M.gguf": "reasoning",
    "models/ggml-large-v3-turbo-q5_0.bin": "speech",
    "models/vision/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf": "vision",
    "models/vision/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf": "vision projector",
}


def run_checks(
    root: Path = PROJECT_ROOT,
    requirements: Path | None = None,
    version_of=default_version_of,
    importable=default_importable,
    which=shutil.which,
    is_file=None,
    is_dir=None,
) -> Report:
    root = Path(root)
    report = Report()
    requirements = Path(requirements or root / "requirements.txt")
    pins = parse_requirements(requirements.read_text(encoding="utf-8"))
    check_python_pins(pins, report, version_of=version_of, importable=importable)
    check_binaries(
        [(command, required) for command, required, _ in REQUIRED_BINARIES],
        report,
        which=which,
        version_of=version_of,
    )
    file_exists = is_file or (lambda path: Path(path).is_file())
    dir_exists = is_dir or (lambda path: Path(path).is_dir())
    for label, rel, required in REQUIRED_PATHS:
        exists = dir_exists if is_directory_label(label) else file_exists
        check_paths([(label, root / rel, required)], report, file_exists=exists)
    return report


def print_report(report: Report, quiet: bool = False, stream=None) -> None:
    stream = stream or sys.stdout
    if not quiet:
        width = max((len(item.name) for item in report.results), default=0)
        for item in report.results:
            status = "PASS" if item.ok else "FAIL"
            print(f"[{status}] {item.name:<{width}}  {item.detail}", file=stream)
    else:
        for item in report.failures:
            print(f"[FAIL] {item.name}: {item.detail}", file=stream)
    counts = report.counts()
    labels = {"python": "requirements", "binary": "binaries", "file": "files"}
    summary = ", ".join(
        f"{labels.get(kind, kind)} {ok}/{total}" for kind, (total, ok) in sorted(counts.items())
    )
    verdict = "PASS" if report.passed else "FAIL"
    print(f"\ncheck_deps: {verdict} ({summary})", file=stream)
    if report.failures:
        print("Missing or mismatched:", file=stream)
        for item in report.failures:
            print(f"  - {item.name}: {item.detail}", file=stream)


def report_to_json(report: Report) -> str:
    return json.dumps(
        {
            "passed": report.passed,
            "results": [
                {"kind": r.kind, "name": r.name, "ok": r.ok, "detail": r.detail}
                for r in report.results
            ],
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


# ---------------------------------------------------------------------------
# MANIFEST.lock generator
# ---------------------------------------------------------------------------

# Distributions that matter to Misty. ``source`` records where the observation
# came from: "venv" (pip installed into .venv) or "system" (inherited through
# include-system-site-packages = true).
PROJECT_DISTRIBUTIONS: tuple[dict[str, str], ...] = (
    {"name": "PySide6", "role": "Qt6 widgets, IPC, tray", "source": "system"},
    {"name": "shiboken6", "role": "PySide6 binding runtime", "source": "system"},
    {"name": "numpy", "role": "audio buffers (recorder, transcriber)", "source": "venv+system"},
    {"name": "sounddevice", "role": "microphone capture (PortAudio cffi)", "source": "venv"},
    {"name": "cffi", "role": "sounddevice backend", "source": "venv+system"},
    {"name": "pycparser", "role": "cffi dependency", "source": "venv+system"},
    {"name": "Pillow", "role": "screenshots, OCR and VLM image preparation", "source": "system"},
    {"name": "dbus-python", "role": "D-Bus (media, KWin capture)", "source": "system"},
    {"name": "dbus-next", "role": "async D-Bus KWin window scripting", "source": "system"},
    {"name": "PyGObject", "role": "AT-SPI accessibility (gi)", "source": "system"},
    {"name": "beautifulsoup4", "role": "HTML parsing for documents and web fetch", "source": "system"},
    {"name": "soupsieve", "role": "beautifulsoup4 selector dependency", "source": "system"},
    {"name": "playwright", "role": "browser automation with bundled Chromium", "source": "venv"},
    {"name": "psutil", "role": "system telemetry", "source": "system"},
)

VENDORED: tuple[dict[str, str], ...] = (
    {
        "name": "llama.cpp",
        "path": "third_party/llama.cpp",
        "commit": "a4d4f2fd4ec85ec6a0a9ece4870416c1b6cc24f5",
        "role": "llama-server (planner) and llama-mtmd-cli (VLM)",
        "artifacts": [
            "third_party/llama.cpp/build/bin/llama-server",
            "third_party/llama.cpp/build/bin/llama-mtmd-cli",
            "third_party/llama.cpp/build/bin/libggml-vulkan.so",
        ],
    },
    {
        "name": "whisper.cpp",
        "path": "third_party/whisper.cpp",
        "commit": "bd9e1a12352fa57e448bdea6a0649aa9c24c64aa",
        "role": "whisper-cli speech-to-text",
        "artifacts": ["third_party/whisper.cpp/build/bin/whisper-cli"],
    },
)

BINARY_VERSION_ARGS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("-version",),
    "pandoc": ("--version",),
    "rg": ("--version",),
    "wl-copy": ("--version",),
    "xdotool": ("--version",),
    "tesseract": ("--version",),
    "git": ("--version",),
    "cmake": ("--version",),
    "g++": ("--version",),
    "ninja": ("--version",),
    "wl-paste": ("--version",),
    # pdftotext, ydotool, wpctl, qdbus6, xdg-open, xdg-user-dir, gtk-launch and
    # kwin_wayland have no reliable version flag; their version comes from the
    # owning package via pacman below.
}


def _package_version(path: str | None) -> str | None:
    """Resolve an Arch-style package owning ``path`` (best effort).

    Uses ``LC_ALL=C`` so the parser sees the English wording.
    """
    if not path:
        return None
    try:
        result = subprocess.run(
            ["pacman", "-Qo", path],
            capture_output=True, text=True, timeout=3, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # English: "usr/bin/pdftotext is owned by poppler 26.10.0-2"
    # Polish (observed here): "... należy do poppler 26.10.0-2"
    text = result.stdout.strip()
    owner = None
    for marker in (" is owned by ", " należy do "):
        if marker in text:
            owner = text.split(marker, 1)[1].strip()
            break
    if not owner:
        return None
    parts = owner.rsplit(" ", 1)
    if len(parts) == 2:
        return f"{parts[0]} {parts[1]} (pacman)"
    return f"{owner} (pacman)"


def binary_version(command: str, path: str | None) -> str | None:
    if path is None:
        return None
    args = BINARY_VERSION_ARGS.get(command)
    if args:
        try:
            result = subprocess.run(
                [path, *args], capture_output=True, text=True,
                timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        lines = (result.stdout or result.stderr or "").strip().splitlines()
        if lines:
            return lines[0].strip()
    return _package_version(path)


def _sha256(path: Path, timeout: float) -> tuple[str | None, str]:
    """Return ``(hexdigest|None, status)`` honouring a per-file timeout."""
    deferred = f"timeout {int(timeout)}s exceeded -> hash deferred"
    started = os.times().elapsed
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if os.times().elapsed - started > timeout:
                    return None, deferred
    except OSError as error:
        return None, f"error: {error}"
    return digest.hexdigest(), "computed"


def model_inventory(root: Path, timeout: float = HASH_TIMEOUT_SECONDS) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    for pattern in MODEL_GLOBS:
        for path in sorted(Path(root).glob(pattern)):
            rel = path.relative_to(root).as_posix()
            if rel in seen or not path.is_file():
                continue
            seen.add(rel)
            stat = path.stat()
            sha, status = _sha256(path, timeout)
            entries.append({
                "path": rel,
                "component": MODEL_COMPONENTS.get(rel, "other"),
                "size_bytes": stat.st_size,
                "mtime_utc": _iso_utc(stat.st_mtime),
                "sha256": sha or "deferred",
                "sha256_status": status,
            })
    entries.sort(key=lambda item: item["path"])
    return entries


def _iso_utc(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def distributions_inventory(root: Path) -> tuple[list[dict], dict]:
    venv_site = Path(root) / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    all_entries: list[dict] = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"] or "(unnamed)"
        location = getattr(dist, "_path", None)
        location = str(Path(location).parent) if location else ""
        all_entries.append({
            "name": normalise(name),
            "version": dist.version,
            "location": "venv" if str(venv_site) in location else "system",
        })
    all_entries.sort(key=lambda item: (item["name"], item["version"], item["location"]))
    canonical = json.dumps(
        [f"{item['name']}=={item['version']}" for item in all_entries],
        separators=(",", ":"),
    ).encode("utf-8")

    project = []
    for spec in PROJECT_DISTRIBUTIONS:
        try:
            version = importlib.metadata.version(spec["name"])
        except importlib.metadata.PackageNotFoundError:
            version = None
        project.append({
            "name": spec["name"],
            "version": version,
            "role": spec["role"],
            "source": spec["source"],
        })
    project.sort(key=lambda item: item["name"].lower())
    summary = {
        "total_distributions": len(all_entries),
        "total_unique_names": len({item["name"] for item in all_entries}),
        "venv_distributions": len([e for e in all_entries if e["location"] == "venv"]),
        "system_distributions": len([e for e in all_entries if e["location"] == "system"]),
        "full_list_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    return project, summary


def build_manifest(root: Path = PROJECT_ROOT, timeout: float = HASH_TIMEOUT_SECONDS) -> dict:
    root = Path(root)
    pins = parse_requirements((root / "requirements.txt").read_text(encoding="utf-8"))
    cfg = {}
    cfg_path = root / ".venv" / "pyvenv.cfg"
    if cfg_path.is_file():
        for line in cfg_path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    executables = []
    for command, required, purpose in sorted(REQUIRED_BINARIES):
        path = shutil.which(command)
        executables.append({
            "command": command,
            "required": required,
            "purpose": purpose,
            "path": path,
            "version": binary_version(command, path),
        })
    vendored = []
    for item in VENDORED:
        entry = dict(item)
        repo = root / item["path"]
        commit = item["commit"]
        if (repo / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if result.returncode == 0:
                    commit = result.stdout.strip() or commit
            except (OSError, subprocess.SubprocessError):
                pass
        entry["commit"] = commit
        entry["artifacts"] = [
            {"path": rel, "present": (root / rel).exists()}
            for rel in item["artifacts"]
        ]
        vendored.append(entry)
    project, summary = distributions_inventory(root)
    return {
        "schema_version": 1,
        "generated_by": "scripts/check_deps.py --write-lock",
        "verified_at": _iso_utc(time.time()),
        "verification_run": "2026-11-10",
        "python": {
            "implementation": platform.python_implementation(),
            "current": platform.python_version(),
            "pyvenv_version": cfg.get("version"),
            "pyvenv_executable": cfg.get("executable"),
            "pyvenv_command": cfg.get("command"),
            "include_system_site_packages": cfg.get("include-system-site-packages") == "true",
            "venv_site_packages": ".venv/lib/python%s/site-packages" % (
                ".".join(platform.python_version().split(".")[:2])
            ),
            "system_site_packages": "/usr/lib/python%s/site-packages" % (
                ".".join(platform.python_version().split(".")[:2])
            ),
        },
        "requirements_pins": [{"name": n, "version": v} for n, v in pins],
        "python_distributions": project,
        "python_distributions_summary": summary,
        "native_executables": executables,
        "vendored_components": vendored,
        "models": model_inventory(root, timeout),
        "external_services": [
            {
                "name": "searxng",
                "kind": "system service (systemd, outside this project)",
                "settings_path": "config/searxng/settings.yml",
                "listening": "the live instance answers on 127.0.0.1:8888 "
                             "(settings.yml server.port); Misty's default "
                             "MISTY_SEARXNG_URL is http://127.0.0.1:8080",
            },
            {
                "name": "ocrmypdf",
                "kind": "pipx venv inside the project",
                "venv_path": "data/pipx/venvs/ocrmypdf",
                "launcher": "data/bin/ocrmypdf",
            },
        ],
        "config_files": [
            {"path": "config/shortcut.json", "active": True, "role": "hold-to-talk shortcut"},
            {"path": "config/audio.json", "active": True, "role": "microphone device"},
            {"path": "config/searxng/settings.yml", "active": True, "role": "SearXNG instance"},
            {"path": "config.toml", "active": False,
             "role": "descriptive only; runtime values come from constants and env vars"},
        ],
    }


def write_lock(root: Path = PROJECT_ROOT, timeout: float = HASH_TIMEOUT_SECONDS) -> Path:
    manifest = build_manifest(root, timeout=timeout)
    target = Path(root) / "MANIFEST.lock"
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true", help="failures only")
    parser.add_argument("--json", action="store_true", help="JSON report")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--write-lock", action="store_true",
        help="regenerate MANIFEST.lock instead of checking",
    )
    parser.add_argument(
        "--hash-timeout", type=float, default=HASH_TIMEOUT_SECONDS,
        help="per-model sha256 budget in seconds (default 60)",
    )
    args = parser.parse_args(argv)
    if args.write_lock:
        target = write_lock(Path(args.root), timeout=args.hash_timeout)
        print(f"wrote {target}")
        return 0
    report = run_checks(Path(args.root))
    if args.json:
        sys.stdout.write(report_to_json(report))
    else:
        print_report(report, quiet=args.quiet)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
