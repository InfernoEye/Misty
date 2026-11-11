"""Offline tests for the isolated MPRIS media controller.

The fake bus/proxy objects mirror only the dbus-python surface the controller
uses.  No real player is required, and the optional live smoke test only lists
players; it never sends a transport command.
"""

import json
import os
import unittest

from app.desktop import media
from app.desktop.media import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_PLAYERS,
    MAX_TEXT_LENGTH,
    OBJECT_PATH,
    PLAYER_INTERFACE,
    ROOT_INTERFACE,
    MediaController,
)

PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
PLAYER_METHODS = ("Play", "Pause", "PlayPause", "Next", "Previous", "Stop")
BAD_NAME = "org.freedesktop.DBus.Error.UnknownMethod"


class FakeDBusError(Exception):
    """Minimal stand-in for dbus.exceptions.DBusException."""

    def __init__(self, message="simulated D-Bus failure", name=None):
        super().__init__(message)
        self._dbus_error_name = name

    def get_dbus_name(self):
        return self._dbus_error_name


class FakePlayerProxy:
    """Property and method surface of one fake MPRIS player."""

    def __init__(
        self,
        identity="Test Player",
        playback_status="Stopped",
        metadata=None,
        position=0,
        fail_properties=None,
        fail_methods=None,
        missing_properties=(),
        missing_methods=(),
    ):
        self.calls = []
        self.fail_properties = dict(fail_properties or {})
        self.fail_methods = dict(fail_methods or {})
        self.missing_properties = set(missing_properties)
        self.missing_methods = set(missing_methods)
        self.properties = {
            ROOT_INTERFACE: {"Identity": identity},
            PLAYER_INTERFACE: {
                "PlaybackStatus": playback_status,
                "Metadata": {} if metadata is None else metadata,
                "Position": position,
            },
        }

    def get_dbus_method(self, method, interface=None):
        if interface == PROPERTIES_INTERFACE and method == "Get":
            return self._get_property
        if interface == PLAYER_INTERFACE and method in PLAYER_METHODS:
            if method in self.missing_methods:
                raise FakeDBusError(name=BAD_NAME)
            return self._make_method_call(method)
        raise FakeDBusError(name=BAD_NAME)

    def _get_property(self, interface, name, timeout=None):
        self.calls.append(("get", interface, name, timeout))
        failure = self.fail_properties.get((interface, name))
        if failure is not None:
            raise failure
        if (interface, name) in self.missing_properties:
            raise FakeDBusError(
                name="org.freedesktop.DBus.Error.UnknownProperty"
            )
        try:
            return self.properties[interface][name]
        except KeyError:
            raise FakeDBusError(
                name="org.freedesktop.DBus.Error.UnknownProperty"
            ) from None

    def _make_method_call(self, method):
        def call(*args, **kwargs):
            self.calls.append(("call", method, kwargs.get("timeout")))
            failure = self.fail_methods.get(method)
            if failure is not None:
                raise failure
            return None

        return call

    def method_calls(self):
        return [call for call in self.calls if call[0] == "call"]


class FakeBus:
    """Minimal stand-in for dbus.SessionBus()."""

    def __init__(self, proxies, extra_names=(), list_error=None):
        self._proxies = dict(proxies)
        self._names = list(self._proxies) + list(extra_names)
        self.list_error = list_error
        self.list_calls = 0
        self.get_object_calls = []

    def list_names(self):
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return list(self._names)

    def get_object(self, bus_name, object_path):
        self.get_object_calls.append((bus_name, object_path))
        proxy = self._proxies.get(bus_name)
        if proxy is None:
            raise FakeDBusError(
                name="org.freedesktop.DBus.Error.ServiceUnknown"
            )
        return proxy


def one_player_bus(proxy, name="org.mpris.MediaPlayer2.test"):
    return FakeBus({name: proxy})


