"""Single-resource model lease arbitration for Misty.

Misty runs three heavy local resources one at a time on a normal desktop:
the reasoning model (llama.cpp ``llama-server``), the vision model
(``llama-mtmd-cli``) and speech-to-text (``whisper-cli``).  This module is an
isolated, dependency-free arbiter that decides *which* of those resources may
be loaded at any moment and runs the caller-supplied load/unload callbacks in
a deterministic order.

It deliberately does not import Qt, ``subprocess``, ``os``, network, config,
memory, the agent or any tool module.  It never constructs a model, never
looks at a prompt, an audio buffer, an image or a path, keeps no state outside
its own objects, performs no I/O and writes no logs.  The only things it knows
about a resource are its name and two injected callables, so private content
cannot reach a message, a diagnostic dict or a log line.  This module exists
so that ``agent.py``, the vision service and the speech transcriber can share
one arbiter once their lifecycles are wired together.

Resource registration
---------------------
``register(name, load, unload)`` stores two callables.  Both are called with
no arguments and must return exactly ``True`` to signal success; ``False``,
``None`` or any other value counts as a failure, and so does any exception
they raise.  Callbacks are supplied by the caller (typically a closure around
an existing process/Python-object adapter) and are never constructed here.
Names are lowercase identifiers such as ``reasoning``, ``vision`` and
``whisper``.

Exclusive ownership
-------------------
At most one resource is loaded at any time and at most one lease exists per
resource.  A thread that holds a lease is the only thread allowed to use that
model; another thread asking for the same resource waits for the holder to
release it (bounded by ``timeout``).  A thread that already holds a lease may
not acquire anything else: acquiring the same resource again is rejected with
``reentrant`` and acquiring a *different* resource is rejected with
``lease_held``.  There is no silent nesting.

Switching
---------
When the requested resource is not the loaded one, and the loaded one is not
in use by another thread, the arbiter unloads every loaded resource first (in
sorted name order, deterministic even if the single-loaded invariant were
ever broken), stopping on the first failure, and only then loads the target.
A failed unload never triggers a new load: the old resource stays marked
loaded and the call fails with ``unload_failed``.  A failed load leaves
nothing marked loaded and the call fails with ``load_failed``.

Release policy
--------------
``release(token)`` ends the lease; when that was the last lease for the
resource, the arbiter calls ``unload`` and only then marks the resource as
gone.  This mirrors current Misty behaviour, which unloads the model after
each request instead of keeping it warm: *nothing is reloaded automatically*.

The optional ``restore=True`` keyword of ``acquire`` is the only way to ask
for a reload of a previous resource, and even then only when a switch actually
unloaded a still-loaded resource for this lease (recovery from a failed
unload).  In the ordinary flow the previous model is already unloaded before
the new lease starts, so restore is a no-op.  Restore is off by default and
never happens for ``restore=False``.

States (``state()["state"]``)
-----------------------------
=======  ==============================================================
idle     nothing loaded, no switch running
loading  a load/unload callback sequence is running
loaded   exactly one resource is loaded
blocked  a resource is still loaded after a failed unload; a later
         acquire will retry the unload before loading anything else
=======  ==============================================================

Error codes (fixed text, never callback exception text)
-------------------------------------------------------
``loaded``, ``already_loaded``, ``released`` (success); ``unknown_resource``,
``timeout``, ``reentrant``, ``lease_held``, ``load_failed``,
``unload_failed``, ``restore_failed``, ``invalid_token``,
``already_released`` (failure).

Concurrency
-----------
All bookkeeping lives behind one ``threading.Condition`` over an ``RLock``.
Callbacks run *outside* the lock, so ``state()`` and ``loaded_resources()``
stay responsive while a model is starting, and a slow callback can never
deadlock a diagnostic reader.  Waiters use a bounded ``time.monotonic()``
deadline; on expiry they return a ``timeout`` result instead of blocking.
Only one switch may run at a time.  ``timeout=None`` waits without a bound
and should only be used by callers that are sure the holder will finish.
"""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import re
import threading
import time


