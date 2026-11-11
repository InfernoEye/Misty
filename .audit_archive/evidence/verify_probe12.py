import sys, tempfile, pathlib
sys.dont_write_bytecode = True
from app.brain.agent import MistyAgent
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry
from app.web.fetch import FetchResult
T = pathlib.Path(tempfile.mkdtemp())
class Fetcher:
    def __init__(self): self.urls = []
    def __call__(self, url, **kw):
        self.urls.append(url)
        return FetchResult(final_url=url, status=200, content_type="text/html", title="t",
            text="page", links=[], truncated=False, text_truncated=False,
            links_truncated=False, bytes_read=5, charset="utf-8")
fe = Fetcher()
reg = ToolRegistry(web_fetcher=fe, memory_store=MemoryStore(T/"m.db"))
agent = MistyAgent(registry=reg)
tool, args = "fetch_web_page", {"url": "https://example.com/?token=SECRET123"}
print("gate asks:", reg.requires_confirmation(tool, args))
print("confirmed=False ->", agent._execute_with_single_retry(tool, args, confirmed=False).get("success"), fe.urls)
print("confirmed=True  ->", agent._execute_with_single_retry(tool, args, confirmed=True).get("success"), fe.urls)
