"""Read-only probe: registry.execute for egress tools WITHOUT approval."""
import sys
sys.dont_write_bytecode = True
import tempfile, os
from pathlib import Path
from app.brain.memory import MemoryStore
TMP = tempfile.mkdtemp()
from app.brain.tools import ToolRegistry
from app.web.fetch import FetchResult
from app.brain import egress

class Exec:
    def __init__(self): self.calls=[]
    def open_url(self, url):
        self.calls.append(url)
        return {"success": True, "message": "opened", "url": url}

class Search:
    def __init__(self): self.queries=[]
    def search(self, q, **kw):
        self.queries.append(q)
        return {"success": True, "results": [
            {"url": "https://example.com/a", "title": "A", "snippet": "s"}]}

class Fetcher:
    def __init__(self): self.urls=[]
    def __call__(self, url, **kw):
        self.urls.append(url)
        return FetchResult(final_url=url, status=200, content_type="text/html",
            title="t", text="x"*500, links=[], truncated=False,
            text_truncated=False, links_truncated=False, bytes_read=500, charset="utf-8")

ex, se, fe = Exec(), Search(), Fetcher()
reg = ToolRegistry(executor=ex, web_search_client=se, web_fetcher=fe,
                   memory_store=MemoryStore(Path(TMP)/'m.sqlite3'),
                   file_ops_engine=None or __import__('app.actions.file_ops',fromlist=['x']).FileOpsEngine(quarantine_root=Path(TMP)/'q'))

SECRET = "Diagnosis for Anna Kowalska, patient 4417, ICD-10 F32.1"
PRIVATE_PATH = "please summarise /home/jane/Medical/anna-diagnosis.pdf"
caseload = [
  ("open_url", {"url": "https://example.com/?q=" + SECRET.replace(" ", "+")}),
  ("search_site", {"site": "google", "query": SECRET}),
  ("web_search", {"query": SECRET}),
  ("web_research", {"query": SECRET, "pages": 1}),
  ("fetch_web_page", {"url": "https://example.com/" + PRIVATE_PATH.replace(" ", "%20")}),
  ("browser_navigate", {"url": "https://example.com/?m=" + SECRET.replace(" ", "+")}),
]
for name, args in caseload:
    needs = reg.requires_confirmation(name, args)
    res = reg.execute(name, args, confirmed=False)
    print(f"{name:16s} needs_confirmation={needs!s:5s} executed_without_approval={res.get('success')} msg={res.get('message')!r}")
print("executor.open_url calls:", ex.calls)
print("search queries:", se.queries)
print("fetcher urls:", fe.urls)
print("with confirmed=True open_url:", reg.execute("open_url", caseload[0][1], confirmed=True).get("success"))
