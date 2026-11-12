
def process_session_id(pid):
    """Return the session id of ``pid``, or None when it has exited."""
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
    if not session_id:
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
