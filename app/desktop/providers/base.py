"""Small provider-neutral records; no UI, model or native runtime imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class DesktopProviderError(RuntimeError):
    """A content-free provider failure with an explicit input outcome."""

    def __init__(self, code: str, message: str, *, unknown_outcome: bool = False):
        super().__init__(message)
        self.code = code
        self.unknown_outcome = unknown_outcome


@dataclass(frozen=True)
class CapabilityReport:
    available: bool
    provider: str
    version: str | None = None
    tool_names: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    platform_verified: bool = False


@dataclass(frozen=True)
class ToolOutcome:
    """Canonical tool data stays private; delivery never implies verification."""

    tool_name: str
    raw: dict[str, Any]
    provider_generation: str
    is_error: bool = False
    verified: bool = False

    @property
    def content(self):
        return self.raw.get("content", [])

    @property
    def structured(self):
        return self.raw.get("structuredContent")


@dataclass(frozen=True)
class CleanupOutcome:
    stopped: bool
    diagnostics: tuple[str, ...] = field(default_factory=tuple)
