"""Probe: direct-action navigation path bypasses the egress approval gate."""
import sys, json, tempfile, pathlib
sys.dont_write_bytecode = True
from app.brain.agent import MistyAgent
from app.brain.direct_actions import resolve_direct_action

T = pathlib.Path(tempfile.mkdtemp())
from app.brain.memory import MemoryStore
from app.brain.tools import ToolRegistry
from app.brain import egress

class Exec:
    def __init__(self): self.calls = []
    def open_url(self, url):
        self.calls.append(url); return {"success": True, "message": "opened", "url": url}
class Search:
    def __init__(self): self.calls = []
    def search(self, q, **kw):
        self.calls.append(q); return {"success": True, "results": [], "count": 0}

ex, se = Exec(), Search()
reg = ToolRegistry(executor=ex, web_search_client=se,
                   memory_store=MemoryStore(T / "m.db"))
asked = []
agent = MistyAgent(registry=reg,
                   confirmation_callback=lambda n, a, d: asked.append((n, a)) or True)
agent.start = lambda: None
agent.stop = lambda: None
agent._chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model must not run"))

SECRET = "Diagnosis for Anna Kowalska, patient 4417, ICD-10 F32.1"
requests = [
    f"Open https://example.com/?q={SECRET.replace(' ', '+')}",
    f"Search allegro for {SECRET}",
]
for text in requests:
    asked.clear(); ex.calls.clear(); se.calls.clear()
    action = resolve_direct_action(text)
    print("request:", text[:60], "->", action[0] if action else None)
    if action is None:
        continue
    print("  classifier:", egress.classify_egress(action[0], action[1]).reason_code)
    answer = agent.run(text)
    print("  approvals asked:", asked)
    print("  network effects:", ex.calls or se.calls)
    print("  answer:", answer[:70].replace("\n", " "))
