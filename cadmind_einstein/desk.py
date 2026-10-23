"""The job desk (DESIGN.md sections 3-5): one miner per job; a job ends delivered, not finished,
declined, or "didn't finish" (out of reach, an older ending, is no longer reached).

A job's life:

1. **claimed** -- the validator pulled the job from the platform's single queue because it had a
   free admitted miner (public, free-plan, or opted-in briefs only; no other work is ever made).
   It holds a lease on it, renewed while it works; reconciled from the platform after a restart.
2. **assigned** -- the router chose one available miner (section 6) and the platform made a job
   workspace granted to that miner's key, from the brief (or the job's seed, for a later round of a
   client's project). The miner sees it on ``work`` and must ``accept`` within ``accept_s``.
3. **working** -- accepted; the deadline is the effort's (low 30, medium 60, high 120, max 180
   minutes).
4. **judging** -- the miner submitted (it is free for another job from then on), or its deadline
   passed: the workspace is frozen, the
   validator forks it and gates the fork (:meth:`Desk._gate`): the miner's ``watt status`` must
   say deliverable, then the validator runs the mutation guard itself (``watt test --mutate`` on
   its fork, the operator's lane, which draws its own sample), and the guard record ``watt status``
   then reads must be that run's. A miner's own record is one draw it may have re-rolled until a
   weak design passed; the validator's draw is one it cannot know.
5. **done** -- a passing attempt is checked for copying, scored and delivered; the client's
reaction scores it further.

Instead of a project the agent may **reject** the assignment (``not-allowed`` or ``not-capable``,
from the published scope guide) with a message for the client.

**An attempt the miner worked that does not pass** -- a gate failure, or a late attempt that fails
-- ends the round **not finished** (DESIGN.md "Continuing an unfinished round"): its project, the
validator's fork, is kept and named in the result with the builder's own summary of what blocks
delivery (``submit``'s ``summary``) or, without one, the gate's stage and error. Nothing is charged;
the client may pay for a round that continues it (a claim with ``continuation``: its seed is the
unfinished project, its base the last delivered version or the brief, and the builder may start
over from the base). **Any other attempt that does not pass** -- not accepted in time, a copy, a
rejection -- goes to ANOTHER miner, **from scratch** (the brief or the seed only). Scoring:

- a timeout or a copy scores 0 at once;
- a rejection or a gate failure is held: 0 if another miner then passes the same job; at an
  undelivered ending it counts (0) unless the opinions came from two owners (below);
- two rejections end the job **declined** (the client reads the messages, the brief is flagged for
  review); four attempts without a worked one end it **didn't finish**. None is charged.

**Independent opinions.** "Nobody could do it" takes miners of different owners: a job a miner
rejected or failed goes next to a miner of ANOTHER coldkey (``SecondOpinion.wait_s`` later, any
other miner: its opinion then is not a second one). Rejections and failures end a job declined or
out of reach only when they come from ``SecondOpinion.coldkeys`` distinct coldkeys; and when a job
ends undelivered with its held failures all from fewer coldkeys, they count (0) instead of being
dropped. So hotkeys of one owner cannot end a hard job for free between them. The rejection
telemetry is also summed per coldkey (:meth:`enforce`).

**Concurrency.** The desk serves up to ``max_inflight`` jobs at once, one miner each. Every slow
platform call -- granting a job workspace, judging (freeze, fork, ``watt status``, export),
returning results, revoking keys -- runs in a bounded worker pool, so ``tick`` never waits on one.
The lease is renewed by its own heartbeat thread (:meth:`Desk.start`), so a slow claim or a slow
grant never lets a lease lapse.

**Durability.** With a :class:`~.store.Store` every mutation is one SQLite transaction (the ledger's
writes included). A restart reloads jobs, attempts, held failures, miners, admission, telemetry,
the copy window and the results still to send, then recovers what was in flight: a grant cut off
mid-call goes back to the queue, an attempt being worked keeps its workspace and its absolute
deadline, and an attempt being judged is judged again (its workspace is frozen; judging reads it).

**Abuse.** :meth:`enforce` (the daemon calls it every tick) disables a miner -- its key revoked
the platform's way, its admission dropped, its job sent to another miner unscored, no job until the
block lifts -- on what :class:`AbusePolicy` counts: copies, Watt suspensions (the platform's
``agent_suspended``), failed entry tests, and the rejection pattern :meth:`telemetry` flags. Each
count acts once (a strike); a later strike doubles the block. The subnet owner's and the
validator's hotkeys are never disabled.

**Registration.** Only hotkeys registered on the subnet get a key or a job
(:meth:`sync_registered`, after each metagraph read). A hotkey that deregisters loses its key (the
platform's operator route; retried until the platform takes it), its admission, and its job, which
goes to another miner without scoring anyone. The subnet owner's and the validator's own
hotkeys never mine.

**Onboarding** (``key_approval="manual"``, the validator's default). A registered hotkey gets its
agent key only once the operator approved it (:meth:`approve`); until then its asks are recorded as
pending (:class:`KeyRequest`, store namespace ``approval``). The operator may refuse a request or
revoke a key (:meth:`revoke`: key deleted, admission dropped, its job reassigned unscored). An
approved key holder passes the entry test once (:meth:`admission`) before any job or weight.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import secrets
import statistics
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .answers import check as answers_check
from .platform import PlatformError
from .scoring import JobOutcome, Ledger, size_class
from .sealing import seal, valid_public
from .similarity import CopyCheck, CopyPolicy, Entry, fingerprint
from .store import Store
from .style import check as style_check

SUSPENDED = "agent_suspended"
"""The platform's code for a miner Watt's telemetry suspended (its key deleted, none minted)."""
DEADLINE_S = {"low": 30 * 60, "medium": 60 * 60, "high": 120 * 60, "max": 180 * 60}
ALIVE_S = 60.0
EXPLORE = 0.10
THIN_JOBS = 5
DONE_KEEP_MS = 7 * 24 * 3_600_000
"""how long a finished job stays queryable (and in the store)"""
CATEGORIES = ("not-allowed", "not-capable", "needs-client")
"""What an agent may reject a job as (:meth:`Desk.reject`)."""
SUBSTANTIVE = ("reject", "gate")
"""attempt kinds that are an opinion on the job (it cannot or should not be done)"""
UNCOUNTED = ("dereg", "disabled", "revoked", "stale", "ours", "lost")
"""attempt kinds that do not count toward a job's attempt cap and score nobody: the miner left the
subnet, was disabled for abuse, or had its key revoked by the operator while it held the job
(``dereg``, ``disabled``, ``revoked``); the first
assignment after a cancel was not accepted in time because the miner was still stopping the
cancelled job (``stale``); the validator could not judge it, on its own side (``ours``); the
platform took the job back when the validator's lease lapsed (``lost``: its miner may take it
again when it is claimed again)"""
REFUSED_DELIVERY = ("delivery_not_of_job", "delivery_digest_mismatch")
"""the platform's refusals of a delivered result whose provenance it cannot accept (the workspace
does not descend from the job's own grant, or is not at the digest the validator gated): a
security ALERT, never sent again, and the job's scores withdrawn -- the validator's side, not the
miner's (see :meth:`Desk._refused_delivery`)"""
OURS_BUSY = (
    "run_in_progress",
    "busy",
    "backoff",
    "account_agent_quota",
    "compute_limit_day",
    "rate_limited",
    "rate_limit_per_min",
    "account_rate_limited",
    "too_many_in_flight",
    "workspace_limit",
    "storage_limit",
    "operator_frozen",
    "operator_ceiling",
)
"""platform (or Watt) refusals of the VALIDATOR's own judging calls -- its key already running a
command, Watt busy, its rate, its quotas on the validator's own workspaces: our side, so the
judgement is retried, never counted against the miner"""
GATE_RUN_S = 1800.0
"""the default budget of one validator guard run (``watt report --mutate``): past it the validator
stops the run (freeing its lane for other judgements) and counts a gate timeout"""
GATE_TIMEOUTS = 2
"""gate timeouts (the budget, or Watt's own wall-clock stop) that fail an attempt; fewer are judged
again later, since one slow run may be load on our side"""
JUDGE_MAX_RETRIES = 20
JUDGE_GIVE_UP_S = 4 * 3600.0
"""a judgement retried on our side this many times, or for this long since its first retry, ends the
job "didn't finish": not charged, nobody scored"""
RETRY_FIRST_S = 15.0
RETRY_BUSY_FIRST_S = 5.0
RETRY_MAX_S = 600.0
CANCEL_COOLOFF_S = 45.0
"""how long a miner whose job was cancelled gets no new job (time to stop its agent)"""
MINT_PER_HOUR = 40
"""agent keys the validator mints per hour in all: below the platform's own ceiling, past which the
platform freezes the validator's key role"""
MINT_PER_COLDKEY_HOUR = 12
MINT_FRESH_S = 600.0
KEY_APPROVAL = ("manual", "auto")
"""How a registered hotkey gets its agent key (:meth:`Desk.agent_key`): ``manual`` -- only once the
operator approved it (:meth:`Desk.approve`), its asks recorded as pending until then -- or ``auto``,
any registered hotkey (the self-serve rule before 0.4.8)."""
PENDING_RETRY_S = 900
"""how long a miner whose key request is pending is told to wait before asking again"""
NOTE_MAX = 300
"""the miner's contact note on a key request, at most (characters)"""
REASON_MAX = 500
"""an operator's reason for a refusal or a revocation, at most (characters)"""
PENDING_MESSAGE = (
    "Your request for an agent key is recorded and waits for the subnet team's approval. Ask "
    "again later with the same call: once approved, it answers your key. Then pass the entry "
    "test once (POST /v1/miners/admission) before any job or weight."
)
"""what a miner whose key request is pending reads (the validator's ``--pending-message-file``)"""
ADMISSION_EFFORT = "high"
"""the entry test's effort, and so its deadline (``DEADLINE_S``)"""
"""a hotkey whose live key was minted less than this ago gets no new one"""


class _GateRetryError(Exception):
    """The validator's own guard run did not end on its own (still going, an error of the bench,
    stopped by something other than its limits): our side, so the judgement is retried."""


class _GateTimeoutError(Exception):
    """The validator's own guard run hit its budget (stopped by the validator) or Watt's wall-clock
    limit: counted on the attempt; judged again, or failed at ``GATE_TIMEOUTS``."""


@dataclass(frozen=True)
class SecondOpinion:
    """When a job's failures stand as "nobody could do it" (DESIGN.md section 4)."""

    coldkeys: int = 2
    """distinct coldkeys whose rejections or gate failures end a job declined or out of reach, and
    that must have tried an undelivered job for its held failures to be dropped; 1 restores the
    per-hotkey rule"""
    wait_s: float = 1800.0
    """how long a job a miner rejected or failed waits for a miner of another coldkey before any
    other miner may take it"""


ENDINGS = {"declined": "declined", "out of reach": "out_of_reach", "not finished": "not_finished"}
"""the undelivered endings ``watch`` counts per coldkey (observability only)"""
WATCH_PAIRS = 20_000
"""copy comparisons kept for the hourly report (the oldest dropped past it, and counted)"""


def job_event(event: str, job: str, **fields: Any) -> str:
    """One structured log line for one job event: ``job-event`` and a JSON object (``event``,
    ``job``, then the fields that are set). Callers pass ids, kinds, categories and counts only --
    never a key, a brief or a message the client reads."""
    row: dict[str, Any] = {"event": event, "job": job}
    row.update({k: v for k, v in fields.items() if v is not None and v != ""})
    return "job-event " + json.dumps(row, separators=(",", ":"), default=str)


_NAMED_VERDICTS = ("unguarded", "unproven", "read", "excused")
"""The guard verdicts of a must that refuse it, in the order the verdict's words name them:
``unguarded`` (a survivor its tests missed), ``unproven`` (refused only by rules not about it,
W0825), ``read`` (its mutants only read by a measure) and ``excused`` (only harmless or equivalent
mutants reached it)."""


def _survivors(guard: Any) -> str:
    """The must requirements a refused guard run names unguarded, unproven, read or excused, for the
    verdict's words: the unguarded ones from its ``survivors``, every one from its per-requirement
    ``requirements`` verdicts (a guard older than those carries only ``survivors``). The words only
    name the musts; the refusal itself is the run's, never this."""
    if not isinstance(guard, dict):
        return ""
    named: dict[str, set[str]] = {k: set() for k in _NAMED_VERDICTS}
    for s in guard.get("survivors") or []:
        if isinstance(s, dict):
            named["unguarded"].update(str(r) for r in s.get("requirements") or [])
    for v in guard.get("requirements") or []:
        if isinstance(v, dict) and v.get("status") in named and v.get("requirement"):
            named[str(v["status"])].add(str(v["requirement"]))
    words = [
        f"{k} {', '.join('R.' + r for r in sorted(named[k]))}" for k in _NAMED_VERDICTS if named[k]
    ]
    return f": {'; '.join(words)}" if words else ""


@dataclass(frozen=True)
class AbusePolicy:
    """When :meth:`Desk.enforce` disables a miner (DESIGN.md sections 4 and 11). A count of 0 (or
    ``rejections=False``) turns that rule off."""

    copies: int = 2
    """outputs judged copies of another hotkey's"""
    suspensions: int = 1
    """Watt's telemetry suspended the miner's key (subset violations, secrecy refusals, quotas)"""
    admission_failures: int = 3
    """entry tests that did not pass the gate (one a day at most)"""
    rejections: bool = True
    """the rejection telemetry's flag: ``rejections_min`` rejections at over ``rejection_ratio``
    times the fleet's rate, or ``overturned`` rejections a miner of another coldkey then passed (a
    ``needs-client`` rejection is never overturned: :meth:`Desk._overturns`)"""
    rejections_min: int = 5
    rejection_ratio: float = 2.0
    overturned: int = 3
    disable_s: float = 86_400.0
    """the first block; each later strike doubles it, up to ``max_disable_s``"""
    max_disable_s: float = 30 * 86_400.0


class Platform(Protocol):
    """What the desk uses of :class:`~.platform.OperatorClient` (the contract lives there)."""

    def mint_key(self, hotkey: str) -> dict[str, Any]: ...
    def disable_key(self, hotkey: str) -> dict[str, Any]: ...
    def grant(self, hotkey: str, *, brief: str = "", seed: str = "", name: str = "job") -> str: ...
    def grant_continuation(
        self, hotkey: str, *, seed: str, base: str = "", name: str = "job"
    ) -> tuple[str, str]: ...
    def freeze(self, ws: str) -> dict[str, Any]: ...
    def fork(self, ws: str) -> str: ...
    def run(
        self,
        ws: str,
        command: str,
        args: Any = None,
        *,
        budget_s: float = 1800.0,
        on_run: Callable[[str], None] | None = None,
        stop: threading.Event | None = None,
    ) -> dict[str, Any]: ...
    def attach(
        self, run: str, *, budget_s: float = 1800.0, stop: threading.Event | None = None
    ) -> dict[str, Any]: ...
    def cancel_run(self, run: str) -> None: ...
    def export(self, ws: str) -> bytes: ...
    def delete(self, ws: str) -> None: ...
    def claim(self) -> dict[str, Any] | None: ...
    def lease(self, jobs: list[str], working: list[str] | None = None) -> dict[str, Any]: ...
    def claimed(self) -> list[dict[str, Any]]: ...
    def complete(self, job: str, result: dict[str, Any]) -> Any: ...


