def windows_of_app(windows, app_id):
    """Return the windows owned by the application with exactly ``app_id``.

    Matching is by the exact desktop application id the launcher uses, never
    by title or fuzzy text.
    """
    return [w for w in windows if w.get("app_id") == app_id]


def choose_target(candidates):
    """Decide how to target an application's windows.

    Returns ``("none", [])`` when the app has no window, ``("one", [window])``
    when there is exactly one, and ``("choose", candidates)`` when the user
    must pick between several.
    """
    if not candidates:
        return "none", []
    if len(candidates) == 1:
        return "one", candidates
    return "choose", candidates