class ListPlayersTests(unittest.TestCase):
    def test_filters_non_mpris_and_malformed_bus_names(self):
        vlc = FakePlayerProxy(identity="VLC", playback_status="Playing")
        spotify = FakePlayerProxy(identity="Spotify", playback_status="Paused")
        bus = FakeBus(
            {
                "org.mpris.MediaPlayer2.vlc": vlc,
                "org.mpris.MediaPlayer2.spotify": spotify,
            },
            extra_names=[
                "org.freedesktop.DBus",
                "com.example.NotAPlayer",
                "org.mpris.MediaPlayer2",
                "org.mpris.MediaPlayer2Evil",
                "org.mpris.MediaPlayer2.",
                "org.mpris.MediaPlayer2.bad/name",
            ],
        )

        result = MediaController(bus=bus).list_players()

        self.assertTrue(result["success"])
        self.assertIsNone(result["code"])
        self.assertEqual(
            [player["bus_name"] for player in result["players"]],
            [
                "org.mpris.MediaPlayer2.spotify",
                "org.mpris.MediaPlayer2.vlc",
            ],
        )
        self.assertEqual(result["players"][0]["identity"], "Spotify")
        self.assertEqual(result["players"][0]["playback_status"], "Paused")
        self.assertEqual(
            [name for name, _path in bus.get_object_calls],
            [
                "org.mpris.MediaPlayer2.spotify",
                "org.mpris.MediaPlayer2.vlc",
            ],
        )
        for name, path in bus.get_object_calls:
            self.assertEqual(path, OBJECT_PATH)
            self.assertTrue(
                name.startswith("org.mpris.MediaPlayer2.")
            )

    def test_bounds_identity_text_and_returns_json_safe_data(self):
        identity = ("A" * (MAX_TEXT_LENGTH * 2)) + "\n\x00tail"
        proxy = FakePlayerProxy(identity=identity, playback_status="Playing")
        result = MediaController(bus=one_player_bus(proxy)).list_players()

        self.assertTrue(result["success"])
        bounded = result["players"][0]["identity"]
        self.assertLessEqual(len(bounded), MAX_TEXT_LENGTH)
        self.assertNotIn("\n", bounded)
        self.assertNotIn("\x00", bounded)
        self.assertTrue(bounded.endswith("..."))
        json.dumps(result)

    def test_players_list_is_bounded(self):
        proxies = {}
        for index in range(MAX_PLAYERS + 5):
            name = f"org.mpris.MediaPlayer2.player{index:02d}"
            proxies[name] = FakePlayerProxy(
                identity=f"Player {index}", playback_status="Paused"
            )

        result = MediaController(bus=FakeBus(proxies)).list_players()

        self.assertTrue(result["success"])
        self.assertEqual(len(result["players"]), MAX_PLAYERS)
        self.assertEqual(
            len(
                {
                    player["bus_name"]
                    for player in result["players"]
                }
            ),
            MAX_PLAYERS,
        )

    def test_list_names_failure_is_dbus_error(self):
        bus = FakeBus({}, list_error=FakeDBusError())
        result = MediaController(bus=bus).list_players()

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "dbus_error")
        self.assertEqual(result["message"], media._MESSAGES["dbus_error"])

    def test_identity_property_failure_is_dbus_error(self):
        proxy = FakePlayerProxy(
            fail_properties={
                (ROOT_INTERFACE, "Identity"): FakeDBusError(),
            }
        )
        result = MediaController(bus=one_player_bus(proxy)).list_players()

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "dbus_error")