class DeskError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Attempt:
    hotkey: str
    workspace: str
    assigned_ms: int
    accept_by_ms: int
    accepted_ms: int = 0
    deadline_ms: int = 0
    submitted_ms: int = 0
    digest: str = ""
    outcome: str = ""
    """'' while open; then why it ended, in words"""
    kind: str = ""
    """'' while open; pass | reject | gate | timeout | copy"""
    category: str = ""
    message: str = ""
    """a rejection's category (not-allowed | not-capable | needs-client) and the agent's message to
    the client"""
    fork: str = ""
    """the validator's fork being gated, kept across a judgement retried on our side (its guard
    outcomes and build are reused, and no second fork is made)"""
    gate_run: str = ""
    """the validator's own guard run on the fork, while it runs (stopped on a cancel); a judgement
    retried on our side (a failed poll, a restart) takes this run up again instead of starting
    another"""
    gate_digest: str = ""
    """the digest the miner's record said deliverable when ``gate_run`` was started (a run taken up
    again is checked against it: the fork's status cannot be read while the run holds the fork)"""
    gate_started_ms: int = 0
    """when ``gate_run`` started: a run taken up again keeps what is left of its budget"""
    gate_timeouts: int = 0
    retries: int = 0
    """judgements retried on our side"""
    last_retry: str = ""
    """why the last judgement was retried on our side (kept when the attempt is then judged)"""
    first_retry_ms: int = 0
    reference: str = ""
    """a continuation's base, a read-only workspace the platform granted beside this one (the last
    delivered version), from which the builder may start over; '' when the base is the brief"""
    summary: str = ""
    """the builder's own words on what blocks delivery, given with its submit (optional)"""


@dataclass
class Job:
    job: str
    brief: str
    effort: str = "medium"
    privacy: str = "public"
    seed: str = ""
    prefer: str = ""
    exclude: list[str] = field(default_factory=list)
    state: str = "queued"
    attempts: list[Attempt] = field(default_factory=list)
    created_ms: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    kind: str = "client"
    """client | admission (one miner's entry test: assigned to it alone, never scored)"""
    target: str = ""
    queued_ms: int = 0
    pending: list[JobOutcome] = field(default_factory=list)
    """failed attempts (rejections, gate failures) whose 0 counts only if another miner passes"""
    no_questions: bool = False
    """the client asked not to be asked questions (an API job always does): the builder decides
    every would-be question itself, and the gate refuses a delivery with a client decision open"""
    subprojects: bool | None = None
    """whether the client allows splitting the device into sub-projects (the platform's
    ``subprojects``); None when the platform did not say. The workspace's brief/job.toml carries it,
    and Watt refuses a sub-project when it is false (W0233), so the gate's ``watt status`` does"""
    continuation: dict[str, Any] = field(default_factory=dict)
    """a round that continues an unfinished one (DESIGN.md "Continuing an unfinished round"): its
    seed is the unfinished project and ``base`` the operator's workspace of the project's last
    delivered version ('' when there is none: the base is the brief). Empty on every other job."""
    style: dict[str, Any] = field(default_factory=dict)
    """a style round (cadmind-watt ADR-020): ``baseline``, the settled contract the round began
    from, as the platform's ``watt style begin`` answered it; the gate refuses a delivery that did
    not keep it (:mod:`.style`). Empty on every other job."""

    @property
    def current(self) -> Attempt | None:
        return (
            self.attempts[-1] if self.attempts and self.state in ("assigned", "working") else None
        )


def _style_of(value: Any) -> dict[str, Any]:
    """A claim's ``style`` (a style round's baseline), kept when it names a contract."""
    if not isinstance(value, Mapping):
        return {}
    baseline = value.get("baseline")
    if not isinstance(baseline, Mapping) or not baseline.get("contract"):
        return {}
    return {"baseline": dict(baseline)}


def _continuation_of(value: Any) -> dict[str, Any]:
    """A claim's ``continuation`` (a round continuing an unfinished one): ``{"base": ws}``, the base
    '' when the project has no delivered version. Empty when the claim carries none."""
    if not isinstance(value, Mapping):
        return {}
    return {"base": str(value.get("base") or "")[:100]}


SUMMARY_MAX = 2000
"""the longest builder summary kept (characters, whitespace collapsed)"""


def _gate_summary(kind: str, verdict: Mapping[str, Any]) -> str:
    """What blocks delivery, in a sentence, when the builder said nothing: Watt's stage and the
    gate's error, as the validator read them."""
    stage = str(verdict.get("stage") or "")
    error = " ".join(str(verdict.get("error") or "").split())
    head = (
        "The builder's time ran out before the project passed Watt's checks"
        if kind == "timeout"
        else "The project the builder handed over did not pass Watt's checks"
    )
    where = f" (it stopped at Watt's {stage} stage)" if stage and stage != "none" else ""
    detail = error[:1200]
    return f"{head}{where}." + (f" {detail[:1].upper()}{detail[1:]}" if detail else "")


def _flag(value: object) -> bool | None:
    """A platform boolean the job may not carry: True or False as sent, None otherwise."""
    return value if isinstance(value, bool) else None


def _job_from(row: Mapping[str, Any]) -> Job:
    r = dict(row)
    r["attempts"] = [Attempt(**a) for a in r.get("attempts") or []]
    r["pending"] = [
        JobOutcome(**{k: v for k, v in o.items() if k not in ("s", "parts")})
        for o in r.get("pending") or []
    ]
    return Job(**r)


@dataclass
class Miner:
    hotkey: str
    key_id: str = ""
    last_seen_ms: int = 0
    job: str = ""
    blocked_until_ms: int = 0
    admitted: bool = False
    admission_ms: int = 0
    cancelled_job: str = ""
    """its last job, cancelled by the client: said on ``work`` until it accepts another job"""
    cancelled_ms: int = 0
    after_cancel: bool = False
    """its next assignment is the first since a cancel: not accepting it in time scores nothing"""
    minted_ms: int = 0


@dataclass
class KeyRequest:
    """A hotkey's standing for its agent key under manual approval (``key_approval="manual"``): its
    asks, recorded while pending, and the operator's decision. Persisted (store namespace
    ``approval``, one row per hotkey)."""

    hotkey: str
    state: str = "pending"
    """pending | approved | refused | revoked"""
    coldkey: str = ""
    """its coldkey at its last ask (from the metagraph; '' when unknown)"""
    first_ms: int = 0
    last_ms: int = 0
    """its first and last ask"""
    asks: int = 0
    ip: str = ""
    """the client address of its last ask"""
    note: str = ""
    """the miner's own contact or note, from its last ask that gave one"""
    decided_ms: int = 0
    reason: str = ""
    """a refusal's or a revocation's reason, said to the miner on its next ask"""
    approved_coldkey: str = ""
    """the coldkey the approval was given to: a hotkey under another coldkey asks anew"""
    by: str = ""
    """who decided: operator ('' while pending)"""


def _clean(text: Any, limit: int) -> str:
    """One line of printable text, at most ``limit`` characters (a note or a reason)."""
    words = " ".join(str(text or "").split())
    return "".join(ch for ch in words if ch.isprintable())[:limit]


ADMISSION_BRIEF = """Entry test to the einstein subnet. There is no client: nobody answers
questions, so decide every open question yourself and record each decision with its reason.

The device: a small stand that holds a phone upright on a desk, in portrait or in landscape, with a
way through for its charging cable. It must not tip over when the phone is tapped, and the phone
must come out with one hand.

Choose the sizes, the material and the way it is made, and state where each figure comes from.
Make the project deliverable -- `watt status` deliverable, every must proven -- and submit it before
the deadline. The validator then runs its own guard on it, as on a client's job. The test is not
scored.
"""


