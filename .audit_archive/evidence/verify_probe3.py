"""Mutation probe: FakeRegistry.requires_confirmation loses the argument path."""
import sys, unittest
sys.dont_write_bytecode = True
from tests.agent_fakes import FakeRegistry
from app.brain import agent as agent_mod

found = {"arg_aware": 0, "calls": 0}
orig = FakeRegistry.requires_confirmation
def recording(self, name, arguments=None):
    found["calls"] += 1
    if arguments is not None:
        found["arg_aware"] += 1
    return False
FakeRegistry.requires_confirmation = recording

import tests.test_audit_regressions as mod
suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
res = unittest.TextTestRunner(verbosity=0, stream=open('/dev/null','w')).run(suite)
print("tests run:", res.testsRun, "failures:", len(res.failures), "errors:", len(res.errors))
print("requires_confirmation calls:", found)
FakeRegistry.requires_confirmation = orig
