"""Mutation: remove the egress gate from ToolRegistry.execute and rerun tests."""
import sys, unittest, io
sys.dont_write_bytecode = True
from app.brain import tools as tools_mod
src = open(tools_mod.__file__, encoding="utf-8").read()
assert "egress_refusal = None" in src
old = """        egress_refusal = None
        if egress_policy.is_egress_tool(name):
            decision = self.egress_decision(name, arguments)
            if decision is not None and decision.requires_confirmation:
                egress_refusal = self._egress_refusal(decision)
"""
assert old in src
new_src = src.replace(old, "        egress_refusal = None\n")
import types
mod2 = types.ModuleType("app.brain.tools_mut")
mod2.__file__ = tools_mod.__file__
exec(compile(new_src, tools_mod.__file__, "exec"), mod2.__dict__)
print("mutant executes egress without gate:",
      mod2.ToolRegistry.requires_confirmation("fetch_web_page", {"url": "https://example.com/"}))
# Does any test in the named modules catch the mutant? They import the real module,
# so instead run a behavioural check the tests would need.
names = ["tests.test_tool_contracts", "tests.test_audit_regressions"]
loader = unittest.defaultTestLoader
suite = unittest.TestSuite([loader.loadTestsFromName(n) for n in names])
res = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
print("baseline", res.testsRun, "fails", len(res.failures), len(res.errors))
