import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry


class FakeMedia:
    def __init__(self):
        self.calls = []

    def list_players(self):
        self.calls.append(("list_players",))
        return {
            "success": True,
            "players": [
                {"bus_name": "org.mpris.MediaPlayer2.vlc", "identity": "VLC"}
            ],
        }

    def now_playing(self, player=None):
        self.calls.append(("now_playing", player))
        return {
            "success": True,
            "player": player or "org.mpris.MediaPlayer2.vlc",
            "title": "Track",
            "artist": "Artist",
        }

    def command(self, action, player=None):
        self.calls.append(("command", action, player))
        return {"success": True, "action": action, "player": player}


class MediaToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryStore(Path(directory.name) / "memory.sqlite3")
        self.media = FakeMedia()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=self.memory,
            media_controller=self.media,
        )

    def test_tools_require_controller(self):
        names = {
            item["function"]["name"]
            for item in self.registry.available_tools()
        }
        self.assertTrue({"media_list_players", "media_now_playing", "media_control"} <= names)
        without = ToolRegistry(executor=Mock(), memory_store=self.memory)
        names = {
            item["function"]["name"]
            for item in without.available_tools()
        }
        self.assertNotIn("media_control", names)

    def test_list_and_now_playing(self):
        self.registry.execute("media_list_players", {})
        self.registry.execute(
            "media_now_playing",
            {"player": "org.mpris.MediaPlayer2.vlc"},
        )
        self.assertEqual(self.media.calls[0], ("list_players",))
        self.assertEqual(
            self.media.calls[1],
            ("now_playing", "org.mpris.MediaPlayer2.vlc"),
        )

    def test_control_forwards_allowlisted_action(self):
        result = self.registry.execute(
            "media_control", {"action": "play_pause"}
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            self.media.calls, [("command", "play_pause", None)]
        )

    def test_invalid_arguments_do_not_reach_controller(self):
        for name, arguments in (
            ("media_list_players", {"x": 1}),
            ("media_now_playing", {"command": "run"}),
            ("media_control", {}),
            ("media_control", {"action": "play", "extra": 1}),
        ):
            with self.subTest(name=name):
                result = self.registry.execute(name, arguments)
                self.assertFalse(result["success"])
        self.assertEqual(self.media.calls, [])


if __name__ == "__main__":
    unittest.main()
