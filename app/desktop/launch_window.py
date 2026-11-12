
def process_session_id(pid):
    """Return a process session id, or None for an invalid/exited process."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return int(stat.read().rsplit(")", 1)[1].split()[3])
    except (OSError, IndexError, ValueError):
        return None


def window_in_session(windows, session_id):
    """Return the one window whose owning process is in ``session_id``.

    Identity is the process session, not the application name. Returns None
    when no window belongs to the session, and raises when more than one does,
    so an ambiguous launch never selects a window silently.
    """
    # Zero can represent a session outside the visible PID namespace; it is
    # not usable proof that this launch owns another process's window.
    if (isinstance(session_id, bool) or not isinstance(session_id, int)
            or session_id <= 0):
        return None
    matches = [
        window for window in windows
        if window.get("pid") and process_session_id(window["pid"]) == session_id
    ]
    if len(matches) > 1:
        raise RuntimeError(
            f"{len(matches)} windows belong to launch session {session_id}."
        )
    return matches[0] if matches else None
