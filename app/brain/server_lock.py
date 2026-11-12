"""Serializes requests to the single shared local model server.

The server runs with one slot (``-np 1``). Planning and vision both call it,
so they take this lock to never interleave on that slot.
"""

import threading

SHARED_SERVER_LOCK = threading.Lock()