__all__ = [
    "DEFAULT_TIMEOUT",
    "LEASE_CODES",
    "STATE_NAMES",
    "LeaseError",
    "LeaseResult",
    "LeaseToken",
    "ModelLease",
    "ModelLeaseError",
    "plan_switch",
]


DEFAULT_TIMEOUT = 5.0

MAX_RESOURCE_NAME_LENGTH = 64

_RESOURCE_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")

STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_LOADED = "loaded"
STATE_BLOCKED = "blocked"

STATE_NAMES = (STATE_IDLE, STATE_LOADING, STATE_LOADED, STATE_BLOCKED)

# Deterministic order for diagnostics and documentation.
LEASE_CODES = (
    "loaded",
    "already_loaded",
    "released",
    "unknown_resource",
    "timeout",
    "reentrant",
    "lease_held",
    "load_failed",
    "unload_failed",
    "restore_failed",
    "invalid_token",
    "already_released",
)

# Fixed user-facing text.  Callback exception text is never interpolated.
_MESSAGES = {
    "loaded": "The requested model resource is loaded.",
    "already_loaded": "The requested model resource was already loaded.",
    "released": "The model lease was released.",
    "unknown_resource": "The requested model resource is not registered.",
    "timeout": "Timed out waiting for the model resource.",
    "reentrant": "This thread already holds a lease on the requested resource.",
    "lease_held": "This thread already holds a lease on another resource.",
    "load_failed": "The model resource could not be loaded.",
    "unload_failed": "The loaded model resource could not be unloaded.",
    "restore_failed": "The previous model resource could not be restored.",
    "invalid_token": "The model lease token is not valid for this arbiter.",
    "already_released": "The model lease was already released.",
}

_LOAD = "load"
_UNLOAD = "unload"

_GRANT = "grant"
_SWITCH = "switch"
_WAIT = "wait"

_COUNTER_NAMES = (
    "acquired",
    "released",
    "rejected",
    "timeouts",
    "loads",
    "unloads",
    "load_failures",
    "unload_failures",
)

_RELEASED_TOKEN_MEMORY = 256


class LeaseError(ValueError):
    """Raised for invalid arguments passed to this module.

    These are programming errors (a non-string resource name, a negative or
    non-finite timeout, a forged token object) rather than runtime lease
    outcomes, which are always returned as a ``LeaseResult``.
    """


class ModelLeaseError(RuntimeError):
    """Raised by the context-manager API when a lease cannot be obtained or
    cannot be released cleanly.

    ``code`` carries one of ``LEASE_CODES`` so callers can branch on a fixed
    identifier instead of parsing text.  The message is always fixed text and
    never contains callback exception content.
    """

    def __init__(self, message, *, code="lease_error"):
        super().__init__(message)
        self.code = code


def _is_valid_resource_name(value):
    return (
        type(value) is str
        and _RESOURCE_NAME_PATTERN.fullmatch(value) is not None
    )


def _require_resource_name(value):
    if type(value) is not str:
        raise LeaseError("name must be a plain string")
    if not _is_valid_resource_name(value):
        raise LeaseError(
            "name must match [a-z][a-z0-9_]* and be at most "
            f"{MAX_RESOURCE_NAME_LENGTH} characters"
        )


def _deadline(timeout):
    """Convert a timeout in seconds to an absolute monotonic deadline."""
    if timeout is None:
        return None
    if type(timeout) is bool or not isinstance(timeout, (int, float)):
        raise LeaseError("timeout must be a number of seconds or None")
    value = float(timeout)
    if math.isnan(value) or math.isinf(value) or value < 0.0:
        raise LeaseError("timeout must be finite and not negative")
    return time.monotonic() + value


def _message(code):
    return _MESSAGES[code]


