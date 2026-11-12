"""Read-only research routing and bounded, criterion-preserving decomposition.

These recognizers select tools, never grant permissions. Marketplace names are
search scopes, not evidence that a visible browser must be operated.
"""
import re
from urllib.parse import urlsplit

_RESEARCH = re.compile(r"^(?:(?:please|misty[, :]*|(?:can|could|would)\s+you|i\s+(?:want|need)\s+you\s+to)\s+)*(?:(?:research|compare|evaluate|recommend)\b|(?:give|show)\s+me\s+(?:some\s+)?recommendations?\b|find\s+(?:me\s+)?(?:the\s+|a\s+)?best\b)", re.I)
_VISUAL = re.compile(r"\b(?:click|tap|scroll|drag|type|press|screenshot|on[- ]screen|screen|visible|currently\s+open|this\s+(?:page|tab|window)|my\s+browser|log\s+in|sign\s+in|checkout|add\s+to\s+cart)\b", re.I)
_LOCAL = re.compile(r"\b(?:my|local|saved|attached|selected|these|this)\s+(?:files?|folders?|documents?|photos?|images?|notes?|messages?|windows?|clipboard)\b|\b(?:knowledge\s+base|clipboard)\b", re.I)
_OTHER_ACTION = re.compile(r"\b(?:buy|purchase|order|delete|remove|send|email|write|save|download|install|move|copy|open|launch|play|remind|remember|subscribe)\b", re.I)


def prefers_research(request):
    """Explicit informational work without a second effect or visual target."""
    return (isinstance(request, str) and 0 < len(request.strip()) <= 8000
            and bool(_RESEARCH.search(request))
            and not _VISUAL.search(request) and not _OTHER_ACTION.search(request)
            and not _LOCAL.search(request))


def marketplace_scope(request):
    """An explicitly named search destination; no guessed country/currency."""
    from app.brain.direct_actions import SITE_ALIASES, SITE_HOME_URLS
    for alias in sorted(SITE_ALIASES, key=len, reverse=True):
        if re.search(r"\b(?:on|at|from|in)\s+" + re.escape(alias) + r"\b", request, re.I):
            home = SITE_HOME_URLS.get(SITE_ALIASES[alias], "")
            host = urlsplit(home).hostname
            if host:
                return host.removeprefix("www.")
    match = re.search(r"\b(?:on|at|from|in)\s+(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", request, re.I)
    return match[1].lower() if match else None


def research_plan(request, definitions):
    """Decompose explicit research without letting an LLM drop criteria.

    Long or context-dependent requests stay with the ordinary contextual
    planner. A short query is kept verbatim, including all budget/spec words.
    Both phases go through the normal validator, policy, executor and audit.
    """
    if not prefers_research(request):
        return None
    query = request.strip()
    scope = marketplace_scope(query)
    if len(query) + (len(scope) + 6 if scope else 0) > 260:
        return None
    available = {item.get("function", {}).get("name") for item in definitions}
    tool = "web_research" if "web_research" in available else (
        "web_search" if "web_search" in available else None)
    if tool is None:
        return None
    steps = [{"id": "research_evidence", "tool": tool,
              "arguments": {"query": query}, "depends_on": [],
              "reason": "Research the complete user criteria before selecting an option."}]
    if scope:
        steps.append({"id": "research_destination", "tool": tool,
                      "arguments": {"query": f"site:{scope} {query}"},
                      "depends_on": ["research_evidence"],
                      "reason": "Find public destination evidence; a search page is not a verified listing."})
    return {"steps": steps, "response": ""}


def grounded_destination_query(request, evidence, proposed_subject):
    """Constrain model output to a source-present subject plus original criteria.

    A source mention does not prove quality or availability. This only builds a
    discovery query; retrieved text cannot select tools, permissions or sites.
    """
    scope = marketplace_scope(request)
    fallback = f"site:{scope} {request.strip()}" if scope else request.strip()
    if not isinstance(proposed_subject, str):
        return fallback
    subject = " ".join(proposed_subject.split())
    if (not subject or len(subject) > 100 or re.search(r"[\n\r{}<>]|https?://|site:", subject, re.I)
            or subject.casefold() not in str(evidence).casefold()):
        return fallback
    query = f"site:{scope} {subject} {request.strip()}" if scope else f"{subject} {request.strip()}"
    return query if len(query) <= 300 else fallback