class NowPlayingTests(unittest.TestCase):
    def test_returns_bounded_metadata_for_single_player(self):
        proxy = FakePlayerProxy(
            identity="VLC media player",
            playback_status="Playing",
            metadata={
                "xesam:title": "Example Song",
                "xesam:artist": ["First Artist", "Second Artist"],
                "xesam:album": "Example Album",
                "mpris:length": 245_500_000,
            },
            position=12_345_678,
        )

        result = MediaController(bus=one_player_bus(proxy)).now_playing()

        self.assertTrue(result["success"])
        self.assertIsNone(result["code"])
        self.assertEqual(result["player"], "org.mpris.MediaPlayer2.test")
        self.assertEqual(result["identity"], "VLC media player")
        self.assertEqual(result["title"], "Example Song")
        self.assertEqual(result["artist"], "First Artist, Second Artist")
        self.assertEqual(result["album"], "Example Album")
        self.assertEqual(result["status"], "Playing")
        self.assertEqual(result["position"], 12.346)
        self.assertEqual(result["duration"], 245.5)
        json.dumps(result)

    def test_missing_and_hostile_metadata_is_bounded(self):
        proxy = FakePlayerProxy(
            metadata={
                "xesam:title": ("T" * (MAX_TEXT_LENGTH * 2)) + "\x00\n",
                "xesam:artist": ["A" * (MAX_TEXT_LENGTH * 2), "", 7],
                "mpris:length": -5,
            },
            position=-1,
        )

        result = MediaController(bus=one_player_bus(proxy)).now_playing()

        self.assertTrue(result["success"])
        self.assertLessEqual(len(result["title"]), MAX_TEXT_LENGTH)
        self.assertLessEqual(len(result["artist"]), MAX_TEXT_LENGTH)
        self.assertNotIn("\x00", result["title"])
        self.assertNotIn("\n", result["title"])
        self.assertEqual(result["album"], "")
        self.assertEqual(result["position"], 0.0)
        self.assertEqual(result["duration"], 0.0)

    def test_missing_duration_returns_none(self):
        proxy = FakePlayerProxy(metadata={"xesam:title": "No length"})
        result = MediaController(bus=one_player_bus(proxy)).now_playing()

        self.assertTrue(result["success"])
        self.assertIsNone(result["duration"])

    def test_selects_the_only_playing_player_among_several(self):
        paused = FakePlayerProxy(
            identity="Paused",
            playback_status="Paused",
            metadata={"xesam:title": "Paused track"},
        )
        playing = FakePlayerProxy(
            identity="Playing",
            playback_status="Playing",
            metadata={"xesam:title": "Playing track"},
        )
        bus = FakeBus(
            {
                "org.mpris.MediaPlayer2.paused": paused,
                "org.mpris.MediaPlayer2.playing": playing,
            }
        )

        result = MediaController(bus=bus).now_playing()

        self.assertTrue(result["success"])
        self.assertEqual(result["player"], "org.mpris.MediaPlayer2.playing")
        self.assertEqual(result["title"], "Playing track")

    def test_single_player_is_selected_even_when_paused(self):
        proxy = FakePlayerProxy(
            playback_status="Paused", metadata={"xesam:title": "Paused"}
        )
        result = MediaController(bus=one_player_bus(proxy)).now_playing()

        self.assertTrue(result["success"])
        self.assertEqual(result["title"], "Paused")

    def test_explicit_player_overrides_playing_player(self):
        paused = FakePlayerProxy(
            playback_status="Paused",
            metadata={"xesam:title": "Requested"},
        )
        playing = FakePlayerProxy(
            playback_status="Playing",
            metadata={"xesam:title": "Other"},
        )
        bus = FakeBus(
            {
                "org.mpris.MediaPlayer2.paused": paused,
                "org.mpris.MediaPlayer2.playing": playing,
            }
        )

        result = MediaController(bus=bus).now_playing(
            player="org.mpris.MediaPlayer2.paused"
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["title"], "Requested")

    def test_multiple_non_playing_players_are_ambiguous(self):
        first = FakePlayerProxy(
            playback_status="Paused", metadata={"xesam:title": "One"}
        )
        second = FakePlayerProxy(
            playback_status="Stopped", metadata={"xesam:title": "Two"}
        )
        bus = FakeBus(
            {
                "org.mpris.MediaPlayer2.one": first,
                "org.mpris.MediaPlayer2.two": second,
            }
        )

        result = MediaController(bus=bus).now_playing()

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "ambiguous_player")
        self.assertEqual(
            [item["bus_name"] for item in result["candidates"]],
            [
                "org.mpris.MediaPlayer2.one",
                "org.mpris.MediaPlayer2.two",
            ],
        )
        for candidate in result["candidates"]:
            self.assertEqual(
                set(candidate),
                {"bus_name", "identity", "playback_status"},
            )
        self.assertEqual(first.method_calls(), [])
        self.assertEqual(second.method_calls(), [])
        self.assertNotIn(
            "Metadata", [call[2] for call in first.calls if call[0] == "get"]
        )

    def test_multiple_playing_players_are_ambiguous(self):
        bus = FakeBus(
            {
                "org.mpris.MediaPlayer2.one": FakePlayerProxy(
                    playback_status="Playing"
                ),
                "org.mpris.MediaPlayer2.two": FakePlayerProxy(
                    playback_status="Playing"
                ),
            }
        )

        result = MediaController(bus=bus).now_playing()

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "ambiguous_player")
        self.assertEqual(len(result["candidates"]), 2)

    def test_no_players_is_no_player(self):
        result = MediaController(bus=FakeBus({})).now_playing()

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_player")

    def test_absent_valid_player_is_no_player(self):
        proxy = FakePlayerProxy()
        bus = one_player_bus(proxy)
        result = MediaController(bus=bus).now_playing(
            player="org.mpris.MediaPlayer2.other"
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_player")

    def test_arbitrary_bus_name_is_rejected_before_dbus(self):
        bus = one_player_bus(FakePlayerProxy())
        for player in (
            "com.example.Evil",
            "org.mpris.MediaPlayer2.evil/../../x",
            "org.mpris.MediaPlayer2Evil",
            "org.mpris.MediaPlayer2.",
            "org.mpris.MediaPlayer2..player",
            "org.mpris.MediaPlayer2.player\x00",
            "",
            42,
        ):
            with self.subTest(player=player):
                result = MediaController(bus=bus).now_playing(player=player)
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "invalid_player")
        self.assertEqual(bus.list_calls, 0)
        self.assertEqual(bus.get_object_calls, [])

    def test_metadata_property_failure_is_dbus_error(self):
        proxy = FakePlayerProxy(
            fail_properties={
                (PLAYER_INTERFACE, "Metadata"): FakeDBusError(),
            }
        )
        result = MediaController(bus=one_player_bus(proxy)).now_playing()

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "dbus_error")

    def test_missing_position_property_is_optional(self):
        proxy = FakePlayerProxy(
            metadata={"xesam:title": "No position"},
            missing_properties={(PLAYER_INTERFACE, "Position")},
        )
        result = MediaController(bus=one_player_bus(proxy)).now_playing()

        self.assertTrue(result["success"])
        self.assertIsNone(result["position"])

    def test_malformed_metadata_is_unsupported(self):
        proxy = FakePlayerProxy(metadata=["not", "a", "mapping"])
        result = MediaController(bus=one_player_bus(proxy)).now_playing()

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "unsupported")


