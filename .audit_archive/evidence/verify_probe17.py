import sys, os, shutil, tempfile
from pathlib import Path
sys.dont_write_bytecode = True
from app.actions import fs_safe
tmp = Path(tempfile.mkdtemp(prefix="sweep-"))
tree = tmp/"staging"; tree.mkdir()
tree.joinpath("f").write_text("x")
shutil.rmtree(tree)
victim = tmp/"victim"; victim.mkdir(); (victim/"keep").write_text("k")
os.symlink(victim, tree)
ok, leftover = fs_safe.remove_tree_confined(tree)
current = os.stat(tree, follow_symlinks=False)
print("remove_tree_confined ->", (ok, leftover),
      "| stale symlink still present:", os.path.lexists(tree),
      "| kernel says ENOENT for callers:", end=" ")
try:
    print(os.stat(tree, follow_symlinks=False) is not None)
except FileNotFoundError:
    print("(gone)")
# Is the name free for reuse even while the stale link remains?
tree2 = tmp/"staging2"; tree2.mkdir(); shutil.rmtree(tree2); os.symlink(victim, tree2)
fs_safe.remove_tree_confined(tree2)
print("stale link lexists:", os.path.lexists(tree2), "| still a symlink:",
      (os.path.lexists(tree2) and os.path.islink(tree2)))
