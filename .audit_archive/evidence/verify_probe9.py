"""Probe: task-card Retry replays an egress tool with confirmed=True (no gate)."""
import sys, tempfile, pathlib
sys.dont_write_bytecode = True
from app.brain.agent import MistyAgent
from app.brain import egress
T = pathlib.Path(tempfile.mkdtemp())
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry

class Fetcher:
    def __init__(self): self.urls = []
    def __call__(self, url, **kw):
        from app.web.fetch import FetchResult
        self.urls.append(url)
        return FetchResult(final_url=url, status=200, content_type="text/html",
            title="t", text="page", links=[], truncated=False,
            text_truncated=False, links_truncated=False, bytes_read=5, charset="utf-8")
fe = Fetcher()
reg = ToolRegistry(web_fetcher=fe, memory_store=MemoryStore(T/"m.db"))
agent = MistyAgent(registry=reg)
tool, args = "fetch_web_page", {"url": "https://example.com/medical/anna-diagnosis.pdf"}
print("classifier says:", egress.classify_egress(tool, args).reason_code,
      "| gate would ask:", reg.requires_confirmation(tool, args))
print("normal agent path (confirmed=False, no callback):",
      agent._execute_with_single_retry(tool, args, confirmed=False).get("success"),
      "fetcher calls:", fe.urls)
# what the task-card retry does:
result = agent._execute_with_single_retry(tool, dict(args), confirmed=True)
print("task-card retry path (confirmed=True):", result.get("success"), "fetcher calls:", fe.urls)
