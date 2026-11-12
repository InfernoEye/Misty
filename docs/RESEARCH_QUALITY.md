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

## Research-and-find routing (2026-11-12)

The user reported that a laptop research request opened a generic marketplace
search, then entered desktop control and stopped on unsupported `inspect_screen`
arguments. The current routing handles explicit, nonvisual research before those
shortcuts. For supported standalone requests, it keeps the original criteria in
one public research query and adds a dependent lookup scoped to the named site.
The local model can prepend a candidate name only when that name occurs in a
readable source body. This literal check does not prove the candidate meets the
criteria. Session follow-ups retain the contextual planner rather than discard
an earlier budget or specification.

The public-research tool filter excludes screen capture, browser launch, and local
file search as substitutes. Explicit GUI interaction and side effects keep their
normal routing and approvals. Simple requests to search a site still use the
navigation shortcut. Research wording inside a search payload does not change
that intent.

The inspection schema and runtime allowlist already agreed; the supplied log did
not identify the rejected argument. The missing recovery contract was concrete:
unknown fields failed before capture without `invalid_arguments` and
`not_executed`. The tool now returns those fields and fixed schema guidance. The
desktop loop can correct a clearly unexecuted call within its repair budget;
it does not drop unknown fields, broaden capture, or replay uncertain input.

If initial research fails, the dependent destination lookup does not run. The
answer reports that fact without implying that Misty found listing matches or
repeating the full action-count report. Activity and audit events retain the
execution details.

## Retrieval failure review (2026-11-12)

A later user run reached `web_research` but returned “Search returned results,
but no page could be read.” The log did not contain individual fetch errors, so
it does not establish which sites or HTTP responses caused that run to fail.

The code review found a separate retrieval defect: once a headline matched the
query, the tool excluded lower-overlap results. A blocked or unreadable top
result could therefore hide a readable source with a paraphrased headline. The
tool now keeps the stable relevance-ranked candidate list and tries distinct
URLs within the existing `pages + 5` fetch-attempt bound. It keeps the same
public-address, redirect, content, timeout, and byte limits; it does not retry a
refused URL or switch to browser input.

Results now include `fetch_attempts` and a `fetch_failures` histogram alongside
`fetch_errors`. The histogram uses fixed categories such as `http_denied`,
`rate_limited`, `timeout`, `blocked_address`, and `insufficient_text`; it does not
include raw exception text or failing URLs. Failure codes distinguish
`no_search_results` from `no_readable_sources`. Readable source text remains a
requirement for research success. Search snippets do not replace it. The excerpt
fallback no longer restores cookie/subscribe lines that the boilerplate filter
rejected; it can still combine short specification rows into readable text.

## Honest limits

- The answering model is the shared Ministral 3 8B planner server. It receives bounded excerpts, not entire pages; long articles are truncated.
- Search quality is limited by the local SearXNG instance and by sites refusing automated fetches (403). Weak results remain possible for vague queries; the answer should then say what it could not confirm.
- Nothing here has been validated end-to-end against the live SearXNG service and live model beyond the read-only probe described below.

## Validation

Read-only probes against the local SearXNG instance (no input to any desktop application, no model inference): queries covering a vulnerability question, an election question, a "how do I" question and a vague location question returned usable pages after the fixes above, where before most returned zero or one page. Automated coverage is in `tests/test_research_answer_sources.py`, `tests/test_research_relevance.py` and `tests/test_web_research_tool.py`.
