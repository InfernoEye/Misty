"""Read-only research recovery must stay bounded and preserve fetch refusals."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.brain.memory import MemoryStore
from app.brain.tools import MAX_EXTRA_RESEARCH_CANDIDATES, ToolRegistry
from app.web.fetch import (
    BlockedAddressError,
    FetchTimeoutError,
    HTTPStatusError,
    InvalidURLError,
    NetworkError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
    WebFetchError,
)


class WebResearchRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.search = Mock()
        self.fetcher = Mock()
        self.registry = ToolRegistry(
            executor=Mock(),
            memory_store=MemoryStore(Path(directory.name) / 'memory.sqlite3'),
            web_search_client=self.search,
            web_fetcher=self.fetcher,
        )

    @staticmethod
    def candidate(index, title='A portable workstation review'):
        return {'url': f'https://sources.example/{index}', 'title': title,
                'snippet': 'Public search snippet, not fetched source evidence.'}

    @staticmethod
    def page(index, text=None):
        return SimpleNamespace(
            final_url=f'https://sources.example/{index}', title='Public review',
            text=text if text is not None else 'A readable public source discusses configurations and trade-offs. ' * 8,
            truncated=False,
        )

    def results(self, *items):
        self.search.search.return_value = {'success': True, 'results': list(items)}

    def test_unreadable_topical_hit_does_not_discard_paraphrased_source(self):
        topical = self.candidate(1, 'Laptop comparison')
        paraphrase = self.candidate(2)
        # Search rank is opposite the relevance rank: topical is attempted first.
        self.results(paraphrase, topical)
        self.fetcher.side_effect = [HTTPStatusError(403, topical['url']), self.page(2)]

        result = self.registry.execute('web_research', {'query': 'research laptop', 'pages': 1})

        self.assertTrue(result['success'], result)
        self.assertEqual(result['results'][0]['url'], paraphrase['url'])
        self.assertEqual([call.args[0] for call in self.fetcher.call_args_list],
                         [topical['url'], paraphrase['url']])
        self.assertEqual(result['fetch_failures'], {'http_denied': 1})
        self.assertEqual(result['fetch_attempts'], 2)
        self.assertEqual(result['fetch_errors'], 1)
        for call in self.fetcher.call_args_list:
            self.assertEqual(call.kwargs, {'timeout': 8.0, 'max_bytes': 500_000,
                                          'max_chars': 4_000, 'max_links': 5,
                                          'max_redirects': 3})

    def test_short_topical_page_does_not_discard_paraphrased_source(self):
        self.results(self.candidate(1, 'Laptop details'), self.candidate(2))
        self.fetcher.side_effect = [self.page(1, 'Short notice.'), self.page(2)]
        result = self.registry.execute('web_research', {'query': 'laptop', 'pages': 1})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['fetch_failures'], {'insufficient_text': 1})
        self.assertEqual(result['results'][0]['url'], 'https://sources.example/2')

    def test_failure_diagnostics_are_fixed_categories_not_exception_contents(self):
        secret_url = 'https://user:private-password@internal.example/path?token=private-token'
        failures = [
            (BlockedAddressError(secret_url), 'blocked_address'),
            (InvalidURLError(secret_url), 'invalid_url'),
            (FetchTimeoutError(secret_url), 'timeout'),
            (HTTPStatusError(403, secret_url), 'http_denied'),
            (HTTPStatusError(429, secret_url), 'rate_limited'),
            (HTTPStatusError(503, secret_url), 'http_error'),
            (UnsupportedContentTypeError('private-type', secret_url), 'unsupported_content'),
            (ResponseTooLargeError(50, secret_url), 'response_too_large'),
            (TooManyRedirectsError(secret_url), 'redirect_limit'),
            (NetworkError(secret_url), 'network_error'),
            (WebFetchError(secret_url), 'fetch_error'),
        ]
        for error, category in failures:
            with self.subTest(category=category):
                self.results(self.candidate(1, 'Laptop review'))
                self.fetcher.reset_mock()
                self.fetcher.side_effect = error
                result = self.registry.execute('web_research', {'query': 'laptop'})
                self.assertFalse(result['success'])
                self.assertEqual(result['code'], 'no_readable_sources')
                self.assertEqual(result['fetch_failures'], {category: 1})
                self.assertIn(f'{category}: 1', result['message'])
                self.assertNotIn('private-', repr(result))
                self.assertNotIn('internal.example', repr(result))
                self.fetcher.assert_called_once()

    def test_alternative_sources_are_bounded_and_duplicate_refusals_not_retried(self):
        items = [self.candidate(1, 'Laptop review')] * 3
        items.extend(self.candidate(i, 'Laptop review') for i in range(2, 30))
        self.results(*items)
        self.fetcher.side_effect = BlockedAddressError('refused')
        result = self.registry.execute('web_research', {'query': 'laptop', 'pages': 1})
        self.assertFalse(result['success'])
        bound = 1 + MAX_EXTRA_RESEARCH_CANDIDATES
        self.assertEqual(self.fetcher.call_count, bound)
        self.assertEqual(result['fetch_attempts'], bound)
        self.assertEqual(result['fetch_errors'], bound)
        urls = [call.args[0] for call in self.fetcher.call_args_list]
        self.assertEqual(len(urls), len(set(urls)))
        self.search.search.assert_called_once()  # No backend/query fallback on refusal.

    def test_empty_search_is_not_reported_as_a_page_fetch_failure(self):
        self.results()
        result = self.registry.execute('web_research', {'query': 'laptop'})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'no_search_results')
        self.assertEqual(result['message'], 'Search returned no results.')
        self.assertEqual(result['fetch_attempts'], 0)
        self.assertEqual(result['fetch_failures'], {})
        self.assertEqual(self.search.search.call_count, 2)  # Existing empty-result broadening only.
        self.fetcher.assert_not_called()

    def test_search_refusal_is_preserved_without_retry_or_fetch(self):
        refusal = {'success': False, 'code': 'http_error', 'message': 'Search service refused.'}
        self.search.search.return_value = refusal
        result = self.registry.execute('web_research', {'query': 'laptop'})
        self.assertEqual(result, refusal)
        self.search.search.assert_called_once()
        self.fetcher.assert_not_called()

    def test_long_consent_notice_is_not_reintroduced_as_readable_evidence(self):
        self.results(self.candidate(1, 'Laptop details'))
        notice = 'Subscribe to our newsletter and accept cookies to continue reading. ' * 10
        self.fetcher.return_value = self.page(1, notice)
        result = self.registry.execute('web_research', {'query': 'laptop'})
        self.assertFalse(result['success'])
        self.assertEqual(result['results'], [])
        self.assertEqual(result['fetch_failures'], {'insufficient_text': 1})

    def test_short_specification_rows_still_form_readable_source_text(self):
        self.results(self.candidate(1, 'Laptop details'))
        rows = ['Memory: 32GB DDR5', 'Storage: 1TB NVMe SSD',
                'Display: 14-inch matte panel', 'Condition: used, minor wear',
                'Battery: 80 percent health', 'Processor: eight-core CPU',
                'Warranty: one year remaining', 'Weight: 1.5 kilograms',
                'Seller location: Germany', 'Shipping: tracked delivery']
        self.fetcher.return_value = self.page(1, '\n'.join(rows))
        result = self.registry.execute('web_research', {'query': 'laptop'})
        self.assertTrue(result['success'], result)
        self.assertIn('32GB DDR5', result['results'][0]['text'])

    def test_long_search_snippet_is_not_substituted_for_unreadable_source(self):
        candidate = self.candidate(1, 'Laptop details')
        candidate['snippet'] = 'A plausible recommendation from a search snippet. ' * 30
        self.results(candidate)
        self.fetcher.return_value = self.page(1, '')
        result = self.registry.execute('web_research', {'query': 'laptop'})
        self.assertFalse(result['success'])
        self.assertEqual(result['results'], [])
        self.assertEqual(result['fetch_failures'], {'insufficient_text': 1})


if __name__ == '__main__':
    unittest.main()
