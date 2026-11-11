"""Isolated MPRIS media-player discovery and transport control.

The controller speaks only the standard MPRIS 2 interfaces over the current
user's session D-Bus.  It does not use Qt, the network, a shell, or any model,
and it never touches a player outside the allowlisted MPRIS transport methods.

Security properties:

* Only well-known bus names shaped like ``org.mpris.MediaPlayer2.<player>`` are
  ever queried.  A caller cannot pass an arbitrary bus name or object path.
* Only ``play``, ``pause``, ``play_pause``, ``next``, ``previous`` and ``stop``
  can reach a player.  Volume, seeking, shuffling, looping and playback-rate
  changes are deliberately not exposed.
* Every callsite returns a JSON-safe dictionary with a fixed failure code.
  Failure messages never include track titles, artist names, or raw D-Bus
  error text.
* Track metadata is bounded in both field count and string length before it is
  returned.  Nothing is logged.
* Selection never guesses: when several players are present and none is
  uniquely playing, the caller receives the bounded candidate list instead of
  a command being sent.

The optional ``bus`` argument accepts an injected session-bus-like object.  It
must provide ``list_names()`` and ``get_object(bus_name, object_path)``; the
returned proxy must provide ``get_dbus_method(name, interface)``.  This keeps
the module fully testable without a real player.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping

__all__ = [
    "ALLOWED_ACTIONS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAILURE_CODES",
    "MAX_PLAYERS",
    "MAX_TEXT_LENGTH",
    "MediaController",
    "OBJECT_PATH",
    "PLAYER_INTERFACE",
    "ROOT_INTERFACE",
]


OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_INTERFACE = "org.mpris.MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

MPRIS_PREFIX = "org.mpris.MediaPlayer2"
PLAYER_NAME_PATTERN = re.compile(
    r"^org\.mpris\.MediaPlayer2(?:\.[A-Za-z0-9_-]+)+$"
)

ALLOWED_ACTIONS = frozenset(
    {"play", "pause", "play_pause", "next", "previous", "stop"}
)
_ACTION_METHODS = {
    "play": "Play",
    "pause": "Pause",
    "play_pause": "PlayPause",
    "next": "Next",
    "previous": "Previous",
    "stop": "Stop",
}
_ACTION_MESSAGES = {
    "play": "Playback started.",
    "pause": "Playback paused.",
    "play_pause": "Playback toggled.",
    "next": "Skipped to the next track.",
    "previous": "Returned to the previous track.",
    "stop": "Playback stopped.",
}

FAILURE_CODES = frozenset(
    {
        "invalid_action",
        "invalid_player",
        "no_player",
        "ambiguous_player",
        "dbus_error",
        "unsupported",
    }
)
_MESSAGES = {
    "invalid_action": "That media action is not supported.",
    "invalid_player": "That media player is not a valid MPRIS player name.",
    "no_player": "No MPRIS media player is available.",
    "ambiguous_player": "Several media players are available; choose one.",
    "dbus_error": "The media player could not be reached over D-Bus.",
    "unsupported": "The media player does not expose the required MPRIS interface.",
}

MAX_PLAYERS = 16
MAX_TEXT_LENGTH = 200
MAX_BUS_NAME_LENGTH = 255
MAX_METADATA_ITEMS = 16
MAX_POSITION_SECONDS = 86_400.0
DEFAULT_TIMEOUT_SECONDS = 2.0

_UNSUPPORTED_ERROR_SUFFIXES = frozenset(
    {"UnknownMethod", "UnknownInterface", "UnknownProperty", "NotSupported"}
)


class _MediaFailure(Exception):
    """Internal fixed-code failure with optional, already-bounded candidates."""

    def __init__(self, code, candidates=None):
        if code not in FAILURE_CODES:
            raise ValueError(f"Unknown media failure code: {code!r}")
        super().__init__(_MESSAGES[code])
        self.code = code
        self.candidates = candidates


def _failure(code, candidates=None):
    result = {
        "success": False,
        "code": code,
        "message": _MESSAGES[code],
    }
    if candidates is not None:
        result["candidates"] = [dict(item) for item in candidates[:MAX_PLAYERS]]
    return result


def _bounded_text(value):
    """Return printable, single-line, length-bounded text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return ""
    elif not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ""

    text = "".join(
        character
        for character in value
        if character.isprintable() or character in {" ", "\t"}
    )
    text = " ".join(text.split())
    if len(text) > MAX_TEXT_LENGTH:
        text = text[: MAX_TEXT_LENGTH - 3] + "..."
    return text


