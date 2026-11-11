"""Single structured domains bypass visual routing; no model/input/capture runs."""
import unittest
from unittest.mock import Mock, patch

from app.brain.task_grant import (
    ROUTE_QUESTION, looks_like_desktop_work, prefer_structured_tools,
)


STRUCTURED = (
    # Window geometry/control/identity are exact tools, not screen inspection.
    "Move Discord to the main screen.",
    "Move Discord to main screen",
    "Please move Discord to the primary monitor",
    "Could you move Firefox to the left screen?",
    "Move the Discord window to screen 2",
    "Send Firefox to DP-3",
    "Move this window to the right monitor",
    "Focus Discord",
    "Activate Firefox",
    "Minimize the Discord window",
    "Maximize Konsole",
    "Restore Firefox",
    "Close the current window",
    "Raise Discord",
    "List windows",
    "Show all open windows",
    "List connected screens",
    "List monitors",
    "Show available displays",
    "List windows and screens",
    "What windows are open?",
    "Which monitors are connected?",
    "Which window is active?",
    "What is the current window?",
    "Show the active window title",
    # Facts/media have no visual dependency.
    "What time is it?",
    "What is the date?",
    "Show system status",
    "How much disk space is left?",
    "How much RAM is available?",
    "Show CPU usage",
    "What is the battery level?",
    "Play some music",
    "Pause playback",
    "Resume media",
    "Stop music",
    "Next track",
    "Previous song",
    "What is playing?",
    "List media players",
    # Names containing screen/GUI words are not vision requests.
    "Find my invoice files",
    "Search files for screenshot",
    "Read screenshot.png",
    "Open the folder named screenshots",
    "List files in ~/Desktop",
    "Copy screenshot.png to /tmp/screenshots/",
    "Move the file screenshot.png to /tmp/output.png",
    "Rename file window.txt to monitor.txt",
    "Delete file /tmp/screenshot.txt",
    "Write text file /tmp/monitor.txt",
    "Create a folder called screenshots",
    "Grep files for monitor",
    "Transcribe file /tmp/screen-recording.wav",
    "Read file 'click then type.txt'",
    "Copy file 'and click.png' to /tmp/copy.png",
    "Read /tmp/click-here.txt",
    "Read my clipboard",
    "Show the clipboard",
    "What is in my clipboard?",
    "Copy 'click the screen' to the clipboard",
    "Set my clipboard to 'type this in Discord'",
    "Clear the clipboard",
    "Remember that my monitor is 144Hz",
    "Remember that clicking the screen opens a menu",
    "Recall my screen preferences",
    "List saved memories",
    "Forget memory 12",
    "What do you remember about my monitor?",
    "Set a reminder to clean my screen tomorrow",
    "Remind me to click the channel at 5pm",
    "List my reminders",
    "Cancel reminder 12",
    "What reminders do I have?",
    "List calendar events tomorrow",
    "Add a calendar event called 'screen meeting' tomorrow",
    "Add event 'type in Discord' to my calendar tomorrow",
    "Show my calendar today",
    "What is on my calendar tomorrow?",
)

VISUAL_OR_AMBIGUOUS = (
    "Move Discord to main screen then click the channel",
    "Move Discord to primary monitor and click Dragon Cave",
    "Move Discord to main screen; type hello",
    "Focus Discord and open its settings menu",
    "Minimize Firefox then select Dragon Cave in Discord",
    "List windows and click the Discord button",
    "Read a file then click the channel",
    "Remember this and click the channel",
    "Show my calendar and select the meeting",
    "Click Dragon Cave in Discord",
    "Click on the right screen",
    "In Discord open channel Dragon Cave",
    "Navigate to the settings page in Firefox",
    "Type hello in Discord",
    "Scroll the active window",
    "Drag the icon onto the right screen",
    "Move the slider to the main screen",
    "Move the selected file to the right screen",
    "Move this to the main screen",
    "Move Discord to main screen and Firefox",
    "Focus on the red button",
    "Close the settings dialog",
    "Close the browser tab",
    "Focus the search field",
    "Focus the input box",
    "Close the notification popup",
    "Move the taskbar to the right screen",
    "List buttons in the active window",
    "What is on the right screen?",
    "What is visible in this window?",
    "Describe the screen",
    "Inspect the Discord window",
    "Take a screenshot of Discord",
    "Capture a screenshot",
    "Read text on the screen",
    "Read 'Dragon Cave' on the screen",
    "Copy the button label from the screen to the clipboard",
    "Remember what is on the screen",
    "Paste clipboard into Discord",
    "Find invoice files in my browser",
    "Search files by using the on-screen search",
    "Find me a good vacuum on Amazon and show it",
    "Open 'Dragon Cave'",
    "Open the channel named screenshot.png",
)


class StructuredRoutingTests(unittest.TestCase):
    def test_invalid_requests_do_not_match(self):
        for request in (None, "", " ", 42, "x" * 8001):
            with self.subTest(request_type=type(request).__name__):
                self.assertFalse(prefer_structured_tools(request))

    def test_guard_is_before_classifier_and_broad_fallback(self):
        with patch("app.brain.task_grant.route_from_model", side_effect=AssertionError("classifier forbidden")), \
             patch("app.brain.task_grant.mentions_screen", side_effect=AssertionError("fallback forbidden")):
            self.assertFalse(looks_like_desktop_work(None, "Move Discord to the main screen."))

    def test_failed_classifier_still_rescues_compound_visual_request(self):
        with patch("app.brain.task_grant.route_from_model", return_value=None) as classifier:
            self.assertTrue(looks_like_desktop_work(None,
                "Move Discord to the main screen then click Dragon Cave"))
            self.assertEqual(classifier.call_count, 2)

    def test_sequences_of_existing_tools_do_not_gain_visual_route(self):
        for request in (
            "Move Discord to the left screen and move Firefox to the right screen",
            "Move this window to the left screen and maximize it",
            "Focus Discord then maximize it",
            "List windows and tell me the time",
        ):
            with self.subTest(request=request), patch("app.brain.task_grant.route_from_model", side_effect=AssertionError("classifier forbidden")):
                self.assertFalse(looks_like_desktop_work(None, request))

    def test_prompt_explicitly_distinguishes_window_tools_from_content(self):
        self.assertIn("screen/window mention alone does NOT require vision", ROUTE_QUESTION)
        self.assertIn("move_window_to_screen", ROUTE_QUESTION)
        self.assertIn("move Discord then click its channel", ROUTE_QUESTION)


def _structured_case(request):
    def test(self):
        self.assertTrue(prefer_structured_tools(request), request)
        # Even a classifier that would insist on desktop is never consulted.
        agent = Mock()
        agent._chat.return_value = {"content": '{"route":"desktop"}'}
        self.assertFalse(looks_like_desktop_work(agent, request), request)
        agent._chat.assert_not_called()
    return test


def _visual_case(request):
    def test(self):
        self.assertFalse(prefer_structured_tools(request), request)
        with patch("app.brain.task_grant.route_from_model", return_value="desktop") as classify:
            self.assertTrue(looks_like_desktop_work(None, request), request)
            classify.assert_called_once_with(None, request)
    return test


for _index, _request in enumerate(STRUCTURED):
    setattr(StructuredRoutingTests, f"test_structured_{_index:03d}", _structured_case(_request))
for _index, _request in enumerate(VISUAL_OR_AMBIGUOUS):
    setattr(StructuredRoutingTests, f"test_visual_or_ambiguous_{_index:03d}", _visual_case(_request))


if __name__ == "__main__":
    unittest.main()