@dataclass(frozen=True, slots=True)
class LeaseToken:
    """Opaque handle for one granted lease.

    Tokens are created only by :meth:`ModelLease.acquire`.  A token can be
    released once; a copy or a hand-built token is rejected by
    ``ModelLease.release`` with ``invalid_token``.
    """

    resource: str
    lease_id: int
    thread_ident: int = field(default=0, repr=False, compare=False)
    restore_resource: str | None = field(default=None, repr=False,
                                         compare=False)

    def to_dict(self):
        """Return a JSON-safe description of this token."""
        return {"resource": self.resource, "lease_id": self.lease_id}


@dataclass(frozen=True, slots=True)
class LeaseResult:
    """JSON-safe outcome of one acquire/release call.

    ``ok`` is True only for a granted lease or a clean release.  ``code`` is a
    member of ``LEASE_CODES`` and ``message`` is fixed text.  A successful
    result is a context manager and also offers ``release()``; leaving the
    ``with`` block releases the lease exactly once.
    """

    ok: bool
    code: str
    message: str
    resource: str | None = None
    token: LeaseToken | None = None
    unloaded: tuple = ()
    loaded: bool = False
    restored: str | None = None
    _arbiter: object = field(default=None, repr=False, compare=False)

    def __enter__(self):
        if not self.ok:
            raise ModelLeaseError(self.message, code=self.code)
        return self

    def __exit__(self, exc_type, exc, traceback):
        result = self.release()
        if not result.ok and exc_type is None:
            # Never swallow a failed unload behind a silent ``with`` exit.
            raise ModelLeaseError(result.message, code=result.code)
        return False

    def release(self):
        """Release this lease using the arbiter that granted it."""
        arbiter = self._arbiter
        if arbiter is None or self.token is None:
            return LeaseResult(
                ok=False,
                code="invalid_token",
                message=_message("invalid_token"),
                resource=self.resource,
            )
        return arbiter.release(self.token)

    def to_dict(self):
        """Return this result as plain JSON-safe data."""
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "resource": self.resource,
            "unloaded": list(self.unloaded),
            "loaded": self.loaded,
            "restored": self.restored,
            "lease_id": None if self.token is None else self.token.lease_id,
        }


def plan_switch(current, target):
    """Return a pure, JSON-safe plan for moving between two resources.

    ``current`` may be ``None`` (nothing loaded).  The plan lists the callback
    steps the arbiter would run, in order, without touching any state:
    ``["unload_current", "load_target"]`` for a real switch,
    ``["load_target"]`` for a cold start, or ``[]`` when already on target.
    Invalid inputs yield ``valid=False`` rather than raising.
    """
    current_name = current if _is_valid_resource_name(current) else None
    target_name = target if _is_valid_resource_name(target) else None
    valid = (
        target_name is not None
        and (current is None or current_name is not None)
    )

    if not valid:
        steps = []
    elif current_name == target_name:
        steps = []
    elif current_name is None:
        steps = ["load_target"]
    else:
        steps = ["unload_current", "load_target"]

    return {
        "current": current_name,
        "target": target_name,
        "valid": valid,
        "needed": bool(steps),
        "steps": steps,
    }


