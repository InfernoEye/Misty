import threading
import unittest
import urllib.request

from PIL import Image

from app.brain.agent import BASE_URL, MODEL_PATH
from app.brain.server_lock import SHARED_SERVER_LOCK
from app.desktop.vision.vlm import VisionLanguageService


def _server_up():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


class SharedServerLockTests(unittest.TestCase):
    def test_lock_is_a_single_shared_instance(self):
        from app.brain import agent
        from app.desktop.vision import vlm

        self.assertIs(agent.SHARED_SERVER_LOCK, SHARED_SERVER_LOCK)
        self.assertIs(vlm.SHARED_SERVER_LOCK, SHARED_SERVER_LOCK)

    @unittest.skipUnless(_server_up(), "shared local model server is not running on BASE_URL")
    def test_concurrent_vision_calls_all_complete_on_the_single_slot(self):
        service = VisionLanguageService(
            base_url=BASE_URL, model_name=MODEL_PATH.name, timeout=180
        )
        results, errors = [], []

        def ask():
            try:
                results.append(service.analyze(
                    Image.new("RGB", (64, 64), (0, 0, 255)),
                    "Colour? One word.", max_tokens=8,
                ).text)
            except Exception as error:  # noqa: BLE001 - surfaced by the assertion
                errors.append(error)

        threads = [threading.Thread(target=ask) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 3)
        self.assertTrue(all("blue" in text.lower() for text in results))


if __name__ == "__main__":
    unittest.main()
