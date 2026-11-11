import sys, os, tempfile, shutil
from pathlib import Path
sys.dont_write_bytecode = True
from app.actions import fs_safe
tmp = Path(tempfile.mkdtemp(prefix="noreplace-"))
destdir = tmp/"dest"; destdir.mkdir()
dest = destdir/"report.txt"; dest.write_text("ORIGINAL")
# Unique temp names, like tempfile.mkstemp(): attacker cannot pre-create it.
for i in range(50):
    temp = tmp/f".misty-tmp-{os.urandom(6).hex()}"
    temp.write_text(f"ATTACKER-PAYLOAD-{i}")
    # race: a second process creates the destination between validation and publish
    outcome = fs_safe.publish_noreplace(str(temp), str(dest))
    if outcome != fs_safe.OVERWRITE_RACE:
        print("OUTCOME", outcome, "-> dest now:", dest.read_text()[:40])
        break
    temp.unlink()
else:
    print("50/50 races: destination preserved, outcome overwrite_race")
print("final dest:", dest.read_text())
