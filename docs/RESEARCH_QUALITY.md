# Research and answer quality

Observed failure (live, 2026-11-11): asking for the newest information on a topic produced a reply listing tool names and "the task was not fully completed", although `web_research` had already fetched readable pages. A follow-up local document search found nothing, which the report then presented as the outcome.

## What was actually wrong

1. **The answer model never saw the pages.** `web_research` returned page text in its tool result, but the final summarisation request contained only tool status and messages. The model could only restate `web_research: Public pages fetched.`
2. **A redundant retry counted as failure.** The planner sometimes issued a second research call; when that retry failed, the original successful retrieval was reported as a failed task. The read-only recovery then searched local documents, which cannot contain current public information.
3. **The report led with the recovery, not the answer.** With a failed retry, the summary text favoured the local search and omitted that sources existed.
4. **Retrieval itself was weak for some phrasings:**
   - News searches forced `language=en`, which dropped most multilingual news results; one page survived, or none.
   - Relevance tokens never case-folded, so a capitalised topic word (`Linux`) could not match a lowercased headline.
   - Topic tokens shorter than three characters were dropped, losing short place names (`US`, `UK`, `EU`).
   - Question framing ("how do I…") was passed to the search engine verbatim and could return zero results.
   - Stub pages were accepted as sources, and feed pages supplied their generic `<title>` instead of the specific headline.

## Current behaviour

- `web_research` prefers topical results, then reads only pages that yield a readable excerpt; results carry the search headline and their own URL.
- News queries search without a language filter; when a filtered search returns nothing, the same query is retried broadly.
- Pages have navigation/ad boilerplate lines removed before they are bounded.
- The final request includes bounded `research_sources` (title, URL, up to 4,000 characters each, at most five pages) so the answer model summarises and cites the sources.
- When readable sources exist, the deterministic summary says so and notes a follow-up read-only step that ran afterwards was not needed. Local-document search finding nothing is not treated as completing or replacing web research.
- A recovery that returns no results is never reported as completing the request.

Nothing in these paths is keyed to a specific topic, site, language or query. The behaviour is generic: word overlap ranks results, and the local model reasons about the actual text.

## Honest limits

- The answering model is the shared Ministral 3 8B planner server. It receives bounded excerpts, not entire pages; long articles are truncated.
- Search quality is limited by the local SearXNG instance and by sites refusing automated fetches (403). Weak results remain possible for vague queries; the answer should then say what it could not confirm.
- Nothing here has been validated end-to-end against the live SearXNG service and live model beyond the read-only probe described below.

## Validation

Read-only probes against the local SearXNG instance (no input to any desktop application, no model inference): queries covering a vulnerability question, an election question, a "how do I" question and a vague location question returned usable pages after the fixes above, where before most returned zero or one page. Automated coverage is in `tests/test_research_answer_sources.py`, `tests/test_research_relevance.py` and `tests/test_web_research_tool.py`.
