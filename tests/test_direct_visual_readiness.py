"""Direct summaries must own VLM readiness even without a planner chat."""
import threading
import unittest
from unittest.mock import Mock

from tests.agent_fakes import FakeRegistry, TestableMistyAgent


class DirectVisualReadinessTests(unittest.TestCase):
    def test_page_summary_starts_model_before_capture_without_chat(self):
        registry = FakeRegistry()
        agent = TestableMistyAgent(registry, [])
        def inspect(name, arguments, confirmed=False):
            self.assertTrue(agent.started)
            self.assertEqual(name, 'inspect_screen')
            self.assertEqual(arguments['mode'], 'active_window')
            return {'success': True, 'visual_interpretation': 'Two video results are visible.'}
        registry.execute = Mock(side_effect=inspect)
        answer = agent.run("summarize what I'm seeing on this page.")
        self.assertEqual(answer, 'Two video results are visible.')
        registry.execute.assert_called_once()
        self.assertEqual(agent.chat_calls, [])

    def test_model_start_failure_does_not_capture_or_execute(self):
        registry = FakeRegistry()
        registry.execute = Mock()
        agent = TestableMistyAgent(registry, [])
        agent.start = Mock(side_effect=RuntimeError('startup failure'))
        answer = agent.run('summarize this page')
        self.assertIn('local visual model could not be prepared', answer)
        registry.execute.assert_not_called()

    def test_cancel_during_start_prevents_capture(self):
        registry = FakeRegistry()
        registry.execute = Mock()
        agent = TestableMistyAgent(registry, [])
        agent.cancel_event = threading.Event()
        agent.start = Mock(side_effect=agent.cancel_event.set)
        answer = agent.run('summarize this page')
        self.assertIn('cancelled', answer)
        registry.execute.assert_not_called()

    def test_ocr_only_inspection_does_not_start_visual_model(self):
        registry = FakeRegistry([{'success': True, 'ocr_text': 'Visible text'}])
        agent = TestableMistyAgent(registry, [])
        answer = agent._execute_direct_action(('inspect_screen', {
            'mode': 'active_window', 'include_visual': False}))
        self.assertEqual(answer, 'Visible text')
        self.assertFalse(agent.started)

    def test_navigation_does_not_start_visual_model(self):
        registry = FakeRegistry([{'success': True, 'url': 'https://example.com'}])
        agent = TestableMistyAgent(registry, [])
        agent.run('open https://example.com')
        self.assertFalse(agent.started)
