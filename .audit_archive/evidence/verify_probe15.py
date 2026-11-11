import sys
sys.dont_write_bytecode = True
from app.brain.agent import MistyAgent, _sanitize_session_context
controller_context = {"pending": {"transcript": "Find my October invoice",
                                  "question": "Which folder should I search?"}}
print("agent._sanitize_session_context(pending shape):", _sanitize_session_context(controller_context))
# what agent.run does with context={} or {"pending": ...}
class A(MistyAgent):
    def __init__(self):
        class R:
            def available_tools(self): return []
            @staticmethod
            def requires_confirmation(n, a=None): return False
        super().__init__(registry=R())
    def start(self): pass
    def stop(self): pass
    def _chat(self, messages, tools=None, response_format=None):
        import json
        self.payload = json.loads(messages[-1]["content"])
        return {"content": json.dumps({"steps": [], "response": "ok", "intent_confidence": 0.0})}
a = A()
a.run("Downloads", context={"pending": controller_context["pending"]})
print("planning payload keys:", sorted(a.payload))
