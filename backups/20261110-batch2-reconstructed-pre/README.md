# Reconstructed pre-batch-2 baseline

These five files represent Misty's state immediately before the confirmation,
clipboard-expiry, and activity-redaction patch.

They were reconstructed on 2026-11-10 from the exact reverse of that patch,
because the patch was applied before a snapshot existed. They are not a byte
copy of an original filesystem snapshot. Each reconstruction replacement was
required to match exactly once, the resulting files compile with Python 3.14,
and their diff against the live files contains only the intended batch-2
changes.

`RESTORE.sh` restores these files, quarantines the current versions, and moves
`app/engine/confirmations.py` out of the import path. It refuses to run if the
project root does not contain `misty.py`.