class Desk:
    def __init__(
        self,
        platform: Platform,
        ledger: Ledger,
        *,
        registered: Callable[[], Iterable[str]],
        accept_s: float = 120.0,
        admission_brief: str = ADMISSION_BRIEF,
        admission_effort: str = ADMISSION_EFFORT,
        key_approval: str = "auto",
        pending_message: str = PENDING_MESSAGE,
        deadlines: Mapping[str, int] | None = None,
        max_attempts: int = 4,
        max_inflight: int = 8,
        lease_every_s: float = 30.0,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
        store: Store | None = None,
        workers: int = 0,
        excluded: Iterable[str] = (),
        abuse: AbusePolicy | None = None,
        log: Callable[[str], None] | None = None,
        coldkeys: Callable[[], Mapping[str, str]] | None = None,
        copy_policy: CopyPolicy | None = None,
        copy_corpus: frozenset[bytes] = frozenset(),
        second_opinion: SecondOpinion | None = None,
        cancel_cooloff_s: float = CANCEL_COOLOFF_S,
        gate_run_s: float = GATE_RUN_S,
        judge_max_retries: int = JUDGE_MAX_RETRIES,
        judge_give_up_s: float = JUDGE_GIVE_UP_S,
        mint_per_hour: int = MINT_PER_HOUR,
        mint_per_coldkey_hour: int = MINT_PER_COLDKEY_HOUR,
        mint_fresh_s: float = MINT_FRESH_S,
        on_delivered: Callable[[str, str], None] | None = None,
    ) -> None:
        self.platform, self.ledger, self.registered = platform, ledger, registered
        self.on_delivered = on_delivered
        """told (job, builder's hotkey) once the platform took a delivered, charged result: the
        reviewer's queue (:mod:`.votes`)"""
        self.cancel_cooloff_s, self.gate_run_s = cancel_cooloff_s, gate_run_s
        self.judge_max_retries, self.judge_give_up_s = judge_max_retries, judge_give_up_s
        self.mint_per_hour, self.mint_per_coldkey_hour = mint_per_hour, mint_per_coldkey_hour
        self.mint_fresh_s = mint_fresh_s
        self._mints: list[tuple[int, str]] = []
        """(at_ms, coldkey) of the mints of the last hour (the validator's own budget)"""
        self._gate_stop: dict[str, threading.Event] = {}
        """per job being judged: set to stop its guard run (a cancel)"""
        self.abuse = abuse or AbusePolicy()
        self.coldkeys = coldkeys or dict
        """hotkey -> coldkey, from the last metagraph read (a hotkey with none is its own)"""
        self.second_opinion = second_opinion or SecondOpinion()
        self.log = log or (lambda _line: None)
        self.admission_brief = admission_brief
        self.accept_s, self.deadlines = accept_s, dict(deadlines or DEADLINE_S)
        if admission_effort not in self.deadlines:
            raise ValueError(f"admission_effort is one of {tuple(self.deadlines)}")
        self.admission_effort = admission_effort
        if key_approval not in KEY_APPROVAL:
            raise ValueError(f"key_approval is one of {KEY_APPROVAL}")
        self.key_approval = key_approval
        message = " ".join(str(pending_message or "").split())[:2000]
        self.pending_message = message or PENDING_MESSAGE
        self.launch_equal = False
        """the launch rule (equal weight per admitted hotkey) is on: set and saved by the daemon's
        ``--launch-equal on|off`` or the operator's route (:meth:`set_launch_equal`), and kept
        across restarts otherwise"""
        self.launch_saved = False
        """whether the state held a launch switch when this desk loaded it"""
        self.approvals: dict[str, KeyRequest] = {}
        """hotkey -> its key request and the operator's decision (manual approval)"""
        self.max_attempts, self.max_inflight = max_attempts, max_inflight
        self.lease_every_s, self._leased_ms = lease_every_s, 0
        self._lease_lock = threading.Lock()
        self._outbox: list[str] = []
        self._revoke: list[str] = []
        """deregistered or disabled hotkeys whose key the platform has not yet deleted"""
        self.rng = rng or random.Random()  # noqa: S311 -- routing, not a secret
        self.clock = clock
        self.excluded = {h for h in excluded if h}
        self.jobs: dict[str, Job] = {}
        self.miners: dict[str, Miner] = {}
        self.copies = CopyCheck(policy=copy_policy or CopyPolicy(), corpus=copy_corpus)
        self.assigned: dict[str, int] = {}
        self.rejected: dict[str, int] = {}
        self.overturned: dict[str, int] = {}
        self.copied: dict[str, int] = {}
        self.failed: dict[str, int] = {}
        """gate failures per hotkey (held or counted), for the per-coldkey watch"""
        self.delivered: dict[str, int] = {}
        """delivered (scored, passing) client jobs per hotkey, ever: what genesis weights"""
        self.not_finished: dict[str, int] = {}
        """rounds a hotkey worked that ended not finished, ever (the miners' stats)"""
        self.suspensions: dict[str, int] = {}
        self.admission_failed: dict[str, int] = {}
        self.strikes: dict[str, int] = {}
        self.abuse_seen: dict[str, dict[str, int]] = {}
        """per hotkey, the counts its last strike was taken at (each count acts once)"""
        self.disabled_log: list[dict[str, Any]] = []
        """disables since the last hourly report"""
        self.delivery_refusals: list[dict[str, Any]] = []
        """delivered results the platform refused on provenance, since the last hourly report"""
        self.watch_pairs: list[dict[str, Any]] = []
        """copy comparisons since the last hourly report (observability: threshold calibration)"""
        self.watch_dropped = 0
        self._lock = threading.RLock()
        self.store = store
        self._saved: dict[tuple[str, str], str] = {}
        self._final: set[str] = set()
        self._copy_seq = 0
        self.pool = ThreadPoolExecutor(
            max_workers=workers or 2 * max_inflight + 2, thread_name_prefix="desk"
        )
        self._futures: set[Future[Any]] = set()
        self._flushing = threading.Lock()
        self._judging: set[str] = set()
        self._retry_at: dict[str, float] = {}
        self._stop = threading.Event()
        self._heart: threading.Thread | None = None
        if store is not None:
            self._load()

    # -- durability --------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _mut(self) -> Iterator[None]:
        """One desk mutation: under the lock, in one store transaction with the ledger's writes,
        persisted at its end."""
        with self._lock:
            if self.store is None:
                yield
                return
            with self.store.tx():
                yield
                self._persist()

    def _rows(self) -> Iterator[tuple[str, str, Any]]:
        for j in self.jobs.values():
            if j.state == "done" and j.job in self._final:
                yield "job", j.job, None  # a finished job never changes: not serialized again
                continue
            yield "job", j.job, asdict(j)
        for m in self.miners.values():
            row = asdict(m)
            row["last_seen_ms"] = m.last_seen_ms // 30_000 * 30_000  # a poll is not a write
            yield "miner", m.hotkey, row
        for r in self.approvals.values():
            yield "approval", r.hotkey, asdict(r)
        yield (
            "meta",
            "desk",
            {
                "assigned": self.assigned,
                "rejected": self.rejected,
                "overturned": self.overturned,
                "outbox": self._outbox,
                "revoke": self._revoke,
                "copied": self.copied,
                "failed": self.failed,
                "delivered": self.delivered,
                "not_finished": self.not_finished,
                "suspensions": self.suspensions,
                "admission_failed": self.admission_failed,
                "strikes": self.strikes,
                "abuse_seen": self.abuse_seen,
                "disabled_log": self.disabled_log,
                "delivery_refusals": self.delivery_refusals,
            },
        )
        yield "meta", "watch", {"pairs": self.watch_pairs, "dropped": self.watch_dropped}
        yield "meta", "launch", {"launch_equal": self.launch_equal}

    def _persist(self) -> None:
        """Write the rows that changed since the last write (called inside the transaction)."""
        assert self.store is not None  # noqa: S101 -- only with a store
        live: set[tuple[str, str]] = set()
        for ns, k, row in self._rows():
            live.add((ns, k))
            if row is None:
                continue
            text = json.dumps(row, sort_keys=True)
            if self._saved.get((ns, k)) != text:
                self.store.put_raw(ns, k, text)
                self._saved[(ns, k)] = text
            if ns == "job" and row["state"] == "done":
                self._final.add(k)
        for ns, k in [key for key in self._saved if key not in live]:
            self.store.delete(ns, k)
            del self._saved[(ns, k)]
            self._final.discard(k)

    def _load(self) -> None:
        assert self.store is not None  # noqa: S101
        for k, row in self.store.items("job").items():
            self.jobs[k] = _job_from(row)
            self._saved[("job", k)] = json.dumps(row, sort_keys=True)
            if row.get("state") == "done":
                self._final.add(k)
        for k, row in self.store.items("miner").items():
            self.miners[k] = Miner(**row)
            self._saved[("miner", k)] = json.dumps(row, sort_keys=True)
        for k, row in self.store.items("approval").items():
            self.approvals[k] = KeyRequest(**row)
            self._saved[("approval", k)] = json.dumps(row, sort_keys=True)
        meta = self.store.get("meta", "desk") or {}
        self.assigned = dict(meta.get("assigned") or {})
        self.rejected = dict(meta.get("rejected") or {})
        self.overturned = dict(meta.get("overturned") or {})
        self._outbox = list(meta.get("outbox") or [])
        self._revoke = list(meta.get("revoke") or [])
        self.copied = dict(meta.get("copied") or {})
        self.failed = dict(meta.get("failed") or {})
        self.delivered = dict(meta.get("delivered") or {})
        self.not_finished = dict(meta.get("not_finished") or {})
        self.suspensions = dict(meta.get("suspensions") or {})
        self.admission_failed = dict(meta.get("admission_failed") or {})
        self.strikes = dict(meta.get("strikes") or {})
        self.abuse_seen = {k: dict(v) for k, v in (meta.get("abuse_seen") or {}).items()}
        self.disabled_log = list(meta.get("disabled_log") or [])
        self.delivery_refusals = list(meta.get("delivery_refusals") or [])
        launch = self.store.get("meta", "launch")
        self.launch_saved = launch is not None
        self.launch_equal = (launch or {}).get("launch_equal") is True
        watch = self.store.get("meta", "watch") or {}
        self.watch_pairs = list(watch.get("pairs") or [])
        self.watch_dropped = int(watch.get("dropped") or 0)
        copies = sorted(self.store.items("copy").items(), key=lambda kv: int(kv[0]))
        for k, row in copies:
            job, hk, digest, sh = row[:4]
            cold, was_copy = (str(row[4]), bool(row[5])) if len(row) >= 6 else ("", False)
            feats = frozenset(bytes.fromhex(x) for x in sh)
            self.copies.remember(Entry(job, hk, digest, feats, cold, copy=was_copy))
            self._copy_seq = max(self._copy_seq, int(k))
        self._recover()

    def _recover(self) -> None:
        """After a restart: a grant cut off mid-call goes back to the queue (its miner freed); a
        working attempt keeps its workspace and deadline; a judged attempt is judged again on
        :meth:`start`; every miner's job is re-derived from the jobs. A submitted attempt being
        judged holds no miner (it was freed at its submit); one judged at its deadline holds its
        miner until the verdict, as before the restart."""
        with self._mut():
            for m in self.miners.values():
                m.job = ""
            for job in self.jobs.values():
                if job.state == "assigning":
                    job.state = "queued"
                elif job.state in ("assigned", "working", "judging") and job.attempts:
                    last = job.attempts[-1]
                    if job.state == "judging" and last.submitted_ms:
                        continue
                    holder = self.miners.get(last.hotkey)
                    if holder is not None:
                        holder.job = job.job
            if self.key_approval == "manual":
                self._unapproved_out()

    def _unapproved_out(self) -> None:
        """Inside a mutation, under manual approval: every hotkey holding a key or an admission
        it was never approved for (one keyed under the self-serve rule) loses both -- its key
        deleted on the platform, its job reassigned unscored, as a revocation -- and goes back to
        pending: it asks, is approved and passes the entry test like anyone else."""
        out = []
        for m in self.miners.values():
            if not (m.key_id or m.admitted) or self.approved(m.hotkey):
                continue
            out.append(m.hotkey)
            m.key_id, m.admitted, m.admission_ms, m.minted_ms = "", False, 0, 0
            self._free_job(m, "manual approval: the key was never approved", "revoked")
            self._queue_revoke(m.hotkey)
            r = self.approvals.get(m.hotkey)
            if r is None:
                self.approvals[m.hotkey] = KeyRequest(m.hotkey, first_ms=self.now_ms())
            elif r.state not in ("refused", "revoked"):
                r.state, r.decided_ms, r.reason, r.by = "pending", 0, "", ""
        if out:
            self.log(
                f"manual approval: {len(out)} hotkey(s) held a key never approved: revoked, back "
                "to pending"
            )

    def _remember_copy(self, e: Entry) -> None:
        if self.store is None:
            return
        self._copy_seq += 1
        self.store.put(
            "copy",
            f"{self._copy_seq:012d}",
            [e.job, e.hotkey, e.digest, [x.hex() for x in e.features], e.coldkey, e.copy],
        )
        old = self._copy_seq - max(1, self.copies.policy.window)
        if old > 0:
            self.store.delete("copy", f"{old:012d}")

    def _cmap(self) -> Mapping[str, str]:
        try:
            return self.coldkeys() or {}
        except Exception:  # noqa: BLE001 -- a failed read: every hotkey stands alone
            return {}

    def coldkey(self, hotkey: str, cmap: Mapping[str, str] | None = None) -> str:
        """The hotkey's coldkey from the last metagraph read; a hotkey with none stands alone."""
        ck = str((self._cmap() if cmap is None else cmap).get(hotkey) or "")
        return ck or f"hotkey:{hotkey}"

    def proven(self) -> list[str]:
        """Hotkeys that delivered at least one scored client job (genesis weights only these)."""
        with self._lock:
            return sorted(h for h, n in self.delivered.items() if n > 0)

    # -- the worker pool and the heartbeat ---------------------------------------------------------

    def _spawn(self, fn: Callable[..., Any], *args: Any) -> None:
        fut = self.pool.submit(fn, *args)
        with self._lock:
            self._futures.add(fut)
        fut.add_done_callback(self._done)

    def _done(self, fut: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(fut)
        if not fut.cancelled() and fut.exception() is not None:
            err = fut.exception()
            logging.getLogger("cadmind_einstein.desk").error(
                "desk worker: %s: %s", type(err).__name__, err
            )

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Wait until no worker task is running (tests, shutdown)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if not self._futures:
                    return True
            time.sleep(0.01)
        return False

    def start(self) -> None:
        """The lease heartbeat, and the judging an earlier process left unfinished."""
        with self._lock:
            again = [
                (j, j.attempts[-1])
                for j in self.jobs.values()
                if j.state == "judging" and j.attempts and j.job not in self._judging
            ]
        for job, a in again:
            self._start_judge(job, a)
        if self._heart is None:
            self._heart = threading.Thread(target=self._beat, name="lease", daemon=True)
            self._heart.start()

    def stop(self) -> None:
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _beat(self) -> None:
        while not self._stop.wait(min(5.0, self.lease_every_s)):
            with contextlib.suppress(Exception):
                self._lease(self.now_ms())

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    # -- miners ------------------------------------------------------------------------------------

    def _miner(self, hotkey: str) -> Miner:
        if hotkey not in set(self.registered()):
            raise DeskError("hotkey is not registered on the subnet", 403)
        if hotkey in self.excluded:
            raise DeskError("the subnet owner's and the validator's hotkeys do not mine", 403)
        m = self.miners.setdefault(hotkey, Miner(hotkey))
        m.last_seen_ms = self.now_ms()
        return m

    def _real_coldkey(self, hotkey: str) -> str:
        """The hotkey's coldkey from the last metagraph read, '' when unknown."""
        return str(self._cmap().get(hotkey) or "")

    def approved(self, hotkey: str) -> bool:
        """Whether ``hotkey`` may hold an agent key: always under ``auto``; under ``manual`` only
        once the operator approved it (and not since refused or revoked)."""
        if self.key_approval == "auto":
            return True
        r = self.approvals.get(hotkey)
        return r is not None and r.state == "approved"

    def _ask(self, hotkey: str, *, ip: str, note: str) -> dict[str, Any] | None:
        """Inside the lock, under manual approval: record this ask. None when the hotkey is
        approved (its key is minted); a pending answer while it waits; a refusal (403, with the
        operator's reason) when it was refused or revoked."""
        now, cold = self.now_ms(), self._real_coldkey(hotkey)
        r = self.approvals.get(hotkey)
        if r is None:
            r = self.approvals[hotkey] = KeyRequest(hotkey, first_ms=now)
        r.last_ms, r.asks, r.coldkey = now, r.asks + 1, cold
        r.ip = _clean(ip, 64)
        if note:
            r.note = _clean(note, NOTE_MAX)
        if r.state == "approved" and r.approved_coldkey and cold and cold != r.approved_coldkey:
            # the approval was given to another owner: this one asks anew
            r.state, r.decided_ms, r.reason, r.by = "pending", 0, "", ""
            self.log(f"key request: {hotkey} now under another coldkey: pending again")
        if r.state == "approved":
            if not r.approved_coldkey:
                r.approved_coldkey = cold
            return None
        if r.state == "refused":
            raise DeskError(f"your key request was refused: {r.reason}", 403)
        if r.state == "revoked":
            raise DeskError(f"your agent key was revoked: {r.reason}", 403)
        if r.asks == 1:
            self.log(f"key request: {hotkey} asks for an agent key (pending approval)")
        return {
            "pending": True,
            "message": self.pending_message,
            "retry_after_s": PENDING_RETRY_S,
            "first_ms": r.first_ms,
        }

    def agent_key(
        self, hotkey: str, x25519_public: bytes, *, ip: str = "", note: str = ""
    ) -> dict[str, Any]:
        """Mint (or rotate) the miner's hotkey-bound agent key and answer it sealed to the
        miner's ephemeral key.

        **Manual approval** (``key_approval="manual"``): a hotkey the operator has not approved
        gets no key; its ask is recorded (hotkey, coldkey, first and last ask, client address
        ``ip`` and the miner's ``note``) and answered ``{"pending": true, ...}``; a refused or
        revoked hotkey is refused (403) with the operator's reason. Once approved, the same call
        mints as below.

        Checked before the platform is asked: the public key must be a valid X25519 key; a hotkey
        whose live key was minted less than ``mint_fresh_s`` ago gets none (it uses that one); and
        the validator's own budgets -- ``mint_per_hour`` in all and ``mint_per_coldkey_hour`` per
        owner, below the platform's ceiling -- are never passed (429), so a crowd of hotkeys
        cannot run the platform's mint role into its freeze. Revocations are never counted."""
        if not valid_public(x25519_public):
            raise DeskError("x25519_public is a 32-byte X25519 public key")
        with self._lock:
            m = self._miner(hotkey)
            now = self.now_ms()
            if m.blocked_until_ms > now:
                raise DeskError("this hotkey's key is disabled for now", 403)
            if self.key_approval == "manual":
                with self._mut():
                    pending = self._ask(hotkey, ip=ip, note=note)
                if pending is not None:
                    return pending
            if m.key_id and m.minted_ms and now - m.minted_ms < self.mint_fresh_s * 1000:
                raise DeskError("your key was just minted; use it", 429)
            cold = self.coldkey(hotkey)
            self._mints = [(t, c) for t, c in self._mints if now - t < 3_600_000]
            if self.mint_per_hour and len(self._mints) >= self.mint_per_hour:
                raise DeskError("the validator mints no more keys this hour; retry later", 429)
            if self.mint_per_coldkey_hour and (
                sum(1 for _, c in self._mints if c == cold) >= self.mint_per_coldkey_hour
            ):
                raise DeskError("this owner's hotkeys had their keys for this hour", 429)
            self._mints.append((now, cold))
        try:
            made = self.platform.mint_key(hotkey)
        except PlatformError as e:
            if e.code == SUSPENDED:
                self._suspended(hotkey)
                raise DeskError("this hotkey is suspended from the Watt agent API", 403) from None
            # The platform cannot mint now (our key frozen or refused, busy, unreachable): the
            # miner asks again later.
            raise DeskError("the validator cannot mint a key now; retry later", 503) from None
        with self._mut():
            if hotkey not in set(self.registered()):  # deregistered while the key was minted
                self._queue_revoke(hotkey)
                raise DeskError("hotkey is not registered on the subnet", 403)
            if not self.approved(hotkey):  # revoked while the key was minted
                self._queue_revoke(hotkey)
                raise DeskError("your agent key was revoked", 403)
            m.key_id = str(made["key_id"])
            m.minted_ms = self.now_ms()
            if hotkey in self._revoke:
                self._revoke.remove(hotkey)
        return {"key_id": m.key_id, "sealed": seal(str(made["secret"]), x25519_public, hotkey)}

    def disable(self, hotkey: str, *, for_s: float = 86_400.0, reason: str = "") -> None:
        """Abuse: the key goes (the platform's operator route), the admission with it, the job it
        holds goes to another miner without scoring anyone, and the hotkey gets no job and no key
        until the block lifts (then it takes the entry test again). Never the subnet owner's or
        the validator's hotkey."""
        if hotkey in self.excluded:
            return
        with self._mut():
            self._disable(hotkey, for_s, reason)
        self._revoke_keys()

    def _disable(self, hotkey: str, for_s: float, reason: str) -> None:
        """Inside a mutation."""
        m = self.miners.setdefault(hotkey, Miner(hotkey))
        until = self.now_ms() + int(for_s * 1000)
        m.blocked_until_ms = max(m.blocked_until_ms, until)
        m.key_id, m.admitted = "", False
        self._queue_revoke(hotkey)
        job = self.jobs.get(m.job)
        a = job.current if job else None
        if job is not None and a is not None and a.hotkey == hotkey:
            self._spawn(self._freeze_quietly, a.workspace)
            self._fail(job, a, "the miner was disabled", kind="disabled")
        self.disabled_log.append(
            {"hotkey": hotkey, "reason": reason, "at_ms": self.now_ms(), "until_ms": until}
        )
        del self.disabled_log[:-500]

    def _abuse_counts(self, hotkey: str) -> dict[str, int]:
        return {
            "copies": self.copied.get(hotkey, 0),
            "suspensions": self.suspensions.get(hotkey, 0),
            "admission_failures": self.admission_failed.get(hotkey, 0),
            "assigned": self.assigned.get(hotkey, 0),
            "rejected": self.rejected.get(hotkey, 0),
            "overturned": self.overturned.get(hotkey, 0),
        }

    def _abuse_reasons(self, hotkey: str, fleet: float) -> list[str]:
        """What, counted since this hotkey's last strike, crosses the policy."""
        pol = self.abuse
        now, seen = self._abuse_counts(hotkey), self.abuse_seen.get(hotkey, {})
        d = {k: v - int(seen.get(k, 0)) for k, v in now.items()}
        why: list[str] = []
        if pol.copies and d["copies"] >= pol.copies:
            why.append(f"{d['copies']} copies (policy {pol.copies})")
        if pol.suspensions and d["suspensions"] >= pol.suspensions:
            why.append(f"{d['suspensions']} Watt suspension(s) (policy {pol.suspensions})")
        if pol.admission_failures and d["admission_failures"] >= pol.admission_failures:
            why.append(
                f"{d['admission_failures']} failed admission tests "
                f"(policy {pol.admission_failures})"
            )
        if pol.rejections:
            r, n, ov = d["rejected"], d["assigned"], d["overturned"]
            if r >= pol.rejections_min and n > 0 and r / n > pol.rejection_ratio * fleet:
                why.append(
                    f"rejections: {r} of {n} assignments, over {pol.rejection_ratio}x the "
                    f"fleet's {fleet:.3f}"
                )
            if pol.overturned and ov >= pol.overturned:
                why.append(f"rejections: {ov} overturned (policy {pol.overturned})")
        return why

    def enforce(self) -> list[dict[str, Any]]:
        """The daemon's tick: disable every miner whose abuse counts, since its last strike,
        cross :attr:`abuse`. Answers what it did (hotkey, reason, block), each logged."""
        acted: list[dict[str, Any]] = []
        with self._mut():
            fleet = sum(self.rejected.values()) / (sum(self.assigned.values()) or 1)
            hotkeys = (
                set(self.copied)
                | set(self.suspensions)
                | set(self.admission_failed)
                | set(self.rejected)
                | set(self.overturned)
            )
            for hk in sorted(hotkeys - self.excluded):
                why = self._abuse_reasons(hk, fleet)
                if why:
                    acted.append(self._strike(hk, "; ".join(why)))
            acted += self._coldkey_strikes(fleet)
        for a in acted:
            self.log(
                f"disabled {a['hotkey']} for {a['for_s'] / 3600:g} h (strike {a['strike']}): "
                f"{a['reason']}"
            )
        if acted:
            self._spawn(self._revoke_keys)
        return acted

    def _strike(self, hk: str, reason: str) -> dict[str, Any]:
        """Inside a mutation: one strike, its block doubling the last."""
        strike = self.strikes.get(hk, 0) + 1
        for_s = min(
            self.abuse.disable_s * 2 ** (strike - 1),
            max(self.abuse.max_disable_s, self.abuse.disable_s),
        )
        self.strikes[hk] = strike
        self.abuse_seen[hk] = self._abuse_counts(hk)
        self._disable(hk, for_s, reason)
        return {"hotkey": hk, "reason": reason, "strike": strike, "for_s": for_s}

    def _coldkey_strikes(self, fleet: float) -> list[dict[str, Any]]:
        """The rejection rule over each coldkey's hotkeys together (counted since each one's last
        strike): one owner spreading its rejections over many hotkeys stays under the per-hotkey
        rule. Every hotkey of a crossing coldkey that rejected since its last strike is disabled.
        Inside a mutation."""
        pol = self.abuse
        if not pol.rejections:
            return []
        cmap = self._cmap()
        by_ck: dict[str, list[str]] = {}
        for hk in sorted(set(self.assigned) - self.excluded):
            by_ck.setdefault(self.coldkey(hk, cmap), []).append(hk)
        out: list[dict[str, Any]] = []
        for ck, hks in sorted(by_ck.items()):
            if len(hks) < 2:
                continue
            delta: dict[str, dict[str, int]] = {}
            for hk in hks:
                now, seen = self._abuse_counts(hk), self.abuse_seen.get(hk, {})
                delta[hk] = {k: now[k] - int(seen.get(k, 0)) for k in now}
            r = sum(d["rejected"] for d in delta.values())
            n = sum(d["assigned"] for d in delta.values())
            ov = sum(d["overturned"] for d in delta.values())
            why = ""
            if r >= pol.rejections_min and n > 0 and r / n > pol.rejection_ratio * fleet:
                why = (
                    f"rejections by coldkey {ck[:16]}: {r} of {n} assignments over "
                    f"{len(hks)} hotkeys, over {pol.rejection_ratio}x the fleet's {fleet:.3f}"
                )
            elif pol.overturned and ov >= pol.overturned:
                why = f"rejections by coldkey {ck[:16]}: {ov} overturned (policy {pol.overturned})"
            if not why:
                continue
            for hk in hks:
                if delta[hk]["rejected"] > 0:
                    out.append(self._strike(hk, why))
        return out

    def _suspended(self, hotkey: str) -> None:
        """The platform says Watt suspended this miner (``agent_suspended``): it deleted the key
        itself, so the miner is no longer free for a job here until the block lifts (it gets no
        new key from the platform while the suspension stands)."""
        with self._mut():
            m = self.miners.setdefault(hotkey, Miner(hotkey))
            m.key_id = ""
            m.blocked_until_ms = max(m.blocked_until_ms, self.now_ms() + 86_400_000)
            self.suspensions[hotkey] = self.suspensions.get(hotkey, 0) + 1

    def _queue_revoke(self, hotkey: str) -> None:
        if hotkey not in self._revoke:
            self._revoke.append(hotkey)

    def _revoke_keys(self) -> None:
        """Ask the platform to delete each queued hotkey's key; what it did not take is kept and
        asked again on the next tick."""
        with self._lock:
            todo = list(self._revoke)
        for hk in todo:
            try:
                self.platform.disable_key(hk)
            except PlatformError as e:
                if e.status != 404:  # no key there is what we wanted
                    continue
            with self._mut():
                if hk in self._revoke:
                    self._revoke.remove(hk)

    def sync_registered(self, hotkeys: Iterable[str]) -> list[str]:
        """After a metagraph read: every known miner no longer registered is deregistered here --
        its key revoked on the platform, its admission dropped, its job's attempt ended without a
        score and the job sent to another miner. Answers the hotkeys deregistered now."""
        reg = set(hotkeys)
        gone: list[str] = []
        with self._mut():
            for m in self.miners.values():
                if m.hotkey in reg or not (m.key_id or m.admitted or m.job):
                    continue
                gone.append(m.hotkey)
                m.key_id, m.admitted, m.admission_ms = "", False, 0
                self._queue_revoke(m.hotkey)
                job = self.jobs.get(m.job)
                a = job.current if job else None
                if job is not None and a is not None and a.hotkey == m.hotkey:
                    self._spawn(self._freeze_quietly, a.workspace)
                    self._fail(job, a, "the miner deregistered", kind="dereg")
                elif job is None or job.state != "judging":
                    m.job = ""
            # a pending key request of a hotkey that left is dropped (it asks again if it comes
            # back); a decision (approved, refused, revoked) is kept: a hotkey keeps its coldkey
            for hk in [h for h, r in self.approvals.items() if r.state == "pending"]:
                if hk not in reg:
                    del self.approvals[hk]
        if gone:
            self._spawn(self._revoke_keys)
        return gone

    # -- manual key approval (the operator) --------------------------------------------------------

    def key_requests(self, state: str = "pending") -> list[dict[str, Any]]:
        """The key requests in ``state`` (pending | approved | refused | revoked | all), oldest ask
        first, each with whether the hotkey holds a key now, is admitted and is registered."""
        if state not in ("pending", "approved", "refused", "revoked", "all"):
            raise DeskError("state is pending, approved, refused, revoked or all")
        reg = set(self.registered())
        with self._lock:
            rows = []
            for r in sorted(self.approvals.values(), key=lambda r: (r.first_ms, r.hotkey)):
                if state != "all" and r.state != state:
                    continue
                m = self.miners.get(r.hotkey)
                rows.append(
                    {
                        **asdict(r),
                        "registered": r.hotkey in reg,
                        "keyed": bool(m and m.key_id),
                        "admitted": bool(m and m.admitted),
                    }
                )
            return rows

    def set_launch_equal(self, on: bool, *, by: str = "operator") -> dict[str, Any]:
        """Turn the launch rule on or off (persisted); the next hourly vector follows it. Answers
        the value before and after."""
        with self._mut():
            was, self.launch_equal = self.launch_equal, bool(on)
        if was != self.launch_equal:
            self.log(f"launch-equal {'on' if self.launch_equal else 'off'} (by {by})")
        return {"launch_equal": self.launch_equal, "was": was}

    def _request(self, hotkey: str) -> KeyRequest:
        """Inside the lock: the hotkey's request, made for a registered hotkey that never asked (an
        operator may decide before the ask)."""
        hotkey = str(hotkey or "").strip()
        if not hotkey:
            raise DeskError("hotkey required")
        if hotkey in self.excluded:
            raise DeskError("the subnet owner's and the validator's hotkeys do not mine")
        r = self.approvals.get(hotkey)
        if r is None:
            if hotkey not in set(self.registered()):
                raise DeskError("no key request from this hotkey, and it is not registered", 404)
            r = self.approvals[hotkey] = KeyRequest(hotkey)
        return r

    def approve(self, hotkey: str) -> dict[str, Any]:
        """The operator approves ``hotkey``: its next ``agent_key`` call mints its key. The
        approval is bound to its coldkey now (or at its first mint, when unknown yet). It still
        passes the entry test before any job or weight."""
        with self._mut():
            r = self._request(hotkey)
            r.state, r.reason, r.decided_ms, r.by = "approved", "", self.now_ms(), "operator"
            r.approved_coldkey = self._real_coldkey(r.hotkey) or r.coldkey
            self.log(f"key request: {r.hotkey} approved by the operator")
            return asdict(r)

    def refuse(self, hotkey: str, reason: str) -> dict[str, Any]:
        """The operator refuses ``hotkey``'s request; the miner reads ``reason`` on its next ask.
        A hotkey that holds a key or an admission is revoked instead (:meth:`revoke`)."""
        why = _clean(reason, REASON_MAX)
        if not why:
            raise DeskError("a refusal says why, for the miner")
        with self._mut():
            r = self._request(hotkey)
            m = self.miners.get(r.hotkey)
            if m is not None and (m.key_id or m.admitted):
                raise DeskError("this hotkey holds a key or an admission: revoke it", 409)
            r.state, r.reason, r.decided_ms, r.by = "refused", why, self.now_ms(), "operator"
            self.log(f"key request: {r.hotkey} refused by the operator: {why}")
            return asdict(r)

    def revoke(self, hotkey: str, reason: str) -> dict[str, Any]:
        """The operator revokes ``hotkey``: its key is deleted on the platform (queued and retried,
        as for a deregistration), its admission dropped, and the job it holds ends for it without
        scoring anyone (``revoked``, uncounted) and goes to another miner; an entry test it holds
        or waits for ends "not admitted" (not counted as a failed test). It is out of the weight
        vector from the next one on. It gets no key until approved again, and then takes the entry
        test again (at once: the daily limit restarts)."""
        why = _clean(reason, REASON_MAX)
        if not why:
            raise DeskError("a revocation says why, for the miner")
        with self._mut():
            r = self._request(hotkey)
            r.state, r.reason, r.decided_ms, r.by = "revoked", why, self.now_ms(), "operator"
            m = self.miners.get(r.hotkey)
            freed = ""
            if m is not None:
                m.key_id, m.admitted, m.admission_ms, m.minted_ms = "", False, 0, 0
                freed = self._free_job(m, "the operator revoked the miner's key", "revoked")
            self._queue_revoke(r.hotkey)
            for job in self.jobs.values():
                if job.kind == "admission" and job.target == r.hotkey and job.state == "queued":
                    job.state = "done"
                    job.result = {"label": "not admitted", "charge": False, "why": "revoked"}
            self.log(f"key revoked by the operator: {r.hotkey}: {why}")
            out = asdict(r)
            out["freed_job"] = freed
        self._spawn(self._revoke_keys)
        return out

    def _free_job(self, m: Miner, why: str, kind: str) -> str:
        """Inside a mutation: the job ``m`` holds ends for it without scoring anyone (``kind``,
        uncounted) and goes back to the queue for another miner (the way a deregistration does); a
        grant still being made is dropped by :meth:`_assign`; a verdict being reached at its
        deadline releases it. Answers the job's id ('' when it held none)."""
        job = self.jobs.get(m.job)
        a = job.current if job else None
        held = m.job
        if job is not None and a is not None and a.hotkey == m.hotkey:
            self._spawn(self._freeze_quietly, a.workspace)
            self._fail(job, a, why, kind=kind)
        elif job is None or job.state != "judging":
            m.job = ""
        return held

    def _freeze_quietly(self, ws: str) -> None:
        with contextlib.suppress(PlatformError):
            self.platform.freeze(ws)

    def work(self, hotkey: str) -> dict[str, Any]:
        """The miner's poll: its assignment, if it has one."""
        with self._mut():
            m = self._miner(hotkey)
            job = self.jobs.get(m.job)
            a = job.current if job else None
            # its last job, cancelled by the client: the miner stops working on it
            told = {"cancelled": m.cancelled_job} if m.cancelled_job else {}
            if job is None or a is None or a.hotkey != hotkey:
                return {"job": None, **told}
            return {
                **told,
                "job": job.job,
                "workspace": a.workspace,
                "brief": job.brief,
                "effort": job.effort,
                "accepted": bool(a.accepted_ms),
                "accept_by_ms": a.accept_by_ms,
                "deadline_ms": a.deadline_ms,
                # the client asked not to be asked questions: decide them, leave none open
                "no_questions": job.no_questions,
                # whether the client allows sub-projects (brief/job.toml; W0233 if false)
                **({"subprojects": job.subprojects} if job.subprojects is not None else {}),
                # a style round: printed shapes only; Watt refuses a change of function (W0220)
                **({"style": True} if job.style.get("baseline") else {}),
                # a round continuing an unfinished one: the workspace starts as that project; the
                # builder may continue it or start over from the base
                **(self._continuation_work(job, a) if job.continuation else {}),
            }

    @staticmethod
    def _continuation_work(job: Job, a: Attempt) -> dict[str, Any]:
        """``work``'s words for a continuation: its base, the last delivered version (a read-only
        workspace granted beside the job's, ``workspace``) or the brief."""
        if job.continuation.get("base") and a.reference:
            return {"continuation": True, "base": {"kind": "delivered", "workspace": a.reference}}
        return {"continuation": True, "base": {"kind": "brief"}}

    def failures(self, hotkey: str, limit: int = 20) -> list[dict[str, Any]]:
        """The miner's own recent attempts that did not pass, with why (its receipt shows them): a
        gate failure says what the validator's gate found, e.g. a client answer left unrecorded."""
        with self._lock:
            rows = [
                {"job": j.job, "kind": a.kind, "why": a.outcome[:1200], "at_ms": a.submitted_ms}
                for j in self.jobs.values()
                for a in j.attempts
                if a.hotkey == hotkey and a.kind in ("gate", "timeout", "copy")
            ]
        return sorted(rows, key=lambda r: r["at_ms"])[-limit:]

    def accept(self, hotkey: str, job_id: str) -> dict[str, Any]:
        with self._mut():
            self._miner(hotkey)
            job = self.jobs.get(job_id)
            a = job.current if job else None
            if job is None or a is None or a.hotkey != hotkey or job.state != "assigned":
                self._gone(job, hotkey)
                raise DeskError("no such assignment", 404)
            now = self.now_ms()
            m = self.miners[hotkey]
            m.cancelled_job, m.after_cancel = "", False
            a.accepted_ms = now
            a.deadline_ms = now + self.deadlines.get(job.effort, DEADLINE_S["medium"]) * 1000
            job.state = "working"
            self._event("accepted", job_id, hotkey=hotkey, deadline_ms=a.deadline_ms)
            self._leased_ms = 0  # the platform hears of the builder on the next beat
            return {"job": job_id, "workspace": a.workspace, "deadline_ms": a.deadline_ms}

    def submit(
        self, hotkey: str, job_id: str, workspace: str, digest: str, summary: str = ""
    ) -> dict[str, Any]:
        """The hand-over. ``summary``, optional: the builder's own words on what blocks delivery
        (shown to the client when the round ends not finished)."""
        with self._mut():
            self._miner(hotkey)
            job = self.jobs.get(job_id)
            a = job.current if job else None
            if (
                job is None
                or a is None
                or a.hotkey != hotkey
                or job.state != "working"
                or a.workspace != workspace
            ):
                self._gone(job, hotkey)
                raise DeskError("no such job in progress for this hotkey", 404)
            a.submitted_ms, a.digest = self.now_ms(), str(digest or "")[:100]
            a.summary = " ".join(str(summary or "").split())[:SUMMARY_MAX]
            job.state = "judging"
            # the miner's part is over: it is free for another job while the validator judges this
            # one (``max_inflight`` still counts the judgement)
            self._release(hotkey, job_id)
            self._leased_ms = 0
            self._event(
                "submitted",
                job_id,
                hotkey=hotkey,
                digest=a.digest,
                on_time=a.submitted_ms <= a.deadline_ms,
                summary_chars=len(a.summary),
            )
        self._start_judge(job, a)
        return {"job": job_id, "state": "judging"}

    @staticmethod
    def _gone(job: Job | None, hotkey: str) -> None:
        """410 for a call on a job its client cancelled, by a miner that worked on it."""
        if (
            job is not None
            and job.state == "done"
            and job.result.get("label") == "cancelled"
            and any(x.hotkey == hotkey for x in job.attempts)
        ):
            raise DeskError("the client cancelled this job: stop working on it", 410)

    def _start_judge(self, job: Job, a: Attempt) -> None:
        with self._lock:
            if job.job in self._judging:
                return
            self._judging.add(job.job)
        self._spawn(self._judge, job, a)

    def reject(self, hotkey: str, job_id: str, category: str, message: str) -> dict[str, Any]:
        """The agent declines the assignment (the scope guide: not allowed, or not capable; or
        ``needs-client``: the client asked not to be asked questions, and a need of the brief can
        only be met or traded away by the client's decision -- Watt's W0240, a decision that
        ``misses`` a need -- whose question and options are the message), with a message the
        client reads if the job ends declined. The job goes to a second miner, which decides on its
        own and is not shown the first one's message. A ``needs-client`` rejection counts in the
        miner's rejection rate like the others, but another miner delivering the job does not
        overturn it: on a no-questions job, missing a need is the client's decision alone, so the
        rejection was honest even when another builder traded the need away itself
        (:meth:`_overturns`)."""
        if category not in CATEGORIES:
            raise DeskError("category is not-allowed, not-capable or needs-client")
        text = " ".join(str(message or "").split())[:2000]
        if not text:
            raise DeskError("a rejection says why, for the client")
        with self._mut():
            self._miner(hotkey)
            job = self.jobs.get(job_id)
            a = job.current if job else None
            if job is None or a is None or a.hotkey != hotkey:
                self._gone(job, hotkey)
                raise DeskError("no such assignment", 404)
            self._spawn(self._freeze_quietly, a.workspace)
            a.category, a.message = category, text
            self.rejected[hotkey] = self.rejected.get(hotkey, 0) + 1
            self._event("rejected", job_id, hotkey=hotkey, category=category)
            self._leased_ms = 0  # no builder now: the platform hears so on the next beat
            self._fail(job, a, f"rejected ({category})", kind="reject")
        self.tick()
        return {"job": job_id, "state": self.status(job_id)["state"]}

    def telemetry(self) -> dict[str, dict[str, Any]]:
        """Rejections per miner, and how many another miner then passed. A rate over twice the
        fleet's (with at least 5), or 3 overturned rejections, is flagged: a miner shedding hard
        work."""
        fleet = sum(self.rejected.values()) / (sum(self.assigned.values()) or 1)
        out: dict[str, dict[str, Any]] = {}
        for hk, n in self.assigned.items():
            r, ov = self.rejected.get(hk, 0), self.overturned.get(hk, 0)
            rate = r / n
            out[hk] = {
                "assigned": n,
                "rejected": r,
                "overturned": ov,
                "rate": round(rate, 3),
                "flagged": (r >= 5 and rate > 2 * fleet) or ov >= 3,
            }
        return out

    # -- watch (observability only: nothing here changes a score) --------------------------------

    def _watch_pair(self, pair: dict[str, Any]) -> None:
        """One copy comparison (inside the judging mutation): kept for the hourly report."""
        self.watch_pairs.append(pair)
        if len(self.watch_pairs) > WATCH_PAIRS:
            over = len(self.watch_pairs) - WATCH_PAIRS
            del self.watch_pairs[:over]
            self.watch_dropped += over

    def watch(self, coldkey_by_hotkey: Mapping[str, str]) -> dict[str, Any]:
        """Observations for calibrating the anti-abuse thresholds, published in the hourly report:

        - ``copy_pairs``: every copy comparison since the last report -- Jaccard ``similarity``
          and ``containment`` of the distinctive features, their ``sizes``, how many features the
          learned common set removed (``common``), what decided (``decided_by``) and the verdict
          (``copy``) -- and ``copy_policy``, the thresholds they were judged by;
        - ``endings_by_coldkey``: per coldkey, the declined, out-of-reach and not-finished endings
          its hotkeys took part in over the desk's 7-day memory, and ``same_coldkey_endings``:
          those whose ending attempts all came from one coldkey (colluding hotkeys; none can end so
          while ``second_opinion.coldkeys`` is 2 or more);
        - ``by_coldkey``: per coldkey, its hotkeys' assignments, rejections, overturned
          rejections, gate failures, copies and delivered jobs (the per-owner rejection rule);
        - ``admitted_by_coldkey``: the admitted hotkeys per coldkey, and ``proven``: the hotkeys
          with a delivered client job (genesis weights);
        - ``disabled``: the disables since the last report;
        - ``delivery_refusals``: delivered results the platform refused on provenance since the
          last report (a security ALERT each; their jobs' scores withdrawn).

        A hotkey with no known coldkey is grouped as ``unknown:<hotkey>``."""

        def ck(hk: str) -> str:
            return coldkey_by_hotkey.get(hk) or f"unknown:{hk}"

        with self._lock:
            endings: dict[str, dict[str, Any]] = {}
            same: list[dict[str, Any]] = []
            for j in self.jobs.values():
                label = str(j.result.get("label") or "") if j.state == "done" else ""
                if j.kind != "client" or label not in ENDINGS:
                    continue
                ending = [
                    a.hotkey
                    for a in j.attempts
                    if a.kind in SUBSTANTIVE or (a.kind == "timeout" and a.accepted_ms)
                ]
                key = ENDINGS[label]
                for c in sorted({ck(h) for h in ending}):
                    row = endings.setdefault(
                        c, {"declined": 0, "out_of_reach": 0, "not_finished": 0, "hotkeys": []}
                    )
                    row[key] += 1
                    row["hotkeys"] = sorted(set(row["hotkeys"]) | {h for h in ending if ck(h) == c})
                if len(ending) >= 2 and len({ck(h) for h in ending}) == 1:
                    same.append({"job": j.job, "label": label, "coldkey": ck(ending[0])})
            admitted: dict[str, list[str]] = {}
            for m in self.miners.values():
                if m.admitted:
                    admitted.setdefault(ck(m.hotkey), []).append(m.hotkey)
            tallies = {
                "assigned": self.assigned,
                "rejected": self.rejected,
                "overturned": self.overturned,
                "gate_failed": self.failed,
                "copies": self.copied,
                "delivered": self.delivered,
            }
            by_coldkey: dict[str, dict[str, Any]] = {}
            for name, counts in tallies.items():
                for hk, n in counts.items():
                    row = by_coldkey.setdefault(
                        ck(hk), {"hotkeys": [], **dict.fromkeys(tallies, 0)}
                    )
                    row[name] += n
                    if hk not in row["hotkeys"]:
                        row["hotkeys"].append(hk)
            for row in by_coldkey.values():
                row["hotkeys"].sort()
            return {
                "copy_pairs": list(self.watch_pairs),
                "copy_pairs_dropped": self.watch_dropped,
                "copy_policy": asdict(self.copies.policy),
                "endings_by_coldkey": endings,
                "same_coldkey_endings": same,
                "by_coldkey": dict(sorted(by_coldkey.items())),
                "proven": sorted(h for h, n in self.delivered.items() if n > 0),
                "admitted_by_coldkey": {k: sorted(v) for k, v in sorted(admitted.items())},
                "disabled": list(self.disabled_log),
                "delivery_refusals": list(self.delivery_refusals),
            }

    def clear_watch(self, pairs: int) -> None:
        """After the hour's report took :meth:`watch`: drop what it published (``pairs`` copy
        comparisons; ones recorded since stay for the next report)."""
        with self._mut():
            del self.watch_pairs[: max(0, pairs)]
            self.watch_dropped = 0
            self.disabled_log.clear()
            self.delivery_refusals.clear()

    # -- jobs --------------------------------------------------------------------------------------

    def create(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """A job the validator CLAIMED from the platform's queue (``claim``), ready to assign. Not
        an
        entry point for the platform: the platform does not push jobs, the validator pulls them."""
        privacy = str(body.get("privacy") or "public")
        if privacy not in ("public", "opt-in"):
            raise DeskError(
                "private jobs never reach miners unless the client opts in (DESIGN.md section 12)",
                403,
            )
        brief = str(body.get("brief") or "")
        seed = str(body.get("seed") or "")
        if not brief.strip() and not seed:
            raise DeskError("a job needs a brief (or a seed project to continue)")
        effort = str(body.get("effort") or "medium")
        if effort not in self.deadlines:
            raise DeskError(f"effort is one of {sorted(self.deadlines)}")
        job_id = str(body.get("job") or f"job-{secrets.token_hex(8)}")
        exclude = [str(x) for x in body.get("exclude") or []]
        rerun_of = str(body.get("rerun_of") or "")
        with self._mut():
            old = self.jobs.get(job_id)
            if old is not None and old.state == "done" and old.result.get("lost"):
                # claimed again after the platform took it back (a lapsed lease): a fresh round of
                # the same job -- its earlier attempts kept as history (the lost one uncounted)
                old.brief, old.seed, old.effort = brief, seed, effort
                old.no_questions = body.get("no_questions") is True
                old.subprojects = _flag(body.get("subprojects"))
                old.style = _style_of(body.get("style"))
                old.continuation = _continuation_of(body.get("continuation"))
                old.state, old.result = "queued", {}
                old.queued_ms = self.now_ms()
                if job_id in self._outbox:
                    self._outbox.remove(job_id)
                self._final.discard(job_id)
                self._event("reclaimed", job_id, attempts=len(old.attempts))
                return self.status(job_id)
            if old is not None:
                return self.status(job_id)
            if rerun_of:
                # A re-run is a new job at the normal price, sent to a different miner; for the
                # first job's
                # miner it is a mild negative signal from the client.
                old = self.jobs.get(rerun_of)
                if old is not None:
                    exclude += [a.hotkey for a in old.attempts]
                self.ledger.signal(
                    rerun_of,
                    {
                        "kind": "rerun",
                        "trust": str(body.get("trust") or "free"),
                        "at_ms": self.now_ms(),
                    },
                )
            self.jobs[job_id] = Job(
                job=job_id,
                brief=brief,
                effort=effort,
                privacy=privacy,
                seed=seed,
                prefer=str(body.get("prefer") or ""),
                exclude=exclude,
                created_ms=self.now_ms(),
                queued_ms=self.now_ms(),
                no_questions=body.get("no_questions") is True,
                subprojects=_flag(body.get("subprojects")),
                style=_style_of(body.get("style")),
                continuation=_continuation_of(body.get("continuation")),
            )
        return self.status(job_id)

    def admission(self, hotkey: str) -> dict[str, Any]:
        """A miner's entry test: a job assigned to it alone, once a day -- a small device from a
        client-style brief (``admission_brief``), at ``admission_effort`` with that effort's
        deadline, and with no client to ask (``no_questions``: the builder decides every open
        question). It passes only through the client jobs' gate (:meth:`_gate`: deliverable on the
        validator's own guard run, no unproven must; and :meth:`_answers_gate`: no client decision
        left open), submitted before its deadline. Passing admits the miner; only admitted miners
        get client jobs and weight. Under manual approval only an approved key holder may ask."""
        with self._mut():
            m = self._miner(hotkey)
            if not self.approved(hotkey):
                raise DeskError("your agent key is not approved yet: ask for it first", 403)
            if not m.key_id:
                raise DeskError("ask for your agent key first")
            if m.admitted:
                return {"admitted": True}
            now = self.now_ms()
            if now - m.admission_ms < 86_400_000:
                live = [
                    j for j in self.jobs.values() if j.kind == "admission" and j.target == hotkey
                ]
                if live:
                    return self.status(live[-1].job)
                raise DeskError("one admission test a day", 429)
            m.admission_ms = now
            job_id = f"admission-{secrets.token_hex(6)}"
            self.jobs[job_id] = Job(
                job=job_id,
                brief=self.admission_brief,
                effort=self.admission_effort,
                kind="admission",
                no_questions=True,
                target=hotkey,
                created_ms=now,
                queued_ms=now,
            )
        self.tick()
        return self.status(job_id)

    def signal(self, job_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """The client's reaction to a delivered job, from the platform: download, stars (1-5),
        change, or a refund the platform granted (``quality``: true when it was granted for the
        delivery's quality -- scored as one star -- false otherwise: recorded, not scored). Late
        signals change the job's score when they arrive."""
        kind = str(body.get("kind") or "")
        if kind not in ("download", "stars", "change", "refund"):
            raise DeskError(
                "kind is download, stars, change or refund (a re-run is a new job with rerun_of)"
            )
        trust = str(body.get("trust") or "free")
        if trust not in ("paid", "free"):
            raise DeskError("trust is paid or free")
        sig: dict[str, Any] = {"kind": kind, "trust": trust, "at_ms": self.now_ms()}
        if kind == "stars":
            stars = int(body.get("stars") or 0)
            if not 1 <= stars <= 5:
                raise DeskError("stars are 1 to 5")
            sig["stars"] = stars
        if kind == "refund":
            if not isinstance(body.get("quality"), bool):
                raise DeskError("a refund says quality: true or false")
            sig["quality"] = body["quality"]
        with self._mut():
            s = self.ledger.signal(job_id, sig)
        if s is None:
            raise DeskError("no delivered job by that id", 404)
        return {"job": job_id, "score": s}

    def cancel(self, job_id: str, *, lost: bool = False) -> dict[str, Any]:
        """The client cancels (what it is charged is the platform's). A job in progress is frozen
        and its miner freed; nobody is scored on it. The miner is told on its next ``work``
        (``cancelled``), gets no new job for ``cancel_cooloff_s`` (time to stop its agent), and its
        first assignment after this one is not scored if it is not accepted in time. A job being
        judged has the validator's guard run stopped and its fork deleted.

        ``lost``: the platform no longer holds the job for this validator (its lease lapsed, and the
        platform queued the job again). The copy here stops the same way, but its open attempt is
        ``lost`` (uncounted; its miner may take the job again), its held failures are kept, and a
        later claim of the job reopens it (:meth:`create`)."""
        with self._mut():
            job = self.jobs.get(job_id)
            if job is None:
                raise DeskError("no such job", 404)
            if job.state == "done":
                return self.status(job_id)
            judging = job.state == "judging"
            a = job.current
            if a is None and judging and job.attempts:
                a = job.attempts[-1]
            if a is not None:
                self._spawn(self._freeze_quietly, a.workspace)
                a.outcome, a.kind = (
                    ("the validator's lease lapsed: the platform took the job back", "lost")
                    if lost
                    else ("cancelled by the client", "cancelled")
                )
                m = self.miners.get(a.hotkey)
                if m is not None and m.job == job_id:
                    # still working on it (a submitted attempt freed its miner, which may hold
                    # another job by now): told, and cooled off
                    m.cancelled_job, m.cancelled_ms, m.after_cancel = job_id, self.now_ms(), True
                self._release(a.hotkey, job_id)
                if judging:
                    self._drop_gate(job.job, a)
            self._event(
                "lease-lost" if lost else "cancelled",
                job_id,
                state="judging" if judging else job.state,
            )
            if not lost:
                job.pending.clear()
            job.state = "done"
            job.result = {"label": "cancelled", "charge": False, "attempts": len(job.attempts)}
            if lost:
                job.result["lost"] = True
            return self.status(job_id)

    def capacity(self) -> dict[str, Any]:
        """What the platform's single queue needs for its ETA: admitted miners active now, how many
        are
        free, and the median wall time of the last 20 passing jobs (None before there are any). The
        platform computes positions; the validator keeps no waiting list."""
        now = self.now_ms()
        with self._lock:
            active = [
                m
                for m in self.miners.values()
                if m.admitted and m.key_id and now - m.last_seen_ms <= ALIVE_S * 1000
            ]
            free = sum(1 for m in active if not m.job)
            held = sum(1 for j in self.jobs.values() if j.state == "queued")
        recent = sorted((o for o in self.ledger.outcomes if o.passed), key=lambda o: o.ended_ms)[
            -20:
        ]
        return {
            "active_miners": len(active),
            "free_miners": max(0, free - held),
            "median_job_s": statistics.median(o.wall_s for o in recent) if recent else None,
        }

    def admitted(self) -> list[str]:
        with self._lock:
            return [
                m.hotkey for m in self.miners.values() if m.admitted and self.approved(m.hotkey)
            ]

    def reachable(self, within_s: float = 86_400.0) -> list[str]:
        now = self.now_ms()
        with self._lock:
            return [
                m.hotkey for m in self.miners.values() if now - m.last_seen_ms <= within_s * 1000
            ]

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise DeskError("no such job", 404)
            return {
                "job": job.job,
                "state": job.state,
                "effort": job.effort,
                "attempts": [asdict(a) for a in job.attempts],
                "result": dict(job.result),
            }

    # -- routing (section 6) -----------------------------------------------------------------------

    def _available(self, job: Job) -> list[Miner]:
        now = self.now_ms()
        reg = set(self.registered())
        tried = self._tried(job)
        cmap = self._cmap()
        # A second opinion comes from another owner: the coldkeys that rejected or failed this job
        # do not get it again until it has waited ``second_opinion.wait_s``.
        opinions = {self.coldkey(a.hotkey, cmap) for a in job.attempts if a.kind in SUBSTANTIVE}
        if not opinions or now - job.queued_ms >= self.second_opinion.wait_s * 1000:
            opinions = set()
        return [
            m
            for m in self.miners.values()
            if m.key_id
            and not m.job
            and m.hotkey in reg
            and m.blocked_until_ms <= now
            and now - m.last_seen_ms <= ALIVE_S * 1000
            and now - m.cancelled_ms >= self.cancel_cooloff_s * 1000
            and m.hotkey not in tried
            and self.coldkey(m.hotkey, cmap) not in opinions
        ]

    @staticmethod
    def _tried(job: Job) -> set[str]:
        """The hotkeys that may not take ``job``: those that tried it (a ``lost`` attempt aside: the
        platform took the job back from the validator, not from the miner) and its excluded ones."""
        return {a.hotkey for a in job.attempts if a.kind != "lost"} | set(job.exclude)

    def _exhausted(self, job: Job, reg: set[str]) -> bool:
        """Whether no miner that could ever take ``job`` is left: none registered, admitted, keyed,
        not excluded and not yet tried. A busy, idle, cooling-off or second-opinion-waiting miner
        still counts (it may take the job later), so a job never ends while a miner of another
        coldkey -- or, after the wait, of the same one -- could still take it."""
        tried = self._tried(job)
        return not any(
            m.admitted and m.key_id and m.hotkey in reg and m.hotkey not in tried
            for m in self.miners.values()
            if m.hotkey not in self.excluded
        )

    def _end_exhausted(self, job: Job) -> None:
        """Inside a mutation: every miner that could take ``job`` has tried it. It ends declined
        (every counted attempt a rejection: the miners' messages shown) or "didn't finish";
        uncharged either way. The held failures count (0) unless they came from
        ``second_opinion.coldkeys`` coldkeys, as at any undelivered ending."""
        counted = [x for x in job.attempts if x.kind not in UNCOUNTED]
        substantive = [x for x in job.attempts if x.kind in SUBSTANTIVE]
        cmap = self._cmap()
        owners = {self.coldkey(x.hotkey, cmap) for x in substantive}
        independent = len(owners) >= max(1, self.second_opinion.coldkeys)
        label = "declined" if all(x.kind == "reject" for x in counted) else "didn't finish"
        if not independent:
            for held in job.pending:
                held.ended_ms = self.now_ms()
                self.ledger.add(held)
        job.pending.clear()
        job.state = "done"
        last = job.attempts[-1]
        job.result = {
            "label": label,
            "charge": False,
            "review": label == "declined",
            "why": "every miner that could take the job has tried it",
            "messages": [
                {"category": x.category, "message": x.message}
                for x in job.attempts
                if x.kind == "reject"
            ],
            "hotkey": last.hotkey,
            "workspace": "",
            "attempts": len(job.attempts),
        }
        self._outbox.append(job.job)
        self._event("ended", job.job, label=label, attempts=len(job.attempts), exhausted=True)

    def route(self, job: Job) -> Miner | None:
        """One miner. An admission job: its own miner. A client job: an ADMITTED miner -- the job's
        previous miner if it is free; else, 10% of the time, a newcomer or a low scorer; else by
        quality (the decayed mean of its job scores), weighted (quality + 0.05)^2. There is no team
        miner: with no miner free the job waits in the queue -- never rejected for lack of miners --
        until a miner is free or the client cancels."""
        free = self._available(job)
        if job.kind == "admission":
            return next((m for m in free if m.hotkey == job.target), None)
        free = [m for m in free if m.admitted]
        if not free:
            return None
        for m in free:
            if m.hotkey == job.prefer:
                return m
        quality = self.ledger.quality(self.now_ms())
        top = max(quality.values(), default=0.0)
        jobs_done: dict[str, int] = {}
        for o in self.ledger.outcomes:
            jobs_done[o.hotkey] = jobs_done.get(o.hotkey, 0) + 1
        weak = [
            m
            for m in free
            if jobs_done.get(m.hotkey, 0) < THIN_JOBS or quality.get(m.hotkey, 0.0) < 0.5 * top
        ]
        if weak and self.rng.random() < EXPLORE:
            return self.rng.choice(weak)
        weights = [(quality.get(m.hotkey, 0.0) + 0.05) ** 2 for m in free]
        return self.rng.choices(free, weights=weights)[0]

    def tick(self) -> None:
        """Assign queued jobs, enforce the acceptance windows and deadlines, claim new jobs for
        free admitted miners. Call every few seconds; it never waits on a grant or a judgement
        (they run in the pool)."""
        now = self.now_ms()
        late: list[tuple[Job, Attempt]] = []
        with self._mut():
            for job in self.jobs.values():
                a = job.current
                if job.state == "assigned" and a and now > a.accept_by_ms:
                    self._spawn(self._freeze_quietly, a.workspace)
                    m = self.miners.get(a.hotkey)
                    if m is not None and m.after_cancel:
                        # the first job after a cancel, missed while the miner stopped the
                        # cancelled one: nobody is scored, nothing counted
                        m.after_cancel = False
                        if self.assigned.get(a.hotkey, 0) > 0:
                            self.assigned[a.hotkey] -= 1
                        self._fail(
                            job, a, "not accepted in time, just after a cancel", kind="stale"
                        )
                    else:
                        self._fail(job, a, "not accepted in time", kind="timeout")
                elif job.state == "working" and a and now > a.deadline_ms:
                    job.state = "judging"
                    late.append((job, a))
                    self._event("deadline", job.job, hotkey=a.hotkey)
            for key in [
                k
                for k, j in self.jobs.items()
                if j.state == "done" and now - j.created_ms > DONE_KEEP_MS and k not in self._outbox
            ]:
                del self.jobs[key]
            want = self._want(now)
        # The claim loop: one job from the platform's queue per free admitted miner, assigned at
        # once. A job held for a reassignment takes a free miner first; with no free miner, nothing
        # is claimed and the jobs stay in the platform's queue.
        for _ in range(want):
            try:
                payload = self.platform.claim()
            except PlatformError:
                break
            if not payload:
                break
            try:
                self.create(payload)
            except DeskError as e:  # a job the platform should not have queued for einstein
                with self._mut():
                    job_id = str(payload.get("job"))
                    self.jobs[job_id] = Job(
                        job=job_id,
                        brief="",
                        state="done",
                        created_ms=self.now_ms(),
                        result={"label": "refused", "charge": False, "why": str(e)},
                    )
                    self._outbox.append(job_id)
        if self._heart is None:
            self._lease(now)
        with self._mut():
            inflight = sum(
                1
                for j in self.jobs.values()
                if j.state in ("assigning", "assigned", "working", "judging")
            )
            reg = set(self.registered())
            for job in [
                j
                for j in self.jobs.values()
                if j.state == "queued"
                and j.kind == "client"
                and any(x.kind not in UNCOUNTED for x in j.attempts)
            ]:
                if self._exhausted(job, reg):
                    self._end_exhausted(job)
            queued = sorted(
                (j for j in self.jobs.values() if j.state == "queued"), key=lambda j: j.created_ms
            )
            plan: list[tuple[Job, Miner]] = []
            for job in queued:
                if inflight >= self.max_inflight:
                    break
                m = self.route(job)
                if m is None:
                    continue
                m.job, job.state = job.job, "assigning"
                plan.append((job, m))
                inflight += 1
        with self._lock:
            stalled = [
                (j, j.attempts[-1])
                for j in self.jobs.values()
                if j.state == "judging"
                and j.attempts
                and j.job not in self._judging
                and self._retry_at.get(j.job, 0.0) <= time.monotonic()
            ]
        for job, a in [*late, *stalled]:
            self._start_judge(job, a)
        for job, m in plan:
            self._spawn(self._assign, job, m)
        self._spawn_flush()
        with self._lock:
            revoke = bool(self._revoke)
        if revoke:
            self._spawn(self._revoke_keys)

    def _want(self, now: int) -> int:
        """How many jobs to claim now: one per free admitted miner, after the held jobs, within
        ``max_inflight``."""
        inflight = sum(
            1
            for j in self.jobs.values()
            if j.state in ("assigning", "assigned", "working", "judging")
        )
        held = sum(1 for j in self.jobs.values() if j.state == "queued" and j.kind == "client")
        reg = set(self.registered())
        free = sum(
            1
            for m in self.miners.values()
            if m.admitted
            and m.key_id
            and not m.job
            and m.hotkey in reg
            and m.blocked_until_ms <= now
            and now - m.last_seen_ms <= ALIVE_S * 1000
            and now - m.cancelled_ms >= self.cancel_cooloff_s * 1000
        )
        return max(0, min(free - held, self.max_inflight - inflight - held))

    def _lease(self, now: int) -> None:
        """Renew the lease on every claimed job not yet done (the platform re-queues a job whose
        lease lapses); a job the platform no longer holds for us (cancelled, re-queued) is dropped
        here. Run by the heartbeat thread (or by ``tick`` when there is none)."""
        if not self._lease_lock.acquire(blocking=False):
            return
        try:
            if now - self._leased_ms < self.lease_every_s * 1000:
                return
            self._leased_ms = now
            with self._lock:
                open_jobs = [
                    j.job
                    for j in self.jobs.values()
                    if j.kind == "client" and (j.state != "done" or self._held_for_review(j))
                ]
                # a builder accepted these: the platform charges a client's cancel from then on
                working = [
                    j.job
                    for j in self.jobs.values()
                    if j.kind == "client"
                    and j.attempts
                    and j.attempts[-1].accepted_ms
                    and j.state in ("working", "judging")
                ]
            if not open_jobs:
                return
            try:
                out = self.platform.lease(open_jobs, working=working)
            except PlatformError:
                return
            for job_id in out.get("lost") or []:
                job = self.jobs.get(str(job_id))
                if job is not None and self._held_for_review(job):
                    with self._mut():  # the platform let it go (cancelled, re-queued): stop leasing
                        job.result = {**job.result, "released": True}
                    continue
                with contextlib.suppress(DeskError):
                    self.cancel(str(job_id), lost=True)
        finally:
            self._lease_lock.release()

    def reconcile(self) -> int:
        """After a restart: the jobs the platform says we hold that the store does not know are
        taken again from scratch (with a store, the ones it knows keep their attempts). Returns how
        many were new."""
        n = 0
        for payload in self.platform.claimed():
            if str(payload.get("job")) not in self.jobs:
                self.create(payload)
                n += 1
        return n

    def _spawn_flush(self) -> None:
        with self._lock:
            if not self._outbox:
                return
        self._spawn(self._flush)

    def _flush(self) -> None:
        """Ended jobs' results go back to the platform, which charges (delivered) or not, and
        releases the hold; a result the platform did not take stays in the outbox (persisted) and
        is sent again on the next tick."""
        if not self._flushing.acquire(blocking=False):
            return
        try:
            with self._lock:
                todo = list(self._outbox)
            for job_id in todo:
                with self._lock:
                    job = self.jobs.get(job_id)
                    result = dict(job.result) if job else None
                if result is not None:
                    try:
                        answer = self.platform.complete(job_id, result)
                    except PlatformError as e:
                        if e.code in REFUSED_DELIVERY:
                            self._refused_delivery(job_id, e)
                        continue  # anything else: sent again on a later tick
                    if (
                        result.get("label") == "delivered"
                        and result.get("charge")
                        and isinstance(answer, Mapping)
                        and answer.get("charged") is False
                    ):
                        self._uncharged(job_id, answer)
                    elif (
                        result.get("label") == "delivered"
                        and result.get("charge")
                        and self.on_delivered is not None
                    ):
                        try:
                            self.on_delivered(job_id, str(result.get("hotkey") or ""))
                        except Exception as e:  # noqa: BLE001 -- a review never blocks a delivery
                            self.log(f"review queue: {type(e).__name__}: {e}")
                with self._mut():
                    if job_id in self._outbox:
                        self._outbox.remove(job_id)
        finally:
            self._flushing.release()

    @staticmethod
    def _held_for_review(job: Job) -> bool:
        """A job whose delivery the platform refused: still claimed there, so still leased."""
        return job.result.get("label") == "delivery refused" and not job.result.get("released")

    def _refused_delivery(self, job_id: str, e: PlatformError) -> None:
        """The platform refused a delivered result on provenance (409 ``delivery_not_of_job`` or
        ``delivery_digest_mismatch``): nothing was kept or charged and the job stays claimed for
        review. A security ALERT; the result is never sent again (the same bytes would be refused
        again).

        **Whose failure.** The validator's (or the platform's), never the miner's: the result names
        the validator's own fork, made from the miner's workspace AFTER it was frozen, which only
        the validator's key can write, and its digest is the one the validator's gate read on that
        fork. A miner can neither substitute the workspace (``submit`` takes only its own granted
        one) nor move the fork's digest. So the job's scores are withdrawn -- the pass is not
        rewarded for a delivery that did not happen, and the held failures it turned into zeros
        are not counted either -- and nobody is penalised. The job keeps its lease (the platform
        holds it claimed) until the platform drops it or the client cancels."""
        with self._mut():
            job = self.jobs.get(job_id)
            if job is None:
                return
            if job_id in self._outbox:
                self._outbox.remove(job_id)
            gone = self.ledger.withdraw(job_id)
            self._unscore(job, gone)
            row = {
                "job": job_id,
                "code": e.code,
                "error": str(e)[:300],
                "hotkey": str(job.result.get("hotkey") or ""),
                "workspace": str(job.result.get("workspace") or ""),
                "digest": str(job.result.get("digest") or ""),
                "withdrawn": len(gone),
                "at_ms": self.now_ms(),
            }
            job.result = {
                **job.result,
                "label": "delivery refused",
                "charge": False,
                "refused": row,
            }
            self.delivery_refusals.append(row)
            del self.delivery_refusals[:-500]
        self.log(
            f"ALERT: the platform refused job {job_id}'s delivery ({e.code}: {e}); not sent again, "
            f"its scores withdrawn, the job held for review"
        )

    def _uncharged(self, job_id: str, answer: Mapping[str, Any]) -> None:
        """The platform took a delivered result and charged nothing (the client cancelled while it
        was sent, or the delivery could not be kept): no delivery happened, so the job's scores are
        withdrawn as for a refused delivery, and nobody is penalised. Not sent again."""
        with self._mut():
            job = self.jobs.get(job_id)
            if job is None:
                return
            gone = self.ledger.withdraw(job_id)
            self._unscore(job, gone)
            job.result = {
                **job.result,
                "label": str(answer.get("label") or "not charged")[:40],
                "charge": False,
                "uncharged": True,
                "withdrawn": len(gone),
            }
        self.log(
            f"job {job_id}: the platform charged nothing for its delivery "
            f"({answer.get('label') or 'no label'}); its scores withdrawn"
        )

    def _assign(self, job: Job, m: Miner) -> None:
        reference = ""
        try:
            if job.continuation:
                ws, reference = self.platform.grant_continuation(
                    m.hotkey,
                    seed=job.seed,
                    base=str(job.continuation.get("base") or ""),
                    name=job.job,
                )
            else:
                ws = self.platform.grant(m.hotkey, brief=job.brief, seed=job.seed, name=job.job)
        except PlatformError as e:
            if e.code == SUSPENDED:
                self._suspended(m.hotkey)
            with self._mut():
                if m.job == job.job:
                    m.job = ""
                if job.state == "assigning":
                    job.state = "queued"
            return
        now = self.now_ms()
        with self._mut():
            if job.state != "assigning" or m.job != job.job:
                # cancelled, or the miner left, while the workspace was being made
                self._spawn(self._freeze_quietly, ws)
                if m.job == job.job:
                    m.job = ""
                if job.state == "assigning":
                    job.state = "queued"
                return
            self.assigned[m.hotkey] = self.assigned.get(m.hotkey, 0) + 1
            if m.cancelled_job == job.job:  # its job again, after a lapsed lease: not "cancelled"
                m.cancelled_job = ""
            job.attempts.append(
                Attempt(
                    hotkey=m.hotkey,
                    workspace=ws,
                    assigned_ms=now,
                    accept_by_ms=now + int(self.accept_s * 1000),
                    reference=str(reference or ""),
                )
            )
            job.state = "assigned"
            self._event(
                "assigned",
                job.job,
                hotkey=m.hotkey,
                attempt=len(job.attempts),
                workspace=ws,
                kind=job.kind,
                effort=job.effort,
                no_questions=job.no_questions or None,
            )

    def _fail(
        self,
        job: Job,
        a: Attempt,
        why: str,
        *,
        kind: str,
        outcome: JobOutcome | None = None,
        fork: str = "",
        verdict: Mapping[str, Any] | None = None,
    ) -> None:
        """An attempt that ended without a passing project. Called inside a mutation.

        - ``timeout`` (not accepted, or late and failing) and ``copy`` score 0 at once: reliability
          and cheating are the miner's alone.
        - ``reject`` and ``gate`` are held: they score 0 only if another miner then passes the job.
        - ``dereg`` (the miner left the subnet) scores nobody and does not count toward the cap.
        - An attempt the builder WORKED (accepted) that does not deliver -- a gate failure, or a
          late attempt that fails -- ends the round "not finished": its project (the validator's
          fork, ``fork``) is kept for the client, who may pay for a round that continues it.
        - Two rejections end the job "declined"; past ``max_attempts`` it is "didn't finish". None
          is charged. The held failures count (0) unless they came from
          ``second_opinion.coldkeys`` coldkeys (then they are dropped: no miner is penalised for a
          job nobody could do).
        """
        a.outcome, a.kind = why, kind
        if kind != "reject":  # a rejection logged its own line
            self._event("attempt-failed", job.job, hotkey=a.hotkey, kind=kind, why=why[:200])
        if job.kind == "admission":
            self._release(a.hotkey, job.job)
            self._event("ended", job.job, label="not admitted", hotkey=a.hotkey)
            job.state = "done"
            job.result = {"label": "not admitted", "charge": False, "why": why}
            if kind in ("gate", "timeout"):
                self.admission_failed[a.hotkey] = self.admission_failed.get(a.hotkey, 0) + 1
            return
        if kind == "gate":
            self.failed[a.hotkey] = self.failed.get(a.hotkey, 0) + 1
        if kind not in UNCOUNTED:
            o = outcome or JobOutcome(
                job=job.job,
                hotkey=a.hotkey,
                ended_ms=self.now_ms(),
                effort=job.effort,
                size=size_class(job.brief),
                accepted=bool(a.accepted_ms) or kind == "reject",
            )
            o.kind = kind
            if kind in ("timeout", "copy"):
                self.ledger.add(o)
            else:
                job.pending.append(o)
        self._release(a.hotkey, job.job)
        substantive = [x for x in job.attempts if x.kind in SUBSTANTIVE]
        counted = [x for x in job.attempts if x.kind not in UNCOUNTED]
        cmap = self._cmap()
        owners = {self.coldkey(x.hotkey, cmap) for x in substantive}
        independent = len(owners) >= max(1, self.second_opinion.coldkeys)
        # The builder worked it and it did not deliver: the round ends here, its project kept
        # (DESIGN.md "Continuing an unfinished round"), never handed to another miner from scratch.
        worked = kind in ("gate", "timeout") and bool(a.accepted_ms)
        ends = (len(substantive) >= 2 and independent) or worked
        if not ends and len(counted) < self.max_attempts:
            job.state = "queued"
            job.queued_ms = self.now_ms()
            self._event("requeued", job.job, attempts=len(job.attempts))
            return
        if worked:
            label = "not finished"
            self.not_finished[a.hotkey] = self.not_finished.get(a.hotkey, 0) + 1
        elif ends:
            label = "declined" if all(x.kind == "reject" for x in substantive) else "out of reach"
        else:
            label = "didn't finish"
        if not independent:
            # No second owner confirmed that nobody could do it: the held failures count (0).
            for held in job.pending:
                held.ended_ms = self.now_ms()
                self.ledger.add(held)
        job.pending.clear()
        job.state = "done"
        job.result = {
            "label": label,
            "charge": False,
            "review": label == "declined",
            "why": why,
            "messages": [
                {"category": x.category, "message": x.message}
                for x in job.attempts
                if x.kind == "reject"
            ],
            "hotkey": a.hotkey,
            "workspace": fork,
            "attempts": len(job.attempts),
            **dict(verdict or {}),
        }
        if worked:
            job.result.pop("copy", None)
            job.result["summary"] = a.summary or _gate_summary(kind, verdict or {})
            job.result["summary_by"] = "builder" if a.summary else "gate"
        self._outbox.append(job.job)
        self._event("ended", job.job, label=label, hotkey=a.hotkey, attempts=len(job.attempts))

    def _release(self, hotkey: str, job_id: str) -> None:
        """Free ``hotkey`` of ``job_id`` -- only of that job: a miner freed at its submit may hold
        another job by the time this one ends."""
        m = self.miners.get(hotkey)
        if m is not None and m.job == job_id:
            m.job = ""

    def _event(self, event: str, job: str, **fields: Any) -> None:
        """One structured line per job event (:func:`job_event`)."""
        with contextlib.suppress(Exception):  # a log line never breaks the desk
            self.log(job_event(event, job, **fields))

    # -- judging (section 4) -----------------------------------------------------------------------

    def _judge(self, job: Job, a: Attempt) -> None:
        try:
            self._judge_once(job, a)
        except Exception as e:  # noqa: BLE001 -- never a silent dead judgement, never a hot loop
            self._retry(job, a, f"{type(e).__name__}: {e}", RETRY_FIRST_S)
        finally:
            with self._lock:
                self._judging.discard(job.job)
        self._spawn_flush()

    # -- the validator's fork and its guard run --------------------------------------------------

    def _drop_gate(self, job_id: str, a: Attempt, *, keep_fork: bool = False) -> None:
        """Stop the validator's guard run on ``a``'s fork and delete the fork, in the worker pool
        (a job that ended without a delivery: the fork serves nothing). ``keep_fork``: a round that
        ends not finished keeps its fork, the project the client may continue. Under the lock."""
        stop = self._gate_stop.get(job_id)
        if stop is not None:
            stop.set()
        run, fork = a.gate_run, ("" if keep_fork else a.fork)
        a.gate_run = ""
        if run or fork:
            self._spawn(self._drop_quietly, run, fork)

    def _drop_quietly(self, run: str, fork: str) -> None:
        if run:
            with contextlib.suppress(PlatformError):
                self.platform.cancel_run(run)
        if fork:
            with contextlib.suppress(PlatformError):
                self.platform.delete(fork)

    def _retry(self, job: Job, a: Attempt, why: str, first_s: float) -> None:
        """A judgement that failed on OUR side: judged again after a back-off (doubling from
        ``first_s``, at most ``RETRY_MAX_S``, with jitter) -- never scored, never sent to another
        miner. Past ``judge_max_retries`` retries, or ``judge_give_up_s`` since the first, the
        job ends "didn't finish": not charged, nobody scored."""
        with self._mut():
            if job.state != "judging" or not job.attempts or job.attempts[-1] is not a:
                return  # ended meanwhile (a cancel)
            now = self.now_ms()
            a.retries += 1
            a.first_retry_ms = a.first_retry_ms or now
            a.outcome = f"judging retried: {why[:200]}"
            a.last_retry = why[:200]
            if a.retries > self.judge_max_retries or (
                now - a.first_retry_ms >= self.judge_give_up_s * 1000
            ):
                self._give_up(job, a, why)
                return
            delay = min(RETRY_MAX_S, first_s * 2 ** (a.retries - 1))
            delay *= 0.8 + 0.4 * self.rng.random()
            self._retry_at[job.job] = time.monotonic() + delay
            self._event(
                "judge-retry",
                job.job,
                hotkey=a.hotkey,
                retry=a.retries,
                in_s=round(delay, 1),
                gate_run=a.gate_run,
                why=why[:200],
            )

    def _give_up(self, job: Job, a: Attempt, why: str) -> None:
        """Inside a mutation: the validator could not judge this attempt on its own side. The job
        ends "didn't finish", charged nothing (the platform releases the hold); nobody is scored
        and the held failures are dropped; the miner is freed and the fork deleted."""
        a.outcome = f"the validator could not judge it: {why[:200]}"
        a.kind = "ours"
        self._event("ended", job.job, label="didn't finish", hotkey=a.hotkey, ours=True)
        self._release(a.hotkey, job.job)
        self._drop_gate(job.job, a)
        job.pending.clear()
        job.state = "done"
        if job.kind == "admission":
            job.result = {"label": "not admitted", "charge": False, "why": a.outcome}
        else:
            job.result = {
                "label": "didn't finish",
                "charge": False,
                "why": "the validator could not judge the delivery",
                "attempts": len(job.attempts),
            }
            self._outbox.append(job.job)
        self.log(
            f"ALERT: job {job.job} ended without a judgement after {a.retries} retries on the "
            f"validator's side ({why[:200]}): not charged, nobody scored"
        )

    @staticmethod
    def _ours(e: PlatformError) -> bool:
        """Whether a platform failure while judging is the validator's side (its credential, its
        quotas, the platform's or Watt's load), never the miner's."""
        return e.ours or e.status in (0, 408, 429) or e.status >= 500 or e.code in OURS_BUSY

    def _status(self, fork: str) -> dict[str, Any]:
        st = self.platform.run(fork, "status", {})
        return dict(((st.get("data") or {}).get("status")) or {})

    def _gate(self, fork: str, job: Job | None = None, a: Attempt | None = None) -> dict[str, Any]:
        """The Watt gate on the validator's fork of a frozen workspace.

        1. ``watt status``: the miner's own record must say deliverable (its checks clean, a guard
           recorded for this digest); otherwise the attempt fails here, at no further cost.
        2. ``watt report --mutate`` on the fork, the validator's own run (the guard, and the
           delivery's files made from the design it guards, in one run) (the operator's lane: the
           bench seeds its draw with a key the miners' lane never sees, so the mutants it builds
           are not the ones the miner's record drew). The fork's guard outcomes and build are
           reused, so only the mutants the miner's run did not build are built.
        3. ``watt status`` again: deliverable, the same digest, and the guard record's draw is the
           one this run reports (``guard_draw.seed``). A guard run that refuses leaves the miner's
           record in place, so ``deliverable`` alone would still read the miner's draw: the run's
           own success and its draw are what pass the gate. (A Watt that does not sample names no
           draw on either side: its guard is the full one, in a fixed order, which no re-roll
           changes.) The validator's draw of a digest is the same on every run, so a design
           delivered before and submitted unchanged draws as it did then.

        The guard run has a budget (``gate_run_s``): still going at it, the validator stops it --
        freeing its lane for the other judgements -- and raises :class:`_GateTimeoutError`, as for
        a run Watt stopped at its own wall-clock limit. A cancel of the job stops it too.

        **A judgement retried on our side** (a poll that failed past the client's own retries, a
        validator restart mid-run) takes the attempt's ``gate_run`` up again -- waiting for it, or
        reading the result it already has -- with what is left of its budget, instead of starting
        another: the fork cannot even be read while that run holds it, and a second run repeats
        minutes of guard work. A run the platform no longer knows (404) is started again.

        Raises :class:`_GateRetryError` (and lets :class:`PlatformError` through) on our side.
        """
        jid = job.job if job is not None else ""
        resume = a.gate_run if a is not None else ""
        if resume and a is not None and a.gate_digest:
            # the miner's record said deliverable when this run was started, at this digest
            digest = a.gate_digest
            verdict: dict[str, Any] = {
                "deliverable": False,
                "stage": "deliverable",
                "digest": digest,
            }
        else:
            before = self._status(fork)
            digest = str(before.get("digest") or "")
            verdict = {
                "deliverable": False,
                "stage": str(before.get("stage") or "none"),
                "digest": digest,
            }
            if not before.get("deliverable"):
                return verdict
        # the guard and the delivery's files in ONE run (`watt report --mutate`): one machine start,
        # and the files are made from the very design this run guards (2026-10-16)
        stop = threading.Event()
        if jid:
            with self._lock:
                self._gate_stop[jid] = stop

        def started(run_id: str) -> None:
            if a is not None:
                with self._mut():
                    a.gate_run = str(run_id)
                    a.gate_digest, a.gate_started_ms = digest, self.now_ms()
            self._event("gate-start", jid, run=str(run_id), fork=fork, digest=digest)

        try:
            run: dict[str, Any] | None = None
            if resume and a is not None:
                spent = (self.now_ms() - a.gate_started_ms) / 1000 if a.gate_started_ms else 0.0
                left = max(1.0, self.gate_run_s - max(0.0, spent))
                self._event("gate-attach", jid, run=resume, fork=fork, left_s=round(left))
                try:
                    run = self.platform.attach(resume, budget_s=left, stop=stop)
                except PlatformError as e:
                    if e.status != 404:
                        raise
                    # the platform no longer knows it: a run of our own, started again
                    self._event("gate-lost", jid, run=resume)
                    with self._mut():
                        a.gate_run = ""
            if run is None:
                run = self.platform.run(
                    fork,
                    "report",
                    {"mutate": True},
                    budget_s=self.gate_run_s,
                    on_run=started,
                    stop=stop,
                )
        finally:
            if jid:
                with self._lock:
                    self._gate_stop.pop(jid, None)
        state, stopped = run.get("state"), str(run.get("stopped") or "")
        if stop.is_set():
            raise _GateRetryError("the job ended while its guard ran")
        if state == "running":
            # past its budget: stopped here, so it no longer holds the validator's lane
            run_id = str(run.get("run") or "")
            if run_id:
                with contextlib.suppress(PlatformError):
                    self.platform.cancel_run(run_id)
            if a is not None:
                with self._mut():
                    a.gate_run = ""
            raise _GateTimeoutError(
                f"the validator's guard run {run_id} passed its {self.gate_run_s:g} s budget"
            )
        if a is not None:
            with self._mut():
                a.gate_run = ""
        self._event(
            "gate-run",
            jid,
            run=str(run.get("run") or resume or ""),
            state=str(state or ""),
            ok=bool(run.get("ok")),
            stopped=stopped,
        )
        if stopped == "wall":
            raise _GateTimeoutError("the validator's guard run hit Watt's wall-clock limit")
        if state != "done" or stopped in ("cancelled", "frozen"):
            why = stopped or str(run.get("error") or state)
            raise _GateRetryError(f"the validator's guard run did not end on its own: {why[:200]}")
        data = run.get("data")
        guard = (data.get("guard") if isinstance(data, dict) else None) or {}
        drawn = (guard.get("sampled") or {}).get("seed") if isinstance(guard, dict) else None
        if not run.get("ok") or not isinstance(guard, dict) or not guard or guard.get("scope"):
            limit = stopped or str(run.get("limit") or "")
            verdict["stage"] = "test"
            verdict["error"] = (
                "the validator's own mutation guard did not pass on this design"
                + (f" (stopped: {limit})" if limit else "")
                + _survivors(guard)
            )
            return verdict
        after = self._status(fork)
        draw = after.get("guard_draw") or {}
        recorded = draw.get("seed") if isinstance(draw, dict) else None
        if (
            not after.get("deliverable")
            or str(after.get("digest") or "") != digest
            or guard.get("digest") != digest
            or recorded != drawn
        ):
            verdict["error"] = (
                "the guard record is not the validator's own run "
                f"(digest {after.get('digest')}, draw {recorded}, the validator's {drawn})"
            )
            return verdict
        return {
            "deliverable": True,
            "stage": str(after.get("stage") or "deliverable"),
            "digest": digest,
        }

    @staticmethod
    def _answers_gate(
        job: Job,
        a: Attempt,
        verdict: dict[str, Any],
        export: bytes,
        kept_open: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """The client's answers, checked on the validator's own export of its fork
        (:mod:`.answers`): every answer the platform wrote into the workspace's ``brief/`` before
        the hand-over (the submit, or the deadline) is recorded in its decision, and a no-questions
        job leaves no client decision open. Otherwise the attempt fails the gate, with the reason
        the miner reads."""
        why = answers_check(
            export,
            before_ms=a.submitted_ms or a.deadline_ms,
            no_questions=job.no_questions,
            kept_open=kept_open,
        )
        if not why:
            return verdict
        return {**verdict, "deliverable": False, "stage": "answers", "error": why}

    def _settled_open(self, job: Job) -> frozenset[str]:
        """A style round's client decisions that stay open (cadmind-watt ADR-020: deciding one would
        change the contract it keeps): exactly the baseline's ``settled_decisions``, as Watt's
        ``style begin`` answers them. A baseline without them exempts nothing (the stricter gate).
        Empty on any other job."""
        baseline = job.style.get("baseline") if job.style else None
        if not isinstance(baseline, Mapping):
            return frozenset()
        named = baseline.get("settled_decisions")
        if not isinstance(named, list):
            return frozenset()
        return frozenset(str(d) for d in named)

    @staticmethod
    def _style_gate(job: Job, verdict: dict[str, Any], export: bytes) -> dict[str, Any]:
        """A style round's delivery keeps the settled contract it began from (:mod:`.style`), read
        from the validator's own export of its fork, after its own ``watt report``."""
        why = style_check(export, job.style.get("baseline"))
        if not why:
            return verdict
        return {**verdict, "deliverable": False, "stage": "style", "error": why}

    def _judge_once(self, job: Job, a: Attempt) -> None:
        """Freeze, fork, gate the fork (:meth:`_gate`), copy-check, score. An error on our side --
        the platform unreachable, 5xx, a timeout, our own key busy or over its rate or quota, our
        guard run not ending on its own -- leaves the job judging, judged again after a back-off
        on the same fork (:meth:`_retry`): never scored against the miner, never sent to another
        miner, and bounded. A guard run past its budget (or Watt's wall) twice fails the attempt.
        A fork that delivers nothing is deleted."""
        on_time = bool(a.submitted_ms) and a.submitted_ms <= a.deadline_ms
        verdict: dict[str, Any] = {"deliverable": False, "stage": "none", "digest": ""}
        fork, compute_s, export = a.fork, 0.0, b""
        try:
            frozen = self.platform.freeze(a.workspace)
            compute_s = int(frozen.get("compute_ms") or 0) / 1000
            if not fork:
                fork = self.platform.fork(a.workspace)
                with self._mut():
                    a.fork = fork
                    if job.state != "judging" or not job.attempts or job.attempts[-1] is not a:
                        self._drop_gate(job.job, a)  # cancelled while the fork was made
                        return
            verdict = self._gate(fork, job, a)
            if verdict["deliverable"]:
                export = self.platform.export(fork)
                if job.kind in ("client", "admission"):
                    # an entry test has no client: no_questions, so no client decision left open
                    kept_open = self._settled_open(job)
                    verdict = self._read_export(
                        verdict, lambda: self._answers_gate(job, a, verdict, export, kept_open)
                    )
                if job.kind == "client" and verdict["deliverable"] and job.style.get("baseline"):
                    verdict = self._read_export(
                        verdict, lambda: self._style_gate(job, verdict, export)
                    )
        except _GateTimeoutError as e:
            with self._mut():
                a.gate_timeouts += 1
                timeouts = a.gate_timeouts
            if timeouts < GATE_TIMEOUTS:
                self._retry(job, a, str(e), RETRY_FIRST_S)
                return
            verdict["stage"] = "test"
            verdict["error"] = (
                f"the guard did not finish within the validator's budget {GATE_TIMEOUTS} times"
            )
        except _GateRetryError as e:
            self._retry(job, a, str(e), RETRY_FIRST_S)
            return
        except PlatformError as e:
            if self._ours(e):
                first = RETRY_BUSY_FIRST_S if e.code == "run_in_progress" else RETRY_FIRST_S
                self._retry(job, a, str(e), first)
                return
            verdict["error"] = str(e)
        end = a.submitted_ms or self.now_ms()
        outcome = JobOutcome(
            job=job.job,
            hotkey=a.hotkey,
            ended_ms=self.now_ms(),
            effort=job.effort,
            size=size_class(job.brief),
            accepted=True,
            on_time=on_time,
            deliverable=verdict["deliverable"],
            stage=verdict["stage"],
            compute_s=compute_s,
            wall_s=max(0.0, (end - a.accepted_ms) / 1000),
        )
        base: frozenset[bytes] = frozenset()
        mine: frozenset[bytes] = frozenset()
        if verdict["deliverable"] and job.kind == "client":
            try:
                base = fingerprint(self.platform.export(job.seed)).features if job.seed else base
            except Exception:  # noqa: BLE001 -- the seed is ours: unread, nothing is subtracted
                base = frozenset()
            start = str(job.continuation.get("base") or "")
            if start:
                # a continuation's builder may have started over from the last delivered version:
                # its features are subtracted too, as the seed's are
                with contextlib.suppress(Exception):  # ours too: unread, nothing more subtracted
                    base = base | fingerprint(self.platform.export(start)).features
            try:
                mine = fingerprint(export).features
            except Exception as e:  # noqa: BLE001 -- the miner's export: unreadable fails it
                verdict = {
                    **verdict,
                    "deliverable": False,
                    "stage": "export",
                    "error": f"the delivered project could not be read ({type(e).__name__})",
                }
                outcome.deliverable, outcome.stage = False, "export"
        cmap = self._cmap()
        cold = self.coldkey(a.hotkey, cmap)
        with self._mut():
            if job.state != "judging" or not job.attempts or job.attempts[-1] is not a:
                self._drop_gate(job.job, a)
                return  # cancelled (or lost) while it was judged: nobody is scored
            self._event(
                "verdict",
                job.job,
                hotkey=a.hotkey,
                deliverable=bool(verdict["deliverable"]),
                stage=str(verdict.get("stage") or ""),
                digest=str(verdict.get("digest") or ""),
                error=str(verdict.get("error") or "")[:200],
                on_time=on_time,
                retries=a.retries,
                last_retry=a.last_retry,
            )
            copy = None
            if verdict["deliverable"] and job.kind == "client":
                copy = self.copies.check(
                    job.job,
                    a.hotkey,
                    verdict["digest"],
                    mine,
                    base,
                    on_pair=self._watch_pair,
                    coldkey=cold,
                )
                self._remember_copy(self.copies.recent[-1])
                outcome.copy_of = str(copy["job"]) if copy else ""
                if copy:
                    self.copied[a.hotkey] = self.copied.get(a.hotkey, 0) + 1
            if job.kind == "admission" and verdict["deliverable"] and not on_time:
                verdict = {
                    **verdict,
                    "deliverable": False,
                    "error": "the entry test was not submitted before its deadline",
                }
            m = self.miners.get(a.hotkey)
            if (
                job.kind == "admission"
                and verdict["deliverable"]
                and (m is None or not m.key_id or not self.approved(a.hotkey))
            ):
                # its key was revoked (or it deregistered) while the test was judged
                self._release(a.hotkey, job.job)
                self._drop_gate(job.job, a)
                a.outcome, a.kind = "the miner's key was revoked while it was judged", "revoked"
                job.state = "done"
                job.result = {"label": "not admitted", "charge": False, "why": "revoked"}
                return
            if job.kind == "admission" and verdict["deliverable"]:
                if m is not None:
                    m.admitted = True
                a.outcome, a.kind = "admitted", "pass"
                self._release(a.hotkey, job.job)
                self._drop_gate(job.job, a)  # an entry test delivers nothing
                job.state = "done"
                job.result = {"label": "admitted", "charge": False}
                return
            if not verdict["deliverable"] or copy is not None:
                kind = "copy" if copy else ("gate" if on_time else "timeout")
                # a worked attempt that does not deliver ends the round not finished: its fork is
                # the project kept for the client (a copy's never is: it is another job's work)
                self._drop_gate(job.job, a, keep_fork=copy is None and job.kind == "client")
                self._fail(
                    job,
                    a,
                    "copy"
                    if copy
                    else "did not pass the Watt gate"
                    + (f": {verdict['error']}" if verdict.get("error") else ""),
                    kind=kind,
                    outcome=outcome,
                    fork=fork,
                    verdict={**verdict, "copy": copy},
                )
                return
            for held in job.pending:  # another miner passed: the held failures count now
                held.ended_ms = self.now_ms()
                self.ledger.add(held)
                if held.kind == "reject" and self._overturns(job, held.hotkey, a.hotkey, cmap):
                    self.overturned[held.hotkey] = self.overturned.get(held.hotkey, 0) + 1
            job.pending.clear()
            outcome.kind = "pass"
            s = self.ledger.add(outcome)
            if job.kind == "client":
                self.delivered[a.hotkey] = self.delivered.get(a.hotkey, 0) + 1
            a.outcome, a.kind = "judged", "pass"
            self._release(a.hotkey, job.job)
            job.state = "done"
            job.result = {
                "label": "delivered",
                "charge": True,
                "hotkey": a.hotkey,
                "workspace": fork,
                "digest": verdict["digest"],
                "digest_claimed": a.digest,
                "stage": verdict["stage"],
                "score": s,
                "on_time": on_time,
                "attempts": len(job.attempts),
            }
            if a.summary:  # the builder's notes on what it delivered
                job.result["summary"], job.result["summary_by"] = a.summary, "builder"
            self._outbox.append(job.job)
            self._event("ended", job.job, label="delivered", hotkey=a.hotkey, score=s)

    def _overturns(
        self, job: Job, rejecter: str, deliverer: str, cmap: Mapping[str, str] | None = None
    ) -> bool:
        """Whether ``deliverer`` passing ``job`` overturns ``rejecter``'s rejection of it (the
        abuse policy's ``overturned`` count). Not when the rejection was ``needs-client`` (on a
        no-questions job, missing a need is the client's decision alone: the rejection was honest
        even if another builder traded the need away), nor when both hotkeys are one owner's (one
        coldkey: no independent opinion). The held 0 the rejection scores is unchanged."""
        category = next(
            (x.category for x in job.attempts if x.hotkey == rejecter and x.kind == "reject"), ""
        )
        if category == "needs-client":
            return False
        cmap = self._cmap() if cmap is None else cmap
        return self.coldkey(rejecter, cmap) != self.coldkey(deliverer, cmap)

    def _unscore(self, job: Job, gone: list[JobOutcome]) -> None:
        """Inside a mutation: the desk's tallies of a job whose scores were withdrawn."""
        deliverer = str(job.result.get("hotkey") or "")
        cmap = self._cmap()
        for o in gone:
            if o.kind == "pass" and self.delivered.get(o.hotkey, 0) > 0:
                self.delivered[o.hotkey] -= 1
            if (
                o.kind == "reject"
                and self.overturned.get(o.hotkey, 0) > 0
                and self._overturns(job, o.hotkey, deliverer, cmap)
            ):
                self.overturned[o.hotkey] -= 1

    @staticmethod
    def _read_export(
        verdict: dict[str, Any], check: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """A check that reads the miner's export: anything it raises (a malformed file, nesting too
        deep) fails the attempt's gate, never the validator."""
        try:
            return check()
        except Exception as e:  # noqa: BLE001 -- the miner's bytes: unreadable fails the gate
            return {
                **verdict,
                "deliverable": False,
                "stage": "answers",
                "error": f"the delivered project could not be read ({type(e).__name__})",
            }
