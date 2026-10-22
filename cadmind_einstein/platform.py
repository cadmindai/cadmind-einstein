"""The validator's client of the platform's agent API (DESIGN.md). The platform splits the
operator's credential by role, one agent key each:

==============  =============  ===================================================================
role            key            calls
==============  =============  ===================================================================
``workspaces``  ``key``        grant, state, freeze, fork, and ``watt`` runs, run polls, export
                               and delete on the validator's own forks
``keys``        ``mint_key``   mint and disable miners' keys
``queue``       ``queue_key``  claim, lease, claimed and complete (a delivered result charges the
                               client), at ``queue_url``
==============  =============  ===================================================================

An unset role key is the operator key (the platform's single-key mode).

**A frozen role.** The platform freezes a role's key past its ceilings (429 ``operator_ceiling`` on
the act, then 403 ``operator_frozen`` on every call) until the owner enables it again. The client
then stops that role: an ALERT is logged once, every call of the role fails at once, locally, with
:class:`RoleFrozenError` (status 503: our side, never a miner's failure), and the platform is asked
again only every ``recheck_s`` (5 minutes) -- by the next call after that. The other roles go on.
**A wrong key** (403 ``operator_role``: the key does not hold the role) raises
:class:`RoleRefusedError`, logged as an ALERT; :meth:`OperatorClient.probe` finds it at startup.
A 401 is a wrong or revoked key (on the queue, also an address the platform does not
allowlist): logged as an ALERT. **Back-pressure**: 429 ``too_many_in_flight`` and 503 hold the
role off for the answer's ``Retry-After`` (5 s if none), failing its calls at once meanwhile --
the desk retries them on a later tick, never in a loop.

Stdlib only. Every failure is a :class:`PlatformError` carrying the HTTP status and the platform's
code; ``ours`` marks a failure on the validator's side (its credential, the platform's load).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote


class PlatformError(Exception):
    def __init__(
        self, message: str, status: int = 0, code: str = "", *, ours: bool = False
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.ours = status, code, ours


class RoleFrozenError(PlatformError):
    """The role's key is frozen on the platform (its ceiling): our side, retried later."""


class RoleRefusedError(PlatformError):
    """The key does not hold the role (403 ``operator_role``): a misconfigured validator."""


WORKSPACES, KEYS, QUEUE = "workspaces", "keys", "queue"
ROLES = (WORKSPACES, KEYS, QUEUE)
FROZEN = ("operator_frozen", "operator_ceiling")
RECHECK_S = 300.0
BACKOFF_S = 5.0
POLL_RETRIES = 3
POLL_BACKOFF_S = 2.0
"""a failed poll of a run is asked again this many times (2, 4, then 8 s apart) before it fails"""
ALERT_EVERY_S = 300.0
"""the same ALERT is logged at most this often"""


def _wait(end: float) -> int:
    """A long-poll's wait: what is left of the budget, between 1 and 100 seconds."""
    return int(max(1.0, min(100.0, end - time.monotonic())))


def _retry_after(headers: Any) -> float:
    try:
        return max(0.5, min(120.0, float(headers.get("Retry-After") or BACKOFF_S)))
    except (TypeError, ValueError):
        return BACKOFF_S


