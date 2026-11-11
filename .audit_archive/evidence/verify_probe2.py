"""Probe: fs_safe primitives -- symlink races, no-overwrite publish."""
import sys, os, errno, tempfile, stat
sys.dont_write_bytecode = True
from pathlib import Path
from app.actions import fs_safe

tmp = Path(tempfile.mkdtemp(prefix="fsprobe-"))
print("tmp:", tmp)

# 1) remove_private_tree when the ROOT was swapped for a symlink to a victim dir
victim = tmp/"victim"; victim.mkdir(); (victim/"keep.txt").write_text("important")
root = tmp/"staging"; root.mkdir(); (root/"f").write_text("x")
import shutil; shutil.rmtree(root)  # simulate: misty created it then attacker replaced it
os.symlink(victim, root)
print("1) remove_private_tree(root->symlink):", fs_safe.remove_private_tree(root),
      "victim intact:", (victim/"keep.txt").exists(), "root lexists:", os.path.lexists(root))

# 2) inner symlink to a directory outside the tree
root2 = tmp/"staging2"; root2.mkdir(); (root2/"a").write_text("a")
os.symlink(victim, root2/"link-to-victim")
print("2) remove_private_tree with inner symlink:", fs_safe.remove_private_tree(root2),
      "victim intact:", (victim/"keep.txt").exists(), "root2 gone:", not os.path.lexists(root2))

# 3) publish_noreplace over an existing file (no clobber)
dest_dir = tmp/"dest"; dest_dir.mkdir()
dest = dest_dir/"out.txt"; dest.write_text("existing")
src = tmp/"t1.tmp"; src.write_text("new")
print("3) publish file over existing:", fs_safe.publish_noreplace(str(src), str(dest)),
      "dest content:", dest.read_text())

# 4) publish_noreplace of tree over existing dir
srctree = tmp/"t2dir"; srctree.mkdir(); (srctree/"f").write_text("new")
desttree = dest_dir/"tree"; desttree.mkdir(); (desttree/"old").write_text("old")
print("4) publish tree over existing dir:", fs_safe.publish_noreplace(str(srctree), str(desttree)),
      "dest entries:", sorted(p.name for p in desttree.iterdir()))

# 5) forced fallback path: simulate renameat2 missing
saved = fs_safe._renameat2
fs_safe._renameat2 = False
try:
    src3 = tmp/"t3.tmp"; src3.write_text("new3")
    dest3 = dest_dir/"out3.txt"
    print("5) link+unlink fallback:", fs_safe.publish_noreplace(str(src3), str(dest3)), dest3.read_text(), "src exists:", src3.exists())
    s4 = tmp/"t4dir"; s4.mkdir(); (s4/"f").write_text("n")
    d4 = dest_dir/"tree4"
    print("5b) marker fallback tree:", fs_safe.publish_noreplace(str(s4), str(d4)), sorted(p.name for p in d4.iterdir()))
    s5 = tmp/"t5dir"; s5.mkdir(); (s5/"f").write_text("n")
    d5 = dest_dir/"tree5"; d5.write_text("existing file")
    print("5c) marker fallback over existing file:", fs_safe.publish_noreplace(str(s5), str(d5)), "d5:", d5.read_text(), "s5 exists:", s5.exists())
finally:
    fs_safe._renameat2 = saved

# 6) temp file whose name was replaced by a hardlink to a victim file, then remove
print("6) _renameat2 loaded:", fs_safe._load_renameat2() is not None)
