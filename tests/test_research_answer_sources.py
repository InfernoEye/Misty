"""Research answers must come from retrieved sources, not tool-status prose."""
import json
import unittest

from tests.agent_fakes import TestableMistyAgent, plan_response, plan_step
from tests.test_web_research_tool import FakeFetcher, FakeSearch
from app.brain.tools import ToolRegistry
from unittest.mock import Mock


class ResearchRegistry(ToolRegistry):
    """Real tool schemas, research enabled, but execution is scripted."""

    def __init__(self, results):
        super().__init__(executor=Mock(), web_search_client=FakeSearch(),
                         web_fetcher=FakeFetcher(), knowledge_index=Mock())
        self.scripted = list(results)
        self.executed = []
        self.arguments = []

    def execute(self, name, arguments, confirmed=False):
        self.executed.append(name)
        self.arguments.append(dict(arguments))
        return self.scripted.pop(0) if self.scripted else {"success": True, "message": "ok"}

SOURCES = [
    {"title": "Kernel TCP flaw", "url": "https://example.org/kernel-flaw",
     "text": "CVE-2026-1234 A use-after-free in the kernel TCP stack was patched.", "snippet": "s"},
    {"title": "Distro advisories", "url": "https://example.net/advisories",
     "text": "Patched kernels were released for the stable distributions.", "snippet": "s"},
]


class ResearchAnswerTests(unittest.TestCase):
    def test_research_sources_reach_the_final_answer_model(self):
        registry = ResearchRegistry(results=[{"success": True, "results": SOURCES, "count": 2},
                                         {"success": True, "message": "ok"}])
        agent = TestableMistyAgent(registry, [
            plan_response([plan_step("step-1", "web_research", {"query": "newest linux vulnerability"})]),
            {"content": "The newest vulnerability is ... [example.org]."},
        ])
        answer = agent.run("What is the newest vulnerability on Linux and summarize it?")
        payload = agent.final_payload()
        self.assertEqual([item["url"] for item in payload["research_sources"]],
                         ["https://example.org/kernel-flaw", "https://example.net/advisories"])
        self.assertIn("use-after-free", payload["research_sources"][0]["text"])
        self.assertIn("newest vulnerability", answer)
        self.assertEqual(payload["outcome"], "completed")

    def test_sources_are_bounded_and_urls_validated(self):
        agent = TestableMistyAgent(ResearchRegistry([]), [{"content": "x"}])
        entries = [{"tool": "web_research", "status": "success", "result": {"success": True, "results": [
            {"title": "t", "url": "https://a.example/1", "text": "x" * 9000},
            {"title": "t", "url": "javascript:alert(1)", "text": "x"},
            {"title": "t", "url": "https://b.example/2", "text": "x"},
            {"title": "t", "url": "https://c.example/3", "text": "x"},
            {"title": "t", "url": "https://d.example/4", "text": "x"},
            {"title": "t", "url": "https://e.example/5", "text": "x"},
            {"title": "t", "url": "https://f.example/6", "text": "x"},
        ]}}]
        sources = agent._research_sources(entries)
        self.assertEqual(len(sources), 5)
        self.assertNotIn("javascript:alert(1)", [s["url"] for s in sources])
        self.assertLessEqual(len(sources[0]["text"]), 4000)

    def test_research_prompt_treats_sources_as_evidence_not_automatic_completion(self):
        from app.brain.agent import ACCURACY_GUIDANCE, FINAL_RESPONSE_INSTRUCTIONS
        self.assertIn("evidence, not proof", ACCURACY_GUIDANCE)
        self.assertIn("dependent goal", ACCURACY_GUIDANCE)
        self.assertIn("cannot find it", ACCURACY_GUIDANCE)
        self.assertIn("cite each source URL", FINAL_RESPONSE_INSTRUCTIONS)

    def test_retrieved_sources_are_announced_as_the_answer_material(self):
        # First call succeeds with sources; the plan retried once more and
        # that retry failed, so the request must still be answered from the
        # pages already retrieved.
        registry = ResearchRegistry(results=[
            {"success": True, "results": SOURCES, "count": 2, "fetch_errors": 3},
            {"success": False, "message": "no readable pages this time"},
            {"success": True, "count": 0, "results": []},
        ])
        agent = TestableMistyAgent(registry, [
            plan_response([plan_step("step-1", "web_research", {"query": "linux vulnerabilities"}),
                           plan_step("step-2", "web_research", {"query": "linux kernel vulnerabilities"})]),
            plan_response([plan_step("step-1", "knowledge_search", {"query": "linux vulnerabilities"})]),
            {"content": "Draft answer from the report."},
            {"content": "Here is the summary."},
        ])
        answer = agent.run("What is the newest vulnerability on Linux and summarize it?")
        self.assertIn("retrieved readable sources", answer)
        self.assertIn("https://example.org/kernel-flaw",
                      json.dumps(agent.final_payload()["research_sources"]))
        self.assertIn("follow-up read-only step ran afterwards but was not needed", answer)
        self.assertIn("Here is the summary.", answer)

    def test_local_search_with_zero_results_cannot_complete_a_web_request(self):
        registry = ResearchRegistry(results=[{"success": False, "message": "no page could be read"},
                                         {"success": True, "count": 0, "results": []},
                                         {"success": True, "count": 0, "results": []}])
        agent = TestableMistyAgent(registry, [
            plan_response([plan_step("step-1", "web_research", {"query": "linux vulnerability"})]),
            plan_response([plan_step("step-1", "knowledge_search", {"query": "linux vulnerability"}),
                           plan_step("step-2", "knowledge_search", {"query": "linux"})]),
            {"content": "Recovered."},
        ])
        agent.run("What is the newest vulnerability on Linux and summarize it?")
        payload = agent.final_payload()
        self.assertEqual(payload["outcome"], "failed")
        self.assertIn("did not supply what the request needed", payload["execution_summary"])


if __name__ == "__main__":
    unittest.main()