class FriendlyPlayerNameTests(unittest.TestCase):
    def two_players(self):
        return FakeBus({
            "org.mpris.MediaPlayer2.stremio": FakePlayerProxy(
                identity="Stremio", playback_status="Paused",
            ),
            "org.mpris.MediaPlayer2.chromium.instance9": FakePlayerProxy(
                identity="Helium", playback_status="Playing",
            ),
        })

    def test_a_display_name_resolves_to_the_running_player(self):
        result = MediaController(bus=self.two_players()).now_playing(player="Helium")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["player"], "org.mpris.MediaPlayer2.chromium.instance9")

    def test_a_display_name_can_control_the_resolved_player(self):
        bus = self.two_players()
        result = MediaController(bus=bus).command("pause", player="Stremio")
        self.assertTrue(result["success"], result)
        self.assertEqual(
            bus.get_object_calls[-1][0], "org.mpris.MediaPlayer2.stremio"
        )

    def test_an_unknown_display_name_is_no_player(self):
        result = MediaController(bus=self.two_players()).now_playing(player="Spotify")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_player")

    def test_a_display_name_is_not_a_bus_name_escape(self):
        result = MediaController(bus=self.two_players()).now_playing(player="../etc")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_player")


class CommandTests(unittest.TestCase):
    def test_each_allowlisted_action_maps_to_one_mpris_method(self):
        expected = {
            "play": "Play",
            "pause": "Pause",
            "play_pause": "PlayPause",
            "next": "Next",
            "previous": "Previous",
            "stop": "Stop",
        }
        for action, method_name in expected.items():
            with self.subTest(action=action):
                proxy = FakePlayerProxy(playback_status="Playing")
                bus = one_player_bus(proxy)
                result = MediaController(bus=bus).command(action)

                self.assertTrue(result["success"])
                self.assertIsNone(result["code"])
                self.assertEqual(result["action"], action)
                self.assertEqual(
                    result["player"], "org.mpris.MediaPlayer2.test"
                )
                self.assertEqual(
                    proxy.method_calls(),
                    [("call", method_name, DEFAULT_TIMEOUT_SECONDS)],
                )
                self.assertEqual(len(proxy.method_calls()), 1)

    def test_command_selects_unique_playing_player(self):
        paused = FakePlayerProxy(
            playback_status="Paused",
            metadata={"xesam:title": "Paused"},
        )
        playing = FakePlayerProxy(
            playback_status="Playing",
            metadata={"xesam:title": "Playing"},
        )
        bus = FakeBus(
            {
                "org.mpris.MediaPlayer2.paused": paused,
                "org.mpris.MediaPlayer2.playing": playing,
            }
        )

        result = MediaController(bus=bus).command("pause")

        self.assertTrue(result["success"])
        self.assertEqual(result["player"], "org.mpris.MediaPlayer2.playing")
        self.assertEqual(paused.method_calls(), [])
        self.assertEqual(
            playing.method_calls(),
            [("call", "Pause", DEFAULT_TIMEOUT_SECONDS)],
        )

    def test_ambiguous_command_sends_nothing(self):
        first = FakePlayerProxy(playback_status="Paused")
        second = FakePlayerProxy(playback_status="Stopped")
        bus = FakeBus(
            {
                "org.mpris.MediaPlayer2.one": first,
                "org.mpris.MediaPlayer2.two": second,
            }
        )

        result = MediaController(bus=bus).command("play")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "ambiguous_player")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(first.method_calls(), [])
        self.assertEqual(second.method_calls(), [])

    def test_invalid_action_is_rejected_before_dbus(self):
        proxy = FakePlayerProxy()
        bus = one_player_bus(proxy)
        controller = MediaController(bus=bus)

        for action in ("seek", "volume", "Play", "PLAY", "", None, 3, ["play"]):
            with self.subTest(action=action):
                result = controller.command(action)
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "invalid_action")
                self.assertEqual(result["message"], media._MESSAGES["invalid_action"])
        self.assertEqual(bus.list_calls, 0)
        self.assertEqual(bus.get_object_calls, [])
        self.assertEqual(proxy.calls, [])

    def test_invalid_player_is_rejected_before_dbus(self):
        proxy = FakePlayerProxy()
        bus = one_player_bus(proxy)
        controller = MediaController(bus=bus)

        for player in ("com.example.Evil", "org.mpris.MediaPlayer2.evil/../x", 7):
            with self.subTest(player=player):
                result = controller.command("play", player=player)
                self.assertFalse(result["success"])
                self.assertEqual(result["code"], "invalid_player")
        self.assertEqual(bus.list_calls, 0)
        self.assertEqual(proxy.calls, [])

    def test_absent_player_is_no_player(self):
        bus = one_player_bus(FakePlayerProxy())
        result = MediaController(bus=bus).command(
            "play", player="org.mpris.MediaPlayer2.absent"
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_player")

    def test_no_players_is_no_player(self):
        result = MediaController(bus=FakeBus({})).command("play")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_player")

    def test_method_failure_is_dbus_error(self):
        proxy = FakePlayerProxy(
            playback_status="Playing",
            fail_methods={"Play": FakeDBusError()},
        )
        result = MediaController(bus=one_player_bus(proxy)).command("play")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "dbus_error")

    def test_missing_player_method_is_unsupported(self):
        proxy = FakePlayerProxy(
            playback_status="Playing", missing_methods={"Play"}
        )
        result = MediaController(bus=one_player_bus(proxy)).command("play")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "unsupported")


class LiveSmokeTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("MISTY_MEDIA_LIVE_TEST") == "1",
        "set MISTY_MEDIA_LIVE_TEST=1 for the opt-in live list-only smoke",
    )
    def test_live_session_bus_lists_players_only(self):
        try:
            import dbus

            bus = dbus.SessionBus()
            names = [str(name) for name in bus.list_names()]
        except Exception as error:
            self.skipTest(f"session D-Bus unavailable: {error}")

        if not any(
            MediaController._valid_player_name(name) for name in names
        ):
            self.skipTest("no MPRIS player is currently on the session bus")

        result = MediaController().list_players()

        self.assertIn("success", result)
        self.assertIsInstance(result["success"], bool)


if __name__ == "__main__":
    unittest.main()