def _metadata_text(value):
    """Flatten a scalar or short list-like MPRIS text value."""
    if isinstance(value, (list, tuple)):
        parts = []
        for item in list(value)[:MAX_METADATA_ITEMS]:
            part = _bounded_text(item)
            if part:
                parts.append(part)
        return _bounded_text(", ".join(parts))
    return _bounded_text(value)


def _bounded_seconds(value):
    """Convert MPRIS microseconds to bounded seconds."""
    if value is None or isinstance(value, bool):
        return None
    try:
        microseconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if microseconds < 0:
        microseconds = 0
    seconds = microseconds / 1_000_000.0
    return round(min(seconds, MAX_POSITION_SECONDS), 3)


def _error_name(error):
    getter = getattr(error, "get_dbus_name", None)
    if callable(getter):
        try:
            name = getter()
        except Exception:
            name = None
        if isinstance(name, str) and name:
            return name

    for attribute in ("_dbus_error_name", "dbus_error_name"):
        name = getattr(error, attribute, None)
        if isinstance(name, str) and name:
            return name
    return ""


def _classify_error(error):
    """Translate an injected or real D-Bus exception into a fixed code."""
    if isinstance(error, _MediaFailure):
        return error

    name = _error_name(error)
    if name and name.rsplit(".", 1)[-1] in _UNSUPPORTED_ERROR_SUFFIXES:
        return _MediaFailure("unsupported")
    return _MediaFailure("dbus_error")


