"""Foreground-only KWin/OCR pilot with host approval and one-use targets.

This provider is explicitly selected, not a Cua fallback/background equivalent.
It never focuses a window or loads a model. Synchronous dependencies own their
blocking timeouts; cancellation/deadlines are admission checks before/after each
stage. Rechecking focus is not an atomic OS guarantee against human input.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from io import BytesIO
import math
import threading
import time
import uuid

from PIL import Image

from app.desktop.context import DesktopContext
from app.desktop.input import DesktopInput, pointer_click
from app.desktop.providers.base import DesktopProviderError

MAX_WINDOWS = 512
MAX_ELEMENTS = 512
MAX_PIXELS = 16_000_000
MAX_IMAGE_BYTES = 16_000_000


@dataclass(frozen=True)
class KWinObservation:
    observation_id: str
    target: dict
    image: bytes
    frame_id: str
    elements: list[dict]
    digest: str

    def __repr__(self):
        return "KWinObservation(<private>)"


@dataclass(frozen=True, repr=False)
class _Snapshot:
    observation_id: str
    target: dict
    frame_id: str
    digest: str
    centers: dict


def _error(code):
    return DesktopProviderError(code, "Native KWin operation could not be admitted.")


def _check(cancel, deadline):
    if deadline is not None and (isinstance(deadline, bool)
            or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise _error("invalid_deadline")
    if cancel is not None and cancel.is_set():
        raise _error("canceled")
    if deadline is not None and time.monotonic() >= deadline:
        raise _error("timeout")


def _geometry(value):
    if not isinstance(value, dict):
        raise _error("invalid_context")
    result = {}
    for key in ("x", "y", "width", "height"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int):
            raise _error("invalid_context")
        result[key] = item
    if result["width"] <= 0 or result["height"] <= 0:
        raise _error("invalid_context")
    return result


def _intersects(first, second):
    return (first["x"] < second["x"] + second["width"]
            and second["x"] < first["x"] + first["width"]
            and first["y"] < second["y"] + second["height"]
            and second["y"] < first["y"] + first["height"])


class KWinDesktopProvider:
    """A registry-injected, click-only approved foreground pilot.

    ``approved`` is a literal host-only boolean and must not be included in a
    model tool schema. Returned metadata is detached from trusted target/token
    state. Every external observe attempt invalidates all previous tokens.
    """

    def __init__(self, registry):
        self.registry = registry
        vision = getattr(registry, "vision_service", None)
        self._capture = getattr(vision, "capture_service", None)
        self._ocr = getattr(vision, "ocr_service", None)
        context_provider = getattr(self._capture, "context_provider", None)
        if not callable(context_provider):
            executor = getattr(registry, "executor", None)
            service = getattr(executor, "window_service", None)
            if service is not None:
                context_provider = DesktopContext(service).snapshot
                # The existing crop/frame resolver must use the same validated
                # context source. Bind only when no source was configured.
                if self._capture is not None:
                    self._capture.context_provider = context_provider
        self._context_provider = context_provider
        self._snapshot = None
        self._lock = threading.RLock()

    def discover(self):
        if not callable(self._context_provider):
            raise _error("context_unavailable")
        try:
            raw = self._context_provider()
            if not isinstance(raw, dict) or raw.get("success") is not True:
                raise _error("context_unavailable")
            raw_windows, raw_screens = raw.get("windows"), raw.get("screens")
            overlays = raw.get("overlays", [])
            if (not isinstance(raw_windows, list) or not isinstance(raw_screens, list)
                    or not isinstance(overlays, list) or not raw_screens
                    or len(raw_windows) > MAX_WINDOWS or len(overlays) > MAX_WINDOWS
                    or len(raw_screens) > 32):
                raise _error("invalid_context")
            result = deepcopy(raw)
            ids = set()
            active = 0
            for window in result["windows"]:
                if (not isinstance(window, dict) or not isinstance(window.get("id"), str)
                        or not window["id"] or window["id"] in ids
                        or isinstance(window.get("pid"), bool)
                        or not isinstance(window.get("pid"), int) or window["pid"] <= 0
                        or any(not isinstance(window.get(key), bool)
                               for key in ("active", "minimized", "on_current_desktop"))):
                    raise _error("invalid_context")
                ids.add(window["id"])
                active += window["active"] is True
                window["geometry"] = _geometry(window.get("geometry"))
            if active > 1:
                raise _error("invalid_context")
            names = set()
            for screen in result["screens"]:
                if (not isinstance(screen, dict) or not isinstance(screen.get("name"), str)
                        or not screen["name"] or screen["name"] in names):
                    raise _error("invalid_context")
                names.add(screen["name"])
                screen["geometry"] = _geometry(screen.get("geometry"))
            for overlay in result.get("overlays", []):
                if not isinstance(overlay, dict):
                    raise _error("invalid_context")
                overlay["geometry"] = _geometry(overlay.get("geometry"))
            result["active_window"] = next((deepcopy(w) for w in result["windows"] if w["active"]), None)
            result["window_count"] = len(result["windows"])
            result["screen_count"] = len(result["screens"])
            return result
        except DesktopProviderError:
            raise
        except Exception:
            raise _error("context_unavailable") from None

    @staticmethod
    def _target(context, window_id, expected=None, *, require_active=True):
        if not isinstance(window_id, str) or not window_id:
            raise _error("invalid_target")
        matches = [window for window in context["windows"] if window["id"] == window_id]
        if len(matches) != 1:
            raise _error("target_unavailable")
        target = matches[0]
        if expected is not None and (target["pid"] != expected["pid"]
                                    or target["geometry"] != expected["geometry"]
                                    or ("screen" in target and "screen" in expected
                                        and target["screen"] != expected["screen"])):
            raise _error("stale_target")
        if target["minimized"] or not target["on_current_desktop"]:
            raise _error("target_not_visible")
        if require_active and not target["active"]:
            raise _error("target_not_active")
        for foreign in [*context["windows"], *context.get("overlays", [])]:
            if foreign.get("id") == window_id:
                continue
            if foreign.get("minimized") is True or foreign.get("on_current_desktop") is False:
                continue
            if _intersects(target["geometry"], foreign["geometry"]):
                raise _error("target_occluded")
        return target

    def _visible_capture(self, target, cancel, deadline):
        _check(cancel, deadline)
        if not callable(getattr(self._capture, "capture", None)):
            raise _error("capture_unavailable")
        try:
            captured = self._capture.capture(mode="window", window_id=target["id"], visible_region_only=True)
            _check(cancel, deadline)
            if (captured.window_id != target["id"] or captured.window_geometry != target["geometry"]
                    or captured.capture_kind != "visible_region"
                    or not isinstance(captured.frame, dict) or captured.frame.get("grounded") is not True
                    or not isinstance(captured.frame.get("frame_id"), str) or not captured.frame["frame_id"]
                    or not isinstance(captured.image, Image.Image)):
                raise _error("ungrounded_capture")
            image = captured.image.convert("RGB")
            if (image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_PIXELS
                    or image.size != (target["geometry"]["width"], target["geometry"]["height"])):
                raise _error("ungrounded_capture")
            stream = BytesIO()
            image.save(stream, format="PNG")
            payload = stream.getvalue()
            if len(payload) > MAX_IMAGE_BYTES:
                raise _error("capture_limit")
            digest = hashlib.sha256(payload).hexdigest()
            return image, payload, digest, captured.frame["frame_id"]
        except DesktopProviderError:
            raise
        except Exception:
            raise _error("capture_failed") from None

    def observe(self, window_id, cancel=None, deadline=None):
        with self._lock:
            self._snapshot = None
            _check(cancel, deadline)
            target = self._target(self.discover(), window_id, require_active=False)
            image, payload, digest, frame_id = self._visible_capture(target, cancel, deadline)
            _check(cancel, deadline)
            if not callable(getattr(self._ocr, "recognize", None)):
                raise _error("ocr_unavailable")
            try:
                ocr = self._ocr.recognize(image.copy())
                _check(cancel, deadline)
                if getattr(ocr, "image_size", None) != image.size:
                    raise _error("ocr_invalid")
                words = ocr.words
                if not isinstance(words, (tuple, list)):
                    raise _error("ocr_invalid")
                elements, centers = [], {}
                for word in words[:MAX_ELEMENTS]:
                    text = getattr(word, "text", None)
                    values = [getattr(word, key, None) for key in ("left", "top", "width", "height")]
                    if (not isinstance(text, str) or not text.strip()
                            or any(isinstance(value, bool) or not isinstance(value, int) for value in values)):
                        continue
                    x, y, width, height = values
                    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > image.width or y + height > image.height:
                        continue
                    token = uuid.uuid4().hex
                    centers[token] = (x + width // 2, y + height // 2)
                    elements.append({"element_token": token, "text": text.strip()[:256],
                                     "x": x, "y": y, "width": width, "height": height})
            except DesktopProviderError:
                raise
            except Exception:
                raise _error("ocr_failed") from None
            _check(cancel, deadline)
            current = self._target(self.discover(), window_id, expected=target, require_active=False)
            observation_id = uuid.uuid4().hex
            self._snapshot = _Snapshot(observation_id, deepcopy(current), frame_id, digest, centers)
            return KWinObservation(observation_id, deepcopy(current), payload, frame_id,
                                   deepcopy(elements), digest)

    @staticmethod
    def _result(code, success=False, not_executed=True):
        return {"success": success, "not_executed": not_executed, "code": code, "verified": False}

    def click(self, observation_id, element_token, *, approved=False, cancel=None, deadline=None):
        with self._lock:
            if approved is not True:
                return self._result("approval_required")
            backend = getattr(self.registry, "input_backend", None)
            if backend is None or not callable(getattr(backend, "execute", None)):
                return self._result("no_input_backend")
            try:
                _check(cancel, deadline)
                saved = self._snapshot
                if (saved is None or not isinstance(observation_id, str)
                        or saved.observation_id != observation_id
                        or not isinstance(element_token, str) or element_token not in saved.centers):
                    return self._result("stale_token")
                target = self._target(self.discover(), saved.target["id"], expected=saved.target)
                _, _, digest, fresh_frame_id = self._visible_capture(target, cancel, deadline)
                if digest != saved.digest:
                    raise _error("stale_image")
                _check(cancel, deadline)
                # Approval/model work can outlive the original frame TTL. The
                # fresh capture has identical pixels and exact target geometry;
                # resolve the same private local center against that trusted
                # fresh mapping exactly once, never a model-supplied frame.
                x, y = self._capture.resolve_input_point(fresh_frame_id, *saved.centers[element_token])
                action = pointer_click(x, y, button="left", count=1)
                self._target(self.discover(), saved.target["id"], expected=saved.target)
                _check(cancel, deadline)
                # Any admitted input invalidates the entire old observation,
                # including all tokens, before the backend can see the action.
                self._snapshot = None
            except DesktopProviderError as error:
                self._snapshot = None
                return self._result(error.code)
            except Exception:
                self._snapshot = None
                return self._result("preflight_failed")
            try:
                result = DesktopInput(backend).execute(action)
            except Exception:
                return self._result("input_outcome_unknown", not_executed=False)
            return self._result("input_delivered" if result.get("success") is True else "input_outcome_unknown",
                                success=result.get("success") is True, not_executed=False)
