"""Screen observations must carry coordinates.

Measured failure this fixes: the loop was given a screen as a list of words
and asked to click one. With no positions it invented (1000, 800) for a
channel whose box is at (95, 191), and spent ~10,800 characters reasoning
about it. With the centre of each line's box it picked (80, 189) — the real
target — in about 3,600.
"""

import unittest

from app.brain.desktop_task import (
    MAX_OBSERVATION_LINES,
    describe_screen,
    render_observation,
)
from app.desktop.vision.capture import CaptureResult
from app.desktop.vision.coordinator import DesktopVisionResult
from app.desktop.vision.ocr import OCRResult, OCRWord


def word(text, left, top, width=None, height=18):
    return OCRWord(
        text=text, confidence=95.0, left=left, top=top,
        width=len(text) * 8 if width is None else width, height=height,
    )


class FakeVision:
    def __init__(self, words=(), text=""):
        self.words = tuple(words)
        self.text = text

    def inspect(self, **kwargs):
        capture = CaptureResult(
            image=None, mode=kwargs.get("mode", "desktop"),
            captured_at="2026-11-10T21:00:00", width=1200, height=1920,
        )
        ocr = OCRResult(
            text=self.text, words=self.words, language="en",
            image_size=(1200, 1920),
        )
        return DesktopVisionResult(capture=capture, ocr=ocr, visual=None)


class LinePositionTests(unittest.TestCase):
    def test_a_line_carries_the_centre_of_its_box(self):
        vision = FakeVision([word("Dragoncave", left=40, top=180)])
        observation = describe_screen(vision)

        text, x, y = observation["ocr_lines"][0]
        self.assertEqual(text, "Dragoncave")
        # width is 80 for 10 chars, so the centre is 40 + 40.
        self.assertEqual((x, y), (80, 189))

    def test_words_on_one_row_become_one_line(self):
        vision = FakeVision([
            word("Cordless", left=300, top=420),
            word("Vacuum", left=380, top=421),
        ])
        observation = describe_screen(vision)

        self.assertEqual(len(observation["ocr_lines"]), 1)
        text, _x, _y = observation["ocr_lines"][0]
        self.assertEqual(text, "Cordless Vacuum")

    def test_distant_same_row_controls_do_not_share_a_click_centre(self):
        vision = FakeVision([
            word("Discord", left=40, top=180),
            word("Dragoncave", left=1600, top=180),
        ])
        lines = describe_screen(vision)["ocr_lines"]
        self.assertEqual([line[0] for line in lines], ["Discord", "Dragoncave"])
        self.assertEqual(lines[1][1], 1640)

    def test_separate_rows_stay_separate(self):
        vision = FakeVision([
            word("General", left=40, top=285),
            word("memes", left=40, top=355),
        ])
        lines = describe_screen(vision)["ocr_lines"]
        self.assertEqual([line[0] for line in lines], ["General", "memes"])

    def test_a_tall_gap_splits_rows(self):
        # Two words 200px apart vertically are different lines even though
        # they share a column.
        vision = FakeVision([
            word("top", left=40, top=100),
            word("bottom", left=40, top=300),
        ])
        self.assertEqual(len(describe_screen(vision)["ocr_lines"]), 2)

    def test_boxes_without_size_are_ignored(self):
        vision = FakeVision([word("ghost", left=10, top=10, width=0)])
        observation = describe_screen(vision)
        self.assertEqual(observation["ocr_lines"], [])

    def test_plain_text_still_works_without_boxes(self):
        # A backend that returns text only must not crash the loop.
        vision = FakeVision([], text="Dragoncave\nGeneral\nmemes")
        observation = describe_screen(vision)

        self.assertEqual(len(observation["ocr_lines"]), 3)
        text, x, y = observation["ocr_lines"][0]
        self.assertEqual(text, "Dragoncave")
        self.assertIsNone(x)
        self.assertIsNone(y)

    def test_lines_are_bounded(self):
        words = [word(f"row{index}", left=10, top=index * 40)
                 for index in range(MAX_OBSERVATION_LINES + 20)]
        observation = describe_screen(FakeVision(words))

        self.assertEqual(len(observation["ocr_lines"]), MAX_OBSERVATION_LINES)
        self.assertTrue(observation["ocr_truncated"])


class RenderTests(unittest.TestCase):
    def test_positions_appear_in_the_prompt_text(self):
        observation = describe_screen(
            FakeVision([word("Dragoncave", left=40, top=180)])
        )
        rendered = render_observation(observation)

        self.assertIn("x=80 y=189", rendered)
        self.assertIn("Dragoncave", rendered)
        self.assertIn("click x, y", rendered.lower())

    def test_text_without_positions_is_rendered_plainly(self):
        observation = describe_screen(FakeVision([], text="Dragoncave"))
        rendered = render_observation(observation)

        self.assertIn("Dragoncave", rendered)
        self.assertNotIn("x=None", rendered)

    def test_an_unavailable_screen_says_why(self):
        rendered = render_observation({"available": False, "note": "no capture"})
        self.assertIn("no capture", rendered)


class RealCoordinateRegressionTests(unittest.TestCase):
    """The exact shapes measured against the live model."""

    def test_the_discord_channel_is_addressable(self):
        vision = FakeVision([
            word("Discord", left=40, top=120),
            word("Dragoncave", left=40, top=180),
            word("General", left=40, top=285),
        ])
        observation = describe_screen(vision)
        centres = {
            line[0].split()[0]: (line[1], line[2])
            for line in observation["ocr_lines"]
        }
        self.assertEqual(centres["Dragoncave"], (80, 189))

    def test_an_amazon_result_is_addressable(self):
        vision = FakeVision([
            word("Cordless", left=300, top=420),
            word("Vacuum", left=380, top=421),
            word("Cleaner", left=440, top=420),
        ])
        observation = describe_screen(vision)
        text, x, y = observation["ocr_lines"][0]
        self.assertEqual(text, "Cordless Vacuum Cleaner")
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)
        self.assertGreater(x, 300)


if __name__ == "__main__":
    unittest.main()
