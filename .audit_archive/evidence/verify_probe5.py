"""Mutation probe: does the suite catch removal of the execute() egress gate?"""
import sys, unittest, io
sys.dont_write_bytecode = True
from app.brain import tools as tools_mod
old = """        egress_refusal = None
        if egress_policy.is_egress_tool(name):
            decision = self.egress_decision(name, arguments)
            if decision is not None and decision.requires_confirmation:
                egress_refusal = self._egress_refusal(decision)
"""
new = "        egress_refusal = None\n"
src = open(tools_mod.__file__, encoding="utf-8").read()
class Screen:
    def __init__(self): self.rewrites = []
    def rewrite(self, text):
        new_src = text.replace(old, new)
        if new_src != text:
            self.rewrites.append(1)
        return new_src
screen = Screen()
def rule(path):
    if path.endswith("app/brain/tools.py"):
        return screen
    return None
names = ["tests.test_tool_contracts", "tests.test_egress_policy", "tests.test_audit_regressions", "tests.test_retry_policy"]
suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromName(n) for n in names])
sys.meta_path.insert(0, type("F", (), {"find_spec": staticmethod(lambda n, p=None, t=None: None)})())
# simpler: purge cached modules and let loader import via a rewriting import hook
for mod in [k for k in sys.modules if k.startswith("app.")]:
    del sys.modules[mod]
import importlib.abc, importlib.util
class Loader(importlib.abc.Loader):
    def __init__(self, spec, r): self.spec = spec; self.rule = r
    def create_module(self, spec): return None
    def exec_module(self, module):
        src = open(self.spec.origin, encoding="utf-8").read()
        exec(compile(self.rule(self.spec.origin).rewrite(src) if self.rule(self.spec.origin) else src, self.spec.origin, "exec"), module.__dict__)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in sys.modules: return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin and spec.origin.endswith(".py") and self is sys.meta_path[0]:
            return importlib.util.spec_from_loader(name, Loader(spec, rule), origin=spec.origin)
        return None
sys.meta_path.pop(0)
sys.meta_path.insert(0, Finder())
res = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
print("tools.py rewrites:", len(screen.rewrites))
print("mutant run:", res.testsRun, "failures", len(res.failures), "errors", len(res.errors))
for t, tb in (res.failures+res.errors)[:10]:
    print(" FAILED:", t, tb.strip().splitlines()[-1][:120])