class OperatorClient:
    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        mint_key: str = "",
        queue_key: str = "",
        queue_url: str = "",
        timeout: float = 160.0,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        recheck_s: float = RECHECK_S,
        poll_retries: int = POLL_RETRIES,
        poll_backoff_s: float = POLL_BACKOFF_S,
    ) -> None:
        self.base, self.timeout = base_url.rstrip("/"), timeout
        self.queue = (queue_url or base_url).rstrip("/")
        self.keys = {WORKSPACES: key, KEYS: mint_key or key, QUEUE: queue_key or key}
        self.log = log or (lambda _line: None)
        self.clock, self.recheck_s = clock, recheck_s
        self.poll_retries, self.poll_backoff_s = max(0, poll_retries), poll_backoff_s
        self._frozen: dict[str, float] = {}
        """per role, when the platform is asked again (a frozen key)"""
        self._held: dict[str, float] = {}
        """per role, the end of a back-off the platform asked for (Retry-After)"""
        self._said: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def key(self) -> str:
        return self.keys[WORKSPACES]

    def frozen(self) -> list[str]:
        """The roles frozen now (the hourly log and ``/v1/chain`` say so)."""
        with self._lock:
            return sorted(self._frozen)

    def _alert(self, what: str, line: str) -> None:
        now = self.clock()
        with self._lock:
            if now - self._said.get(what, -1e18) < ALERT_EVERY_S:
                return
            self._said[what] = now
        self.log(f"ALERT: {line}")

    def _before(self, role: str) -> None:
        now = self.clock()
        with self._lock:
            again = self._frozen.get(role)
            held = self._held.get(role, 0.0)
        if again is not None and now < again:
            raise RoleFrozenError(
                f"the validator's {role} key is frozen on the platform; asked again in "
                f"{again - now:.0f} s",
                503,
                "operator_frozen",
                ours=True,
            )
        if now < held:
            raise PlatformError(
                f"the platform asked the {role} role to wait {held - now:.0f} s",
                503,
                "backoff",
                ours=True,
            )

    def _after_ok(self, role: str) -> None:
        with self._lock:
            was = self._frozen.pop(role, None)
            self._held.pop(role, None)
        if was is not None:
            self.log(f"the validator's {role} key works again: the role resumes")

    def _refused(self, role: str, status: int, code: str, message: str, headers: Any) -> None:
        """Raise the failure; freeze, hold off or alert on the way."""
        if code in FROZEN:
            with self._lock:
                self._frozen[role] = self.clock() + self.recheck_s
            self._alert(
                f"frozen:{role}",
                f"the platform froze the validator's {role} key ({code}: {message}); the role is "
                f"stopped and asked again every {self.recheck_s / 60:g} min -- the owner must "
                "enable the key again",
            )
            raise RoleFrozenError(message, 503, code, ours=True)
        if code == "operator_role":
            self._alert(
                f"role:{role}",
                f"the key configured for the {role} role does not hold it ({message}): fix the "
                "validator's key files",
            )
            raise RoleRefusedError(message, status, code, ours=True)
        if status == 401:
            where = " or this address is not allowlisted for the queue" if role == QUEUE else ""
            self._alert(
                f"401:{role}",
                f"the platform refused the validator's {role} key (401: {message}): a wrong or "
                f"revoked key{where}",
            )
            raise PlatformError(message, status, code, ours=True)
        if (status == 429 and code == "too_many_in_flight") or status == 503:
            with self._lock:
                self._held[role] = self.clock() + _retry_after(headers)
            raise PlatformError(message, 503, code or "busy", ours=True)
        raise PlatformError(message, status, code)

    def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        role: str = WORKSPACES,
        raw: bool = False,
        timeout: float | None = None,
        base: str = "",
    ) -> Any:
        self._before(role)
        data = json.dumps(dict(body)).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 -- a configured http(s) URL
            (base or self.base) + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.keys[role]}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:  # noqa: S310 -- configured
                out = r.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            self._refused(
                role,
                e.code,
                str(err.get("code") or ""),
                str(err.get("error") or e.reason),
                e.headers,
            )
            raise AssertionError("unreachable") from None  # pragma: no cover -- _refused raises
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise PlatformError(f"platform unreachable: {e}") from None
        self._after_ok(role)
        return out if raw else json.loads(out or b"{}")

    def probe(self) -> dict[str, str]:
        """At startup: each role's key that can be checked without acting -- the workspaces key
        reads a workspace that does not exist (404), the queue key reads its claimed jobs. Answers
        role -> ``ok``, ``frozen``, ``unreachable`` or ``unchecked`` (the mint key: it has no read;
        its first mint says). Raises :class:`RoleRefusedError` (or a 401 :class:`PlatformError`)
        on a wrong key: the daemon refuses to start."""
        out = {KEYS: "unchecked"}
        for role, call in (
            (WORKSPACES, lambda: self.state("einstein-startup-probe")),
            (QUEUE, self.claimed),
        ):
            try:
                call()
                out[role] = "ok"
            except RoleFrozenError:
                out[role] = "frozen"
            except PlatformError as e:
                if isinstance(e, RoleRefusedError) or e.status == 401:
                    raise
                out[role] = (
                    "ok"
                    if e.status == 404
                    else "unreachable"
                    if e.status == 0
                    else f"error {e.status} {e.code}".strip()
                )
        return out

    @staticmethod
    def _ws(ws: str) -> str:
        return quote(ws, safe="")

    # -- operator routes ---------------------------------------------------------------------------

    def mint_key(self, hotkey: str) -> dict[str, Any]:
        return dict(self._call("POST", "/api/agent/subnet/keys", {"hotkey": hotkey}, role=KEYS))

    def disable_key(self, hotkey: str) -> dict[str, Any]:
        return dict(
            self._call("POST", "/api/agent/subnet/keys/disable", {"hotkey": hotkey}, role=KEYS)
        )

    def grant(self, hotkey: str, *, brief: str = "", seed: str = "", name: str = "job") -> str:
        # ``job`` names the job the workspace is for: the platform's provenance check on a charging
        # result follows the delivered workspace back to this grant.
        body: dict[str, Any] = {"hotkey": hotkey, "name": name, "job": name}
        body.update({"seed": seed} if seed else {"brief": brief})
        return str(
            self._call("POST", "/api/agent/subnet/workspaces", body, timeout=320)["workspace"]
        )

    def grant_continuation(
        self, hotkey: str, *, seed: str, base: str = "", name: str = "job"
    ) -> tuple[str, str]:
        """A continuation's grant: the miner's workspace forked from ``seed`` (the unfinished
        project) and, when ``base`` names the project's last delivered version, a read-only fork of
        it granted beside it (``reference``), from which the builder may start over."""
        body: dict[str, Any] = {
            "hotkey": hotkey,
            "name": name,
            "job": name,
            "seed": seed,
            "continuation": True,
        }
        if base:
            body["base"] = base
        out = self._call("POST", "/api/agent/subnet/workspaces", body, timeout=320)
        return str(out["workspace"]), str(out.get("reference") or "")

    def state(self, ws: str) -> dict[str, Any]:
        return dict(self._call("GET", f"/api/agent/subnet/workspaces/{self._ws(ws)}"))

    def freeze(self, ws: str) -> dict[str, Any]:
        return dict(self._call("POST", f"/api/agent/subnet/workspaces/{self._ws(ws)}/freeze", {}))

    def fork(self, ws: str) -> str:
        return str(
            self._call(
                "POST", f"/api/agent/subnet/workspaces/{self._ws(ws)}/fork", {}, timeout=320
            )["workspace"]
        )

    # -- the platform's single job queue (engines pull) --------------------------------------------

    def claim(self) -> dict[str, Any] | None:
        """One einstein job from the platform's queue, now leased to this validator, or None:
        ``{job, brief, effort, privacy, seed?, rerun_of?, trust}``."""
        out = self._call("POST", "/api/engine/einstein/claim", {}, base=self.queue, role=QUEUE)
        job = out.get("job")
        return dict(job) if isinstance(job, dict) else None

    def lease(self, jobs: list[str], working: list[str] | None = None) -> dict[str, Any]:
        """Renew the lease on the jobs we hold; ``lost`` lists those the platform no longer gives us
        (cancelled, or re-queued after a lapsed lease). ``working``: the jobs among them a builder
        has accepted (being worked or judged); the platform charges a client's cancel only once a
        builder has (a platform that predates the field ignores it)."""
        body: dict[str, Any] = {"jobs": list(jobs)}
        if working is not None:
            body["working"] = list(working)
        return dict(
            self._call(
                "POST",
                "/api/engine/einstein/lease",
                body,
                base=self.queue,
                role=QUEUE,
            )
        )

    def claimed(self) -> list[dict[str, Any]]:
        """The jobs the platform holds leased to this validator (reconciled after a restart)."""
        out = self._call("GET", "/api/engine/einstein/claimed", base=self.queue, role=QUEUE)
        return [dict(j) for j in out.get("jobs") or []]

    def complete(self, job: str, result: Mapping[str, Any]) -> dict[str, Any]:
        """A job's end: delivered (charged once) or declined / out of reach / didn't finish
        (not). A delivered result names the validator's fork (``workspace``) and the digest its
        gate read (``digest``); the platform refuses it, 409 ``delivery_not_of_job`` or
        ``delivery_digest_mismatch``, when the fork does not descend from the job's own grant or
        is not at that digest (never retried), and answers 503 ``delivery_unverified`` when it
        cannot tell now (retried). Answers the platform's ``{accepted, charged, label}``:
        ``charged`` is whether the client was charged (false for a delivery the client cancelled
        meanwhile, or one that could not be kept)."""
        out = self._call(
            "POST",
            f"/api/engine/einstein/jobs/{quote(job, safe='')}/result",
            {"result": dict(result)},
            base=self.queue,
            role=QUEUE,
        )
        return dict(out) if isinstance(out, Mapping) else {}

    # -- review bundles (DESIGN.md section 13) -----------------------------------------------------

    def review_bundle(self, job: str, audience: str = "ours") -> dict[str, Any]:
        """A delivered job's review bundle: ``{ready: true, image_set, bundle_sha256, bundle_url,
        expires_ms, public}``, or ``{ready: false, state, retry_s}`` while the platform renders it
        (asking queues it). ``audience`` ``third_party`` is refused (403 ``not_public``) for a
        project that is not public."""
        out = self._call(
            "POST",
            f"/api/engine/einstein/jobs/{quote(job, safe='')}/review_bundle",
            {"audience": audience},
            base=self.queue,
            role=QUEUE,
        )
        return dict(out) if isinstance(out, Mapping) else {}

    def fetch_bundle(self, url: str, max_bytes: int) -> bytes:
        """A bundle's bytes from its signed link (no credential is sent: the link is the
        authority). Only a link on the platform's own origin is fetched."""
        origins = {self.base, self.queue}
        if not any(url.startswith(o.rstrip("/") + "/") for o in origins):
            raise PlatformError("the bundle link is not on the platform's origin", 0, "bad_link")
        req = urllib.request.Request(url, method="GET")  # noqa: S310 -- the platform's origin
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                data = r.read(max_bytes + 1)
        except urllib.error.HTTPError as e:
            raise PlatformError(f"bundle fetch: {e.code}", e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise PlatformError(f"platform unreachable: {e}") from None
        if len(data) > max_bytes:
            raise PlatformError("the bundle is larger than a bundle may be", 413, "too_large")
        return bytes(data)

    # -- the validator's own workspaces ------------------------------------------------------------

    def run(
        self,
        ws: str,
        command: str,
        args: Mapping[str, Any] | None = None,
        *,
        budget_s: float = 1800.0,
        on_run: Callable[[str], None] | None = None,
        stop: threading.Event | None = None,
    ) -> dict[str, Any]:
        """A `watt` command in one of the validator's workspaces, waited to its end -- or, still
        ``running``, at ``budget_s`` or as soon as ``stop`` is set (the caller decides whether to
        cancel it). ``on_run`` is told the run's id as soon as it is known. A failed poll is asked
        again (:meth:`_poll`) before it fails the call; the run goes on on the platform either way,
        and :meth:`attach` takes it up again."""
        end = time.monotonic() + budget_s
        out = dict(
            self._call(
                "POST",
                f"/api/agent/workspaces/{self._ws(ws)}/run",
                {"command": command, "args": dict(args or {}), "wait_s": _wait(end)},
            )
        )
        if out.get("state") == "running" and on_run is not None and out.get("run"):
            on_run(str(out["run"]))
        return self._follow(out, end, stop)

    def attach(
        self, run: str, *, budget_s: float = 1800.0, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        """Wait for a run this validator started earlier (a judgement retried after a failed poll,
        or after a restart) as :meth:`run` waits for its own. It is polled at least once, so a run
        that ended meanwhile answers its result. A run the platform no longer knows raises its
        404."""
        end = time.monotonic() + max(0.0, budget_s)
        return self._follow(self._poll(str(run), end, stop), end, stop)

    def _follow(
        self, out: dict[str, Any], end: float, stop: threading.Event | None
    ) -> dict[str, Any]:
        while out.get("state") == "running" and time.monotonic() < end:
            if stop is not None and stop.is_set():
                break
            out = self._poll(str(out["run"]), end, stop)
        return out

    def _poll(self, run: str, end: float, stop: threading.Event | None) -> dict[str, Any]:
        """One long-poll of a run. A poll that fails on the way (the platform unreachable, a 5xx, a
        408 or a 429) is asked again ``poll_retries`` times, ``poll_backoff_s`` doubling between
        them, before its error is raised: a network blip must not throw away a run that is still
        going. A stop set during a back-off answers the run as still running."""
        for i in range(self.poll_retries + 1):
            try:
                return dict(
                    self._call("GET", f"/api/agent/runs/{quote(run, safe='')}?wait_s={_wait(end)}")
                )
            except PlatformError as e:
                transient = e.status in (0, 408, 429) or e.status >= 500
                if i >= self.poll_retries or not transient:
                    raise
                self.log(
                    f"run {run}: a poll failed ({e.status} {e.code or 'no code'}); asked again "
                    f"({i + 1} of {self.poll_retries})"
                )
                delay = self.poll_backoff_s * 2**i
                if stop is not None:
                    if stop.wait(delay):
                        return {"state": "running", "run": run}
                elif delay > 0:
                    time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover -- the loop returns or raises

    def cancel_run(self, run: str) -> None:
        """Stop one of the validator's own runs (a guard run past its budget, or of a cancelled
        job); a run the platform no longer knows is already over."""
        try:
            self._call("POST", f"/api/agent/runs/{quote(run, safe='')}/cancel", {"wait_s": 10})
        except PlatformError as e:
            if e.status != 404:
                raise

    def export(self, ws: str) -> bytes:
        return bytes(
            self._call("GET", f"/api/agent/workspaces/{self._ws(ws)}/export", raw=True, timeout=320)
        )

    def delete(self, ws: str) -> None:
        try:
            self._call("DELETE", f"/api/agent/workspaces/{self._ws(ws)}")
        except PlatformError as e:
            if e.status != 404:
                raise
