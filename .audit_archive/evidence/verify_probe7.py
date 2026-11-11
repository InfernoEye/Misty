"""Probe: 'connection' marker -> a logical read-only failure is retried."""
import sys, json
sys.dont_write_bytecode = True
from tests.agent_fakes import FakeRegistry, plan_response, plan_step
from app.brain.policy import is_transient_failure, is_retryable

for msg in ["could not find a saved connection profile",
            "no Wifi connection configured",
            "The microphone busy with another app",
            "Temporarily out of office flag in calendar",
            "timed out waiting for the user to answer"]:
    print(f"{msg!r:50s} transient={is_transient_failure({'success': False, 'message': msg})}")
print("list_windows retryable:", is_retryable("list_windows"),
      " browser_read retryable:", is_retryable("browser_read"),
      " fetch_web_page retryable:", is_retryable("fetch_web_page"),
      " knowledge_search retryable:", is_retryable("knowledge_search"))

class Agent(__import__("app.brain.agent", fromlist=["x"]).MistyAgent):
    def __init__(self, registry, responses, **kw):
        super().__init__(registry=registry, **kw); self.responses = list(responses)
    def start(self): pass
    def stop(self): pass
    def _chat(self, messages, tools=None, response_format=None):
        return self.responses.pop(0)

reg = FakeRegistry(results=[{"success": False, "message": "no connection profile found"},
                            {"success": True, "message": "ok"}])
a = Agent(reg, [plan_response([plan_step("1", "list_windows")]), {"content": "done"}])
a.run("list my windows")
print("executed:", reg.executed)
