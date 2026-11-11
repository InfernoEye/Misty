"""Probe: SessionContext / clarification context never reaches the planner."""
import sys, json
sys.dont_write_bytecode = True
from app.engine.voice_controller import VoiceController
from app.brain.agent import MistyAgent

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
        self.payload = json.loads(messages[-1]["content"])
        return {"content": json.dumps({"steps": [], "response": "ok", "intent_confidence": 0.0})}

c = VoiceController.__new__(VoiceController)
c.session_context = __import__("app.brain.session_context", fromlist=["x"]).SessionContext()
c.session_context.record_turn("find my October invoice", "Found invoice-10.pdf")
c._pending_clarification = {"transcript": "Find my October invoice",
                            "question": "Which folder should I search?",
                            "created": __import__("time").monotonic()}
a = A()
pending, original, question = c._clarification_take("Downloads")
c._session_invoke_agent(a, "Downloads", pending, c._session_context_payload(),
                        original, question)
print("shape passed to agent.run:", pending, "| session keys:",
      sorted(c._session_context_payload() or {}))
print("planning payload keys:", sorted(a.payload))
