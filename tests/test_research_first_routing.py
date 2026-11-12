"""Research goals must not collapse into search-page launches or GUI loops."""
import json
import threading
import unittest
from unittest.mock import Mock, patch

from app.brain.direct_actions import resolve_direct_action
from app.brain.research_routing import (prefers_research, research_plan,
                                       grounded_destination_query)
from app.brain.task_grant import looks_like_desktop_work
from app.brain.tools import WEB_RESEARCH_DEFINITION, WEB_SEARCH_DEFINITION
from tests.agent_fakes import TestableMistyAgent, plan_response, plan_step
from tests.test_research_answer_sources import ResearchRegistry

REQUEST = 'Research a good laptop with 32GB RAM under 1000 and find it on eBay.'
SOURCE = {'title': 'Framework Laptop 13 review', 'url': 'https://reviews.example/laptop',
          'text': 'Framework Laptop 13 is upgradeable. Compare configurations and prices.',
          'snippet': 'A repairable laptop.'}
LISTING = {'title': 'Framework Laptop 13', 'url': 'https://www.ebay.com/itm/123456',
           'text': '32GB RAM; used; price depends on condition.', 'snippet': 'Laptop listing.'}


class ResearchRoutingTests(unittest.TestCase):
    def test_explicit_research_bypasses_shortcut_and_desktop_classifier(self):
        for request in (REQUEST, 'Compare good cameras and find one on Amazon',
                        'Research a quiet vacuum and find it on allegro',
                        'Research recent advances in battery chemistry'):
            with self.subTest(request=request):
                self.assertIsNone(resolve_direct_action(request))
                agent = Mock()
                self.assertFalse(looks_like_desktop_work(agent, request))
                agent._chat.assert_not_called()

    def test_plain_search_is_still_navigation(self):
        action = resolve_direct_action('Search ThinkPad T14 32GB under 1000 on eBay')
        self.assertEqual(action[0], 'search_site')
        self.assertIn('32GB', action[1]['query'])
        self.assertIn('1000', action[1]['query'])
        self.assertFalse(prefers_research('Search research and development on Google'))
        self.assertFalse(prefers_research('Search camera reviews on eBay'))
        self.assertIsNotNone(resolve_direct_action('Search Google for research and development'))

    def test_visual_and_other_effects_are_not_reclassified(self):
        for request in ('Research this page and click its buy button',
                        'Compare these listings in my browser',
                        'Research a laptop then email my friend',
                        'Research a laptop and buy it',
                        'Compare my files', 'Research this document'):
            with self.subTest(request=request):
                self.assertFalse(prefers_research(request))
                self.assertIsNone(research_plan(request, [WEB_RESEARCH_DEFINITION]))
        agent = Mock()
        agent._chat.return_value = {'content': '{"route": "desktop"}'}
        self.assertTrue(looks_like_desktop_work(agent, 'Compare these listings in my browser'))

    def test_capability_plan_preserves_all_criteria(self):
        plan = research_plan(REQUEST, [WEB_RESEARCH_DEFINITION])
        self.assertEqual(plan['steps'][0]['arguments']['query'], REQUEST)
        self.assertEqual(plan['steps'][1]['depends_on'], ['research_evidence'])
        self.assertEqual(plan['steps'][1]['arguments']['query'], 'site:ebay.com ' + REQUEST)
        self.assertEqual(research_plan(REQUEST, [WEB_SEARCH_DEFINITION])['steps'][0]['tool'], 'web_search')
        self.assertIsNone(research_plan(REQUEST, []))
        self.assertIsNone(research_plan('Research ' + 'x' * 400, [WEB_RESEARCH_DEFINITION]))

    def test_grounded_query_rejects_invention_and_cannot_drop_constraints(self):
        evidence = json.dumps(SOURCE)
        query = grounded_destination_query(REQUEST, evidence, 'Framework Laptop 13')
        self.assertIn('Framework Laptop 13', query)
        self.assertIn(REQUEST, query)
        for bad in ('Imaginary Model 9000', {'subject': 'x'}, 'site:evil.example', None):
            self.assertEqual(grounded_destination_query(REQUEST, evidence, bad),
                             'site:ebay.com ' + REQUEST)

    def test_end_to_end_research_then_source_grounded_destination(self):
        registry = ResearchRegistry([
            {'success': True, 'results': [SOURCE]},
            {'success': True, 'results': [LISTING]},
        ])
        agent = TestableMistyAgent(registry, [
            {'content': '{"subject": "Framework Laptop 13"}'},
            {'content': 'Compare configurations against your budget. [Review](https://reviews.example/laptop) [Listing](https://www.ebay.com/itm/123456)'},
        ])
        agent._maybe_run_desktop_task = Mock(side_effect=AssertionError('must not use desktop'))
        answer = agent.run(REQUEST)
        self.assertEqual(registry.executed, ['web_research', 'web_research'])
        self.assertEqual(registry.arguments[0]['query'], REQUEST)
        self.assertIn('Framework Laptop 13', registry.arguments[1]['query'])
        self.assertIn(REQUEST, registry.arguments[1]['query'])
        self.assertIn('not independently verified', answer)
        self.assertEqual(len(agent.final_payload()['research_sources']), 2)
        self.assertIn('not independently verified', agent.final_payload()['research_goal']['listing_verification'])

    def test_refinement_cannot_ground_subject_in_url_or_snippet_metadata(self):
        registry = ResearchRegistry([])
        agent = TestableMistyAgent(registry, [
            {'content': '{"subject": "Imaginary Model 9000"}'},
        ])
        metadata_source = {**SOURCE,
                           'url': 'https://example.org/Imaginary Model 9000',
                           'snippet': 'Imaginary Model 9000 is the best',
                           'title': 'Imaginary Model 9000'}
        arguments = {'query': 'site:ebay.com ' + REQUEST}
        result = agent._research_destination_arguments(
            REQUEST, {'success': True, 'results': [metadata_source]}, arguments)
        self.assertEqual(result, arguments)
        no_body = {**metadata_source, 'text': ''}
        self.assertEqual(agent._research_destination_arguments(
            REQUEST, {'success': True, 'results': [no_body]}, arguments), arguments)
        self.assertEqual(len(agent.chat_calls), 1)

    def test_unreadable_research_does_not_claim_destination_lookup_or_matches(self):
        registry = ResearchRegistry([{
            'success': False,
            'message': 'Search returned results, but no page could be read.',
        }])
        agent = TestableMistyAgent(registry, [{'content': 'Unsupported recommendation.'}])
        answer = agent.run(REQUEST)
        self.assertEqual(registry.executed, ['web_research'])
        self.assertIn('could not retrieve readable public sources', answer)
        self.assertIn('destination lookup did not run', answer)
        self.assertNotIn('Public listing matches', answer)
        self.assertNotIn('action(s)', answer)
        self.assertNotIn('Unsupported recommendation', answer)
        self.assertFalse(agent.final_payload()['research_goal']['destination_lookup'])

    def test_denial_never_tries_a_different_backend(self):
        registry = ResearchRegistry([])
        registry.requires_confirmation = lambda *args: True
        agent = TestableMistyAgent(registry, [{'content': 'Research was not authorized.'}])
        decisions = []
        agent.confirmation_callback = lambda *args: decisions.append(args) or False
        agent.run(REQUEST)
        self.assertEqual(registry.executed, [])
        self.assertEqual(len(decisions), 1)

    def test_cancel_during_research_prevents_destination_lookup(self):
        registry = ResearchRegistry([])
        cancel = threading.Event()
        def execute(*args, **kwargs):
            cancel.set()
            return {'success': True, 'results': [SOURCE]}
        registry.execute = Mock(side_effect=execute)
        agent = TestableMistyAgent(registry, [])
        agent.cancel_event = cancel
        agent.run(REQUEST)
        self.assertEqual(registry.execute.call_count, 1)
        self.assertEqual(agent.chat_calls, [])

    def test_followup_keeps_session_budget_in_contextual_planner(self):
        registry = ResearchRegistry([{'success': True, 'results': [SOURCE]}])
        agent = TestableMistyAgent(registry, [
            plan_response([plan_step('r', 'web_research', {'query': 'laptop 32GB under 1000 ebay'})]),
            {'content': 'Evidence with limits.'},
        ])
        agent.run('Research a good laptop and find it on eBay', context={
            'session': {'last_request': 'I need 32GB RAM under 1000'}})
        payload = json.loads(agent.chat_calls[0]['messages'][1]['content'])
        self.assertIn('32GB RAM under 1000', payload['session_context']['last_request'])
        names = {item['function']['name'] for item in payload['available_tools']}
        self.assertNotIn('inspect_screen', names)
        self.assertNotIn('search_site', names)
        self.assertNotIn('knowledge_search', names)


if __name__ == '__main__':
    unittest.main()
