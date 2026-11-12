"""Cancellation gates for the legacy desktop observation/decision loop."""
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from app.brain.agent_loop import run_loop, LoopReport
from app.brain.desktop_task import run_desktop_task


class DesktopLoopCancellationTests(unittest.TestCase):
    def test_cancellation_at_each_phase_prevents_next_phase(self):
        for phase in ('before', 'observe', 'decide', 'act'):
            with self.subTest(phase=phase):
                cancel = threading.Event()
                calls = []
                def observe():
                    calls.append('observe')
                    if phase == 'observe':
                        cancel.set()
                    return 'screen'
                def decide(*args):
                    calls.append('decide')
                    if phase == 'decide':
                        cancel.set()
                    return {'kind': 'act', 'tool': 'click', 'arguments': {}}
                def act(*args):
                    calls.append('act')
                    cancel.set()
                    return {'success': True}
                if phase == 'before':
                    cancel.set()
                report = run_loop(observe, decide, act, cancel=cancel)
                self.assertEqual(report.status, 'stopped')
                expected = {'before': [], 'observe': ['observe'],
                            'decide': ['observe', 'decide'],
                            'act': ['observe', 'decide', 'act']}
                self.assertEqual(calls, expected[phase])

    def test_cancelled_decision_cannot_claim_completion(self):
        cancel = threading.Event()
        def decide(*args):
            cancel.set()
            return {'kind': 'done', 'note': 'done'}
        report = run_loop(lambda: 'screen', decide, Mock(), cancel=cancel)
        self.assertEqual(report.status, 'stopped')

    def test_desktop_task_passes_request_event_to_loop(self):
        cancel = threading.Event()
        agent = SimpleNamespace(cancel_event=cancel)
        with patch('app.brain.desktop_task.make_decider'), \
             patch('app.brain.desktop_task.make_actor'), \
             patch('app.brain.desktop_task.agent_loop.run_loop',
                   return_value=LoopReport('stopped', [])) as loop:
            run_desktop_task(agent, 'click target')
        self.assertIs(loop.call_args.kwargs['cancel'], cancel)