class MediaController:
    """Read MPRIS state and send allowlisted transport commands.

    All public methods are thread-safe and return JSON-safe dictionaries.  A
    controller created with ``bus=None`` connects lazily on first use, so
    importing or constructing it never touches the session bus.
    """

    def __init__(self, bus=None, timeout=DEFAULT_TIMEOUT_SECONDS):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < float(timeout) <= 10.0
        ):
            raise ValueError("timeout must be between 0 and 10 seconds.")
        self._bus = bus
        self._timeout = float(timeout)
        self._lock = threading.RLock()

    # -- public API ---------------------------------------------------------

    def list_players(self):
        """Return bounded MPRIS players visible on the session bus."""
        with self._lock:
            try:
                players = self._read_players()
            except _MediaFailure as failure:
                return _failure(failure.code)
            except Exception:
                return _failure("dbus_error")
        return {
            "success": True,
            "code": None,
            "message": f"Found {len(players)} media player(s).",
            "players": players,
        }

    def now_playing(self, player=None):
        """Return bounded metadata for one unambiguously selected player.

        ``position`` and ``duration`` are seconds (clamped to one day) or
        ``None`` when the player does not expose them.
        """
        with self._lock:
            try:
                self._validate_requested_player(player)
                players = self._read_players()
                selected = self._select_player(players, player)
                proxy = self._object(selected["bus_name"])
                metadata = self._get_property(
                    proxy, PLAYER_INTERFACE, "Metadata"
                )
                if not isinstance(metadata, Mapping):
                    raise _MediaFailure("unsupported")
                position = self._get_property(
                    proxy, PLAYER_INTERFACE, "Position", optional=True
                )
                title = _metadata_text(metadata.get("xesam:title"))
                artist = _metadata_text(metadata.get("xesam:artist"))
                album = _metadata_text(metadata.get("xesam:album"))
                duration = _bounded_seconds(metadata.get("mpris:length"))
            except _MediaFailure as failure:
                return _failure(failure.code, failure.candidates)
            except Exception as error:
                failure = _classify_error(error)
                return _failure(failure.code, failure.candidates)

        return {
            "success": True,
            "code": None,
            "message": "Current media metadata retrieved.",
            "player": selected["bus_name"],
            "identity": selected["identity"],
            "title": title,
            "artist": artist,
            "album": album,
            "status": selected["playback_status"],
            "position": _bounded_seconds(position),
            "duration": duration,
        }

    def command(self, action, player=None):
        """Send one allowlisted transport action to one selected player."""
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            return _failure("invalid_action")

        with self._lock:
            try:
                self._validate_requested_player(player)
                players = self._read_players()
                selected = self._select_player(players, player)
                proxy = self._object(selected["bus_name"])
                self._call_player_method(
                    proxy, _ACTION_METHODS[action]
                )
            except _MediaFailure as failure:
                return _failure(failure.code, failure.candidates)
            except Exception as error:
                failure = _classify_error(error)
                return _failure(failure.code, failure.candidates)

        return {
            "success": True,
            "code": None,
            "message": _ACTION_MESSAGES[action],
            "player": selected["bus_name"],
            "identity": selected["identity"],
            "action": action,
        }

    # -- selection ----------------------------------------------------------

    @staticmethod
    def _validate_requested_player(player):
        if player is None:
            return
        if (
            not isinstance(player, str)
            or len(player) > MAX_BUS_NAME_LENGTH
            or PLAYER_NAME_PATTERN.fullmatch(player) is None
        ):
            raise _MediaFailure("invalid_player")

    @staticmethod
    def _select_player(players, requested):
        if requested is not None:
            for item in players:
                if item["bus_name"] == requested:
                    return item
            raise _MediaFailure("no_player")

        if not players:
            raise _MediaFailure("no_player")
        if len(players) == 1:
            return players[0]

        playing = [
            item
            for item in players
            if item["playback_status"] == "Playing"
        ]
        if len(playing) == 1:
            return playing[0]

        raise _MediaFailure("ambiguous_player", candidates=players)

    # -- D-Bus plumbing -----------------------------------------------------

    def _get_bus(self):
        if self._bus is not None:
            return self._bus

        try:
            import dbus
        except ImportError as error:
            raise _MediaFailure("unsupported") from error

        try:
            self._bus = dbus.SessionBus()
        except Exception as error:
            raise _classify_error(error) from error
        return self._bus

    def _object(self, bus_name):
        try:
            return self._get_bus().get_object(bus_name, OBJECT_PATH)
        except _MediaFailure:
            raise
        except Exception as error:
            raise _classify_error(error) from error

    def _get_property(self, proxy, interface, name, *, optional=False):
        try:
            getter = proxy.get_dbus_method("Get", PROPERTIES_INTERFACE)
            return getter(interface, name, timeout=self._timeout)
        except Exception as error:
            failure = _classify_error(error)
            if optional and failure.code == "unsupported":
                return None
            raise failure from error

    def _call_player_method(self, proxy, method_name):
        try:
            method = proxy.get_dbus_method(method_name, PLAYER_INTERFACE)
            method(timeout=self._timeout)
        except Exception as error:
            raise _classify_error(error) from error

    @staticmethod
    def _valid_player_name(name):
        return (
            isinstance(name, str)
            and 0 < len(name) <= MAX_BUS_NAME_LENGTH
            and PLAYER_NAME_PATTERN.fullmatch(name) is not None
        )

    def _read_players(self):
        """Read bounded player identities and statuses from the session bus."""
        bus = self._get_bus()
        try:
            names = bus.list_names()
        except Exception as error:
            raise _classify_error(error) from error

        try:
            names = list(names)
        except TypeError as error:
            raise _MediaFailure("dbus_error") from error

        valid_names = sorted(
            {
                str(name)
                for name in names
                if self._valid_player_name(name)
            }
        )

        players = []
        for bus_name in valid_names[:MAX_PLAYERS]:
            proxy = self._object(bus_name)
            identity = self._get_property(proxy, ROOT_INTERFACE, "Identity")
            status = self._get_property(
                proxy, PLAYER_INTERFACE, "PlaybackStatus"
            )
            players.append(
                {
                    "bus_name": _bounded_text(bus_name),
                    "identity": _bounded_text(identity),
                    "playback_status": _bounded_text(status),
                }
            )
        return players