class ModelLease:
    """Thread-safe arbiter for named, exclusively owned model resources.

    One resource may be loaded at a time and one lease may exist per resource.
    ``acquire`` returns a ``LeaseResult``; on success its ``token`` is
    released with ``release`` or by leaving the result's ``with`` block.
    Callback failures never raise out of ``acquire``/``release`` or mutate
    state behind the caller's back; they become ``load_failed`` /
    ``unload_failed`` results and honest diagnostics.
    """

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._registry = {}
        self._loaded = None
        self._lease_by_resource = {}
        self._leases = {}
        self._released = set()
        self._released_order = deque()
        self._switching = None
        self._unload_failed = False
        self._last_error = None
        self._next_id = 1
        self._counters = {name: 0 for name in _COUNTER_NAMES}

    # -- introspection ---------------------------------------------------
    def __repr__(self):
        with self._condition:
            return (
                "ModelLease("
                f"state={self._state_name_locked()!r}, "
                f"loaded={self._loaded!r}, "
                f"registered={len(self._registry)})"
            )

    def registered_resources(self):
        """Return the registered resource names in sorted order."""
        with self._condition:
            return tuple(sorted(self._registry))

    def loaded_resources(self):
        """Return the loaded resource names in sorted order (0 or 1 entry).

        The tuple serialises as a JSON array.
        """
        with self._condition:
            if self._loaded is None:
                return ()
            return (self._loaded,)

    def state(self):
        """Return a JSON-safe diagnostic snapshot of the arbiter."""
        with self._condition:
            if self._switching is None:
                switching = None
            else:
                switching = {
                    "from": self._switching[0],
                    "to": self._switching[1],
                }
            return {
                "state": self._state_name_locked(),
                "loaded": self._loaded,
                "loaded_resources": [] if self._loaded is None
                else [self._loaded],
                "leases": [
                    {
                        "resource": token.resource,
                        "lease_id": token.lease_id,
                        "thread": token.thread_ident,
                    }
                    for token in sorted(
                        self._leases.values(), key=lambda item: item.lease_id
                    )
                ],
                "registered": sorted(self._registry),
                "switching": switching,
                "last_error": None if self._last_error is None
                else dict(self._last_error),
                "counters": dict(self._counters),
            }

    def _state_name_locked(self):
        if self._switching is not None:
            return STATE_LOADING
        if self._loaded is None:
            return STATE_IDLE
        if self._unload_failed:
            return STATE_BLOCKED
        return STATE_LOADED

    # -- registration ----------------------------------------------------
    def register(self, name, load, unload, *, replace=False):
        """Register load/unload callbacks for one resource.

        Returns True only when the entry was added.  Invalid names, non
        callables, duplicates without ``replace=True`` and replacing a
        resource that is currently loaded or leased all yield False and leave
        the registry unchanged.  Callbacks are stored, never called here.
        """
        if not _is_valid_resource_name(name):
            return False
        if not callable(load) or not callable(unload):
            return False
        if type(replace) is not bool:
            return False
        with self._condition:
            existing = name in self._registry
            if existing and not replace:
                return False
            busy = (
                self._loaded == name
                or name in self._lease_by_resource
                or (self._switching is not None
                    and name in self._switching)
            )
            if existing and busy:
                return False
            self._registry[name] = (load, unload)
        return True

    # -- acquire ---------------------------------------------------------
    def acquire(self, name, timeout=DEFAULT_TIMEOUT, *, restore=False):
        """Try to obtain an exclusive lease on ``name``.

        Returns a ``LeaseResult``; ``ok`` is True only when the lease is
        granted and the resource is loaded.  Waiting is bounded by
        ``timeout`` seconds (``None`` waits forever); on expiry the result has
        code ``timeout`` and nothing changed.  ``restore=True`` asks the
        arbiter to reload a resource this call had to unload once the lease
        ends; it is off by default and never applied silently.
        """
        _require_resource_name(name)
        if type(restore) is not bool:
            raise LeaseError("restore must be a boolean")
        deadline = _deadline(timeout)
        thread_ident = threading.get_ident()

        with self._condition:
            if name not in self._registry:
                self._counters["rejected"] += 1
                return self._result(False, "unknown_resource", resource=name)

            holder = self._lease_by_resource.get(name)
            if holder is not None and holder.thread_ident == thread_ident:
                self._counters["rejected"] += 1
                return self._result(False, "reentrant", resource=name)
            if self._thread_holds_lease_locked(thread_ident):
                self._counters["rejected"] += 1
                return self._result(False, "lease_held", resource=name)

            while True:
                decision = self._decision_locked(name)
                if decision != _WAIT:
                    break
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and remaining <= 0.0:
                    self._counters["timeouts"] += 1
                    return self._result(False, "timeout", resource=name)
                self._condition.wait(remaining)

            if decision == _GRANT:
                token = self._grant_locked(name, thread_ident, None)
                self._counters["acquired"] += 1
                return self._result(
                    True, "already_loaded", resource=name, token=token
                )

            # Claim at decision time: the resource may have changed while
            # waiting, so the source of the switch is read here, not earlier.
            previous = self._loaded
            self._switching = (previous, name)

        restore_target = previous if restore else None
        return self._complete_switch(name, thread_ident, restore_target)

    def _decision_locked(self, name):
        if self._switching is not None:
            return _WAIT
        if self._loaded == name:
            if self._lease_by_resource.get(name) is not None:
                return _WAIT
            return _GRANT
        if (
            self._loaded is not None
            and self._lease_by_resource.get(self._loaded) is not None
        ):
            # Never yank a model another thread is actively using.
            return _WAIT
        return _SWITCH

    def _thread_holds_lease_locked(self, thread_ident):
        return any(
            token.thread_ident == thread_ident
            for token in self._leases.values()
        )

    def _grant_locked(self, name, thread_ident, restore_target):
        lease_id = self._next_id
        self._next_id += 1
        token = LeaseToken(
            resource=name,
            lease_id=lease_id,
            thread_ident=thread_ident,
            restore_resource=restore_target,
        )
        self._leases[lease_id] = token
        self._lease_by_resource[name] = token
        return token

    def _complete_switch(self, target, thread_ident, restore_target):
        completed = False
        failure = None
        unloaded = ()
        token = None
        try:
            failure, unloaded = self._run_switch_steps(target)
            completed = True
        finally:
            with self._condition:
                if completed:
                    if failure is None:
                        self._loaded = target
                        self._unload_failed = False
                        self._last_error = None
                        token = self._grant_locked(
                            target, thread_ident, restore_target
                        )
                        self._counters["acquired"] += 1
                    else:
                        if failure == "unload_failed":
                            self._unload_failed = True
                        self._last_error = {
                            "code": failure,
                            "message": _message(failure),
                        }
                self._switching = None
                self._condition.notify_all()

        if failure is not None:
            return self._result(
                False, failure, resource=target, unloaded=unloaded
            )
        return self._result(
            True,
            "loaded",
            resource=target,
            token=token,
            unloaded=unloaded,
            loaded=True,
        )

    def _run_switch_steps(self, target):
        """Unload every loaded resource, then load ``target``.

        Runs without holding the lock and returns ``(failure_code, unloaded)``.
        """
        with self._condition:
            previous = self._switching[0]
            if previous is not None and self._loaded != previous:
                # Nothing to unload: the source is already gone.
                previous = None
        if previous is not None:
            if not self._invoke(previous, _UNLOAD):
                return "unload_failed", ()
            with self._condition:
                if self._loaded == previous:
                    self._loaded = None
                self._unload_failed = False
                self._condition.notify_all()
            unloaded = (previous,)
        else:
            unloaded = ()

        if not self._invoke(target, _LOAD):
            return "load_failed", unloaded
        return None, unloaded

    # -- release ---------------------------------------------------------
    def release(self, token):
        """Release a lease, unloading the resource when it was the last one.

        Returns a ``LeaseResult``.  Release from any thread is allowed; a
        token can be released only once.  If the unload callback fails the
        resource stays marked loaded (state ``blocked``) and the result has
        code ``unload_failed``.
        """
        if not isinstance(token, LeaseToken):
            raise LeaseError("token must be a LeaseToken")

        with self._condition:
            current = self._leases.get(token.lease_id)
            if current is None or current != token:
                if token.lease_id in self._released:
                    return self._release_result(
                        False, "already_released", token
                    )
                return self._release_result(False, "invalid_token", token)

            del self._leases[token.lease_id]
            if self._lease_by_resource.get(token.resource) is token:
                del self._lease_by_resource[token.resource]
            self._remember_released_locked(token.lease_id)
            self._counters["released"] += 1

            if self._switching is not None or self._loaded != token.resource:
                # Already unloaded by a switch, or a switch owns the lifecycle.
                return self._release_result(
                    True, "released", token, loaded=self._loaded is not None
                )
            if self._leases_for_locked(token.resource):
                return self._release_result(True, "released", token,
                                            loaded=True)

            self._switching = (token.resource, None)

        return self._complete_release(token)

    def _leases_for_locked(self, name):
        return [
            token for token in self._leases.values()
            if token.resource == name
        ]

    def _remember_released_locked(self, lease_id):
        self._released.add(lease_id)
        self._released_order.append(lease_id)
        while len(self._released_order) > _RELEASED_TOKEN_MEMORY:
            self._released.discard(self._released_order.popleft())

    def _complete_release(self, token):
        completed = False
        ok = False
        code = "unload_failed"
        restored = None
        try:
            ok, code, restored = self._run_release_steps(token)
            completed = True
        finally:
            with self._condition:
                if completed and ok:
                    self._last_error = None
                elif completed:
                    self._last_error = {
                        "code": code,
                        "message": _message(code),
                    }
                self._switching = None
                self._condition.notify_all()

        return self._release_result(
            ok, code, token, restored=restored, loaded=self._loaded_snapshot()
        )

    def _run_release_steps(self, token):
        name = token.resource
        if not self._invoke(name, _UNLOAD):
            with self._condition:
                if self._loaded == name:
                    self._unload_failed = True
            return False, "unload_failed", None

        with self._condition:
            if self._loaded == name:
                self._loaded = None
            self._unload_failed = False
            self._condition.notify_all()

        restore = token.restore_resource
        if restore is None or restore == name:
            return True, "released", None
        with self._condition:
            if restore not in self._registry or self._loaded is not None:
                return True, "released", None
        if not self._invoke(restore, _LOAD):
            return False, "restore_failed", None

        with self._condition:
            self._loaded = restore
            self._unload_failed = False
            self._condition.notify_all()
        return True, "released", restore

    def _loaded_snapshot(self):
        with self._condition:
            return self._loaded is not None

    # -- callbacks -------------------------------------------------------
    def _invoke(self, name, kind):
        """Call one injected callback outside the lock.

        Returns True only when it returned exactly ``True``.  Exceptions are
        converted to a failure; their text is never stored or re-raised by
        this module.
        """
        with self._condition:
            callbacks = self._registry.get(name)
        if callbacks is None:
            ok = False
        else:
            callback = callbacks[0] if kind == _LOAD else callbacks[1]
            try:
                ok = callback() is True
            except Exception:
                ok = False
        with self._condition:
            if kind == _LOAD:
                key = "loads" if ok else "load_failures"
            else:
                key = "unloads" if ok else "unload_failures"
            self._counters[key] += 1
        return ok

    # -- context managers ------------------------------------------------
    @contextmanager
    def lease(self, name, timeout=DEFAULT_TIMEOUT, *, restore=False):
        """Context manager holding a lease for the duration of the block.

        Yields the granted ``LeaseResult``.  A lease that cannot be obtained
        raises ``ModelLeaseError`` with the same fixed ``code`` as the result
        (``timeout``, ``unknown_resource``, ``load_failed``, ...).  On exit the
        lease is released; a failed unload during a clean exit raises
        ``ModelLeaseError`` instead of being ignored.  When the block itself
        raised, that exception wins and the unload failure stays visible in
        ``state()["last_error"]``.
        """
        result = self.acquire(name, timeout=timeout, restore=restore)
        if not result.ok:
            raise ModelLeaseError(result.message, code=result.code)
        try:
            yield result
        except BaseException:
            self.release(result.token)
            raise
        release_result = self.release(result.token)
        if not release_result.ok:
            raise ModelLeaseError(
                release_result.message, code=release_result.code
            )

    # -- result helpers --------------------------------------------------
    def _result(self, ok, code, *, resource=None, token=None, unloaded=(),
                loaded=False, restored=None):
        return LeaseResult(
            ok=ok,
            code=code,
            message=_message(code),
            resource=resource,
            token=token,
            unloaded=tuple(unloaded),
            loaded=loaded,
            restored=restored,
            _arbiter=self,
        )

    def _release_result(self, ok, code, token, *, restored=None,
                        loaded=False):
        return LeaseResult(
            ok=ok,
            code=code,
            message=_message(code),
            resource=token.resource,
            token=None,
            unloaded=(),
            loaded=loaded,
            restored=restored,
            _arbiter=None,
        )
