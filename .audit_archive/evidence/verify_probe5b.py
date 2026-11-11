"""Mutation probe v2: does the suite catch removal of the execute() egress gate?

Loads the real modules first, then replaces app.brain.tools with a module
compiled from source minus the gate, then runs the egress/contract tests.
"""
import sys, unittest, io, types
sys.dont_write_bytecode = True
import app.brain.tools as real_tools          # real package modules loaded
import app.brain.egress, app.brain.policy, app.web.fetch
from app.actions import web_launch
import app.brain.agent

old = """        egress_refusal = None
        if egress_policy.is_egress_tool(name):
            decision = self.egress_decision(name, arguments)
            if decision is not None and decision.requires_confirmation:
                egress_refusal = self._egress_refusal(decision)
"""
src = open(real_tools.__file__, encoding="utf-8").read()
assert old in src
mut = types.ModuleType("app.brain.tools")
mut.__file__ = real_tools.__file__
mut.__package__ = "app.brain"
exec(compile(src.replace(old, "        egress_refusal = None\n"), real_tools.__file__, "exec"), mut.__dict__)
sys.modules["app.brain.tools"] = mut
app.brain.agent.ToolRegistry = mut.ToolRegistry

names = ["tests.test_tool_contracts", "tests.test_egress_policy",
         "tests.test_audit_regressions", "tests.test_retry_policy"]
suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromName(n) for n in names])
res = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
print("mutant run:", res.testsRun, "failures", len(res.failures), "errors", len(res.errors))
for t, tb in (res.failures + res.errors)[:10]:
    print(" FAILED:", t.id() if hasattr(t, 'id') else t, tb.strip().splitlines()[-1][:140])
# direct behavioural check of the mutant
import tempfile, pathlib
_T = tempfile.mkdtemp()
reg = mut.ToolRegistry(executor=type("E", (), {"open_url": lambda s, u: {"success": True, "message": u}})(),
                       memory_store=__import__("app.brain.memory", fromlist=["x"]).MemoryStore(pathlib.Path(_T)/"m.db"))
print("mutant fetch of private URL without confirmation:",
      reg.execute("fetch_web_page", {"url": "https://example.com/jane-diagnosis.pdf"}, confirmed=False).get("success"))
