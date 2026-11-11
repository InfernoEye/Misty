"""Probe: silent no-overwrite failure when the cross-device fallback is needed."""
import sys, tempfile, pathlib, os
sys.dont_write_bytecode = True
from app.actions import fs_safe
T = pathlib.Path(tempfile.mkdtemp())
dest = T/"out.bin"; dest.write_text("existing")
src = T/"tmp.bin"; src.write_text("new")
saved = fs_safe._renameat2
# simulate a kernel/fs without renameat2 (ENOSYS): module falls back to link+unlink
fs_safe._renameat2 = False
try:
    print("fallback publish to OTHER filesystem would be needed -> fs_safe returns:",
          fs_safe.publish_noreplace(str(src), str(dest)))
    import inspect, app.actions.file_ops as fo, app.actions.file_transfer as ft, app.actions.archive_zip as az
    for mod in (fo, ft, az):
        tree = inspect.getsource(mod)
        handled = "PUBLISH_UNSUPPORTED" in tree
        print(f"{mod.__name__}: handles PUBLISH_UNSUPPORTED explicitly? {handled}")
finally:
    fs_safe._renameat2 = saved
