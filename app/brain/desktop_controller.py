"""Staged Cua desktop entry point: capability checks, never autonomous input.

The design's host capability/target-policy gates have not passed. Selection of
this backend must report that limitation rather than run legacy pointer tools.
The visual decision runtime is available separately for later conformance tests;
this entry point deliberately does not load it or request input permission.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from app.desktop.providers.base import DesktopProviderError
from app.desktop.providers.cua_mcp import CuaMcpProvider


@dataclass(frozen=True)
class DesktopReadiness:
    code: str
    catalog_available: bool = False
    visible_window_count: int = 0
    input_ready: bool = False
    cleanup_confirmed: bool = True

    def summary(self):
        if not self.cleanup_confirmed:
            return "Cua cleanup could not be confirmed. Desktop input remains disabled; nothing was clicked."
        if self.code == "canceled":
            return "Desktop capability checking was canceled. Nothing was clicked."
        if self.code == "no_visible_windows":
            return (
                "Cua connected, but it could not discover any visible windows in this process's desktop session. "
                "Run the read-only desktop diagnostic from your normal desktop terminal. "
                "Cua input remains disabled; I did not fall back to legacy clicks."
            )
        if self.code == "observation_only":
            return (
                f"Cua discovered {self.visible_window_count} visible window(s), but visual-target and input "
                "acceptance have not passed. This backend is currently read-only, so I did not click anything."
            )
        return (
            f"Cua desktop readiness failed ({self.code}). Desktop input remains disabled; "
            "I did not fall back to legacy clicks."
        )


class CuaReadinessController:
    """Own provider cleanup and expose only content-free readiness metadata."""

    def __init__(self, provider_factory=CuaMcpProvider, progress=None):
        self._factory = provider_factory
        self._progress = progress

    def _emit(self, title, status="planned"):
        if self._progress is not None:
            self._progress({"kind": "decision", "title": title,
                            "details": "Read-only Cua capability checking; no desktop input.",
                            "status": status})

    def check(self, *, cancel=None, timeout=20.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("A finite bounded readiness timeout is required")
        deadline = time.monotonic() + timeout
        provider = None
        result = DesktopReadiness("provider_error")
        self._emit("Checking desktop capabilities")
        try:
            if cancel is not None and cancel.is_set():
                result = DesktopReadiness("canceled")
            else:
                provider = self._factory()
                capability = provider.start(cancel=cancel, deadline=deadline)
                if capability.available is not True:
                    result = DesktopReadiness("provider_unavailable")
                else:
                    outcome = provider.call("list_windows", {"on_screen_only": True},
                                            cancel=cancel, deadline=deadline)
                    data = outcome.structured
                    windows = data.get("windows") if isinstance(data, dict) else None
                    if outcome.is_error:
                        result = DesktopReadiness("window_discovery_failed", True)
                    elif not isinstance(windows, list) or any(not isinstance(w, dict) for w in windows):
                        result = DesktopReadiness("window_result_invalid", True)
                    else:
                        # A provider's tool catalog or nonempty discovery is not
                        # authority to claim capture/grounding/input readiness.
                        visible = [w for w in windows if w.get("is_on_screen") is not False]
                        result = DesktopReadiness("observation_only" if visible else "no_visible_windows",
                                                  True, len(visible))
        except DesktopProviderError as error:
            code = error.code if error.code in {
                "canceled", "timeout", "unavailable", "version_mismatch", "catalog_drift",
                "dependency_missing", "protocol_error", "transport_closed", "rpc_error",
            } else "provider_error"
            result = DesktopReadiness(code)
        except Exception:
            # Native exceptions can include app titles/paths; no private prose
            # is echoed into persistent activity or a user-facing error.
            result = DesktopReadiness("provider_error")
        finally:
            if provider is not None:
                try:
                    stopped = provider.close().stopped is True
                except Exception:
                    stopped = False
                if not stopped:
                    result = DesktopReadiness("cleanup_unconfirmed", result.catalog_available,
                                              result.visible_window_count, False, False)
        if cancel is not None and cancel.is_set() and result.cleanup_confirmed:
            result = DesktopReadiness("canceled", result.catalog_available,
                                      result.visible_window_count)
        self._emit("Desktop input not enabled" if result.code != "canceled" else "Desktop check canceled",
                   "failure" if result.code != "canceled" else "info")
        return result


def run_cua_readiness(agent):
    controller = CuaReadinessController(progress=agent._emit_activity)
    return controller.check(cancel=getattr(agent, "cancel_event", None)).summary()
