"""Improvement jobs (DESIGN.md "Open lanes"): a miner in the improvement lane improves a delivered
device a judge scored low, and the result is offered to the device's client.

The desk (:meth:`.desk.Desk.work`) asks the platform for the next device of a free miner in the
improvement lane and runs the try as a job of kind ``improvement``: assigned to that miner alone,
``no_questions`` (there is no client to ask), its workspace a fork of the delivered version with a
second, read-only fork beside it, gated as any job (the validator's own guard draw), with the one
job deadline. The try's id (``imp-<12 hex>``) is made by the platform; it is the job's id here,
the grant's job, the ledger outcome's job and the job of every signal about it.

When an improvement job ends on the desk, its record comes here (:meth:`ImprovementBook.ended`,
store namespace ``improvement``). This book then, in its own thread
(:meth:`ImprovementBook.pass_once`):

1. **votes** on a result that passed the gate with the validators' judge, exactly as a delivery is
   voted (same judge, prompt, rubric and sample count; :meth:`.votes.Reviewer.judge_bundle`), on
   the bundle the platform makes from the validator's frozen copy; and on the **delivered
   version** it improves, under today's judge. That vote is cached per (device, judge digest), so
   later tries on the same device reuse it;
2. **filters**: a result at least one level above the delivered version is sent to the platform
   as an **offer**; any other is reported ``not_better``. A try that failed before the judge (the
   gate, a timeout, a copy) is reported ``failed``; a device its miner rejected is reported as
   rejected, for good for that miner;
3. **tells the desk** what the platform answered (:attr:`ImprovementBook.on_outcome`), which
   writes the try's outcome in the ledger: an offer taken is a pass (0.5 rating points, 0.25
   weight points while it is pending), a result not a level higher or one the platform refused
   for what the builder did scores 0, and an offer the platform could not take for a reason that
   is not the miner's (the device left the lane, another offer exists) makes no outcome at all;
4. deletes the validator's copy, unless it is now the offered version.

The judge is a filter only: it decides whether an offer is made, never what an accepted offer is
worth. The client's answer does (:mod:`.scoring`).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from .platform import PlatformError

SETUP_MAX = 80
"""the longest ``setup`` label kept (characters)"""
KEEP_MS = 30 * 24 * 3_600_000
"""how long a finished record is kept (the miners' stats read the last 7 days of them)"""
MAX_TRIES = 8
RETRY_FIRST_S = 60.0
RETRY_MAX_S = 3600.0
BUNDLE_WAIT_S = 24 * 3600.0
"""how long a result's bundle may stay unready before the try is reported without a vote"""
REPORT_TRIES = 30
NS, REF_NS = "improvement", "improvement-ref"
OFFER, NOT_BETTER, FAILED = "offer", "not_better", "failed"
"""what a try's result says to the platform"""
REFUSED_DELIVERY = ("delivery_not_of_job", "delivery_digest_mismatch")
"""the platform's refusals of an offer whose workspace it cannot accept: the try scores 0"""
NOT_THE_MINERS = ("improvement_gone", "offer_exists")
"""the platform's refusals of an offer for a reason that is not the miner's doing: no outcome"""


def clean_setup(value: Any) -> str:
    """A miner's ``setup`` label: one line of printable text, at most :data:`SETUP_MAX`."""
    words = " ".join(str(value or "").split())
    return "".join(ch for ch in words if ch.isprintable())[:SETUP_MAX]


class _LaterError(Exception):
    """A vote that cannot be made now: asked again later (``count``: it counts as a try)."""

    def __init__(self, why: str, *, count: bool = True, wait_s: float = 0.0) -> None:
        super().__init__(why)
        self.count, self.wait_s = count, wait_s


class ImprovementBook:
    """The improvement records (store namespace ``improvement``) and the cached votes on the
    delivered versions (``improvement-ref``), with the steps that vote on, filter and report each
    try."""

    def __init__(
        self,
        platform: Any,
        *,
        store: Any = None,
        reviewer: Any = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
        on_outcome: Callable[[Mapping[str, Any], str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        from .votes import _Kv  # noqa: PLC0415 -- votes imports the desk, which makes this book

        self.platform, self.reviewer, self.clock = platform, reviewer, clock
        self.log = log or (lambda _line: None)
        self.on_outcome = on_outcome
        """told (the record, the outcome's kind, its levels) when a judged try has an outcome:
        ``pass`` (the platform took the offer), ``not_better`` or ``refused``"""
        self.kv = _Kv(store)
        self._busy = threading.Lock()

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    @property
    def active(self) -> bool:
        """Whether a try can be judged at all: without a judge there is no filter, so no
        improvement job is served."""
        return self.reviewer is not None and bool(getattr(self.reviewer, "active", False))

    # -- records -----------------------------------------------------------------------------------

    def ended(self, record: Mapping[str, Any]) -> None:
        """An improvement job ended on the desk: its record. ``report`` says what is left to do:
        ``judge`` (it passed the gate: vote, filter, report), ``failed`` (report it failed) or
        ``reject`` (its miner rejected the device)."""
        row = dict(record)
        row.setdefault("ended_ms", self.now_ms())
        what = str(row.get("report") or FAILED)
        row.update(
            state={"judge": "judging", "reject": "rejecting"}.get(what, "reporting"),
            outcome="" if what == "judge" else ("" if what == "reject" else FAILED),
            tries=0,
            next_ms=self.now_ms(),
            score=None,
            base_score=None,
        )
        self.kv.put(NS, str(row["try_id"]), row)
        cutoff = self.now_ms() - KEEP_MS
        for old in self.kv.items(NS).values():
            if old.get("state") == "done" and int(old.get("ended_ms") or 0) < cutoff:
                self.kv.delete(NS, str(old["try_id"]))

    def rows(self, hotkey: str = "") -> list[dict[str, Any]]:
        rows = [r for r in self.kv.items(NS).values() if not hotkey or r.get("hotkey") == hotkey]
        return sorted(rows, key=lambda r: int(r.get("ended_ms") or 0))

    def known(self, try_id: str) -> bool:
        """Whether this book holds a record of ``try_id`` (ended on the desk, done or not)."""
        return self.kv.get(NS, try_id) is not None

    def rejected(self, hotkey: str, since_ms: int) -> int:
        """The devices ``hotkey`` rejected since ``since_ms``."""
        return sum(
            1
            for r in self.rows(hotkey)
            if r.get("report") == "reject" and int(r.get("ended_ms") or 0) >= since_ms
        )

    def notes_for(self, device: str, finding: Any = None) -> dict[str, Any]:
        """What the judge said of the delivered version, for the miner that improves it: the notes
        of this validator's stored vote on it (:func:`.votes.judge_notes`), else the platform's
        finding as one line; empty lists when there is neither."""
        from .votes import FINDING_CHARS, judge_notes  # noqa: PLC0415

        rv = self.reviewer
        if rv is not None and device:
            vote = rv.kv.get("vote", f"{device}|{rv.me}")
            if isinstance(vote, Mapping):
                notes = judge_notes(vote)
                if notes["lines"] or notes["nudges"]:
                    return notes
        if isinstance(finding, Mapping) and (finding.get("line") or finding.get("nudge")):
            line = str(finding.get("line") or "")[:FINDING_CHARS]
            nudge = str(finding.get("nudge") or "")[:FINDING_CHARS]
            return {
                "lines": [
                    {
                        "id": str(finding.get("id") or ""),
                        "line": line,
                        "main": True,
                        "verdict": "fails",
                        "seen": str(finding.get("quote") or "")[:FINDING_CHARS],
                    }
                ]
                if line
                else [],
                "nudges": [nudge] if nudge else [],
            }
        return {"lines": [], "nudges": []}

    # -- the steps ---------------------------------------------------------------------------------

    def pass_once(self) -> str:
        """Advance the oldest due record one step. Answers what happened, in a word."""
        if not self._busy.acquire(blocking=False):
            return "idle"
        try:
            now = self.now_ms()
            due = [
                r
                for r in self.rows()
                if r.get("state") in ("judging", "reporting", "rejecting")
                and int(r.get("next_ms") or 0) <= now
            ]
            if not due:
                return "idle"
            row = dict(due[0])
            if row["state"] == "judging":
                return self._judge(row)
            if row["state"] == "rejecting":
                return self._reject(row)
            return self._report(row)
        finally:
            self._busy.release()

    def _save(self, row: dict[str, Any]) -> None:
        self.kv.put(NS, row["try_id"], row)

    def _unjudged(self, row: dict[str, Any], why: str) -> str:
        """No vote could be made: the try is reported failed, and nobody is scored on it (the
        validator's side, not the miner's)."""
        row.update(state="reporting", outcome=FAILED, tries=0, next_ms=self.now_ms())
        row["why"] = f"the validator could not judge it: {why}"[:1200]
        self._save(row)
        return self._report(row)

    def _later(self, row: dict[str, Any], why: str, *, count: bool, wait_s: float) -> str:
        if count:
            row["tries"] = int(row.get("tries") or 0) + 1
        row["why_unjudged"] = why[:300]
        if count and row["tries"] >= MAX_TRIES:
            return self._unjudged(row, why[:300])
        wait = wait_s or min(RETRY_MAX_S, RETRY_FIRST_S * 2 ** max(0, int(row["tries"]) - 1))
        row["next_ms"] = self.now_ms() + int(wait * 1000)
        self._save(row)
        return "pending"

    def _judge(self, row: dict[str, Any]) -> str:
        rv = self.reviewer
        if not self.active:
            return self._unjudged(row, "the validator makes no votes")
        row["judge"] = str(rv.judge_cfg.version)
        try:
            if row.get("score") is None:
                if not row.get("frozen"):
                    # the bundle is made from a frozen workspace: the validator's own copy
                    self.platform.freeze(row["workspace"])
                    row["frozen"] = True
                    self._save(row)
                got = self.platform.improvement_bundle(row["try_id"], row["workspace"])
                vote = self._vote(rv, got, row)
                row.update(score=vote["score"], value=vote["value"], samples=vote["samples"])
                self._save(row)
            if row.get("base_score") is None:
                base = self.base_vote(str(row.get("device") or ""))
                row["base_score"] = base["score"]
                row["base_cached"] = bool(base.get("cached"))
        except _LaterError as e:
            return self._later(row, str(e), count=e.count, wait_s=e.wait_s)
        except PlatformError as e:
            return self._later(row, f"bundle: {e.status} {e.code} {e}", count=True, wait_s=0.0)
        better = int(row["score"]) >= int(row["base_score"]) + 1
        row.update(
            state="reporting",
            outcome=OFFER if better else NOT_BETTER,
            tries=0,
            why_unjudged="",
            next_ms=self.now_ms(),
        )
        if not better:
            row["why"] = (
                f"judged level {row['score']}, the delivered version level {row['base_score']}: "
                "not a level higher"
            )
        self._save(row)
        return self._report(row)

    def _vote(self, rv: Any, got: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
        if rv.spend is not None and not rv.spend.allow():
            raise _LaterError("the daily spend cap is reached", count=False, wait_s=3600)
        if not got.get("ready"):
            if self.now_ms() - int(row.get("ended_ms") or 0) > BUNDLE_WAIT_S * 1000:
                raise _LaterError("no bundle in time", count=True)
            wait = float(got.get("retry_s") or RETRY_FIRST_S)
            raise _LaterError(
                f"bundle {got.get('state') or 'not ready'}",
                count=False,
                wait_s=min(RETRY_MAX_S, max(15.0, wait)),
            )
        try:
            vote = dict(rv.judge_bundle(got))
        except PlatformError:
            raise
        except Exception as e:  # noqa: BLE001 -- the provider, a bundle: retried, counted
            raise _LaterError(f"vote: {type(e).__name__}: {str(e)[:200]}") from None
        if vote.get("score") is None:
            raise _LaterError("vote: no score")
        return vote

    def base_vote(self, device: str) -> dict[str, Any]:
        """Today's judge on the delivered version a try improves (``device``), cached per (device,
        judge digest): later tries on the same device reuse it."""
        rv = self.reviewer
        if not device:
            raise _LaterError("the try named no delivered version", count=True)
        key = f"{device}|{rv.digest}"
        cached = self.kv.get(REF_NS, key)
        if isinstance(cached, dict) and cached.get("score") is not None:
            return {**cached, "cached": True}
        # the "compare" copy: the delivered version without the pictures a try's bundle never has
        # (its studio render, the client's photos), so both sides are judged on the same kinds of
        # pictures
        got = self.platform.review_bundle(device, "compare")
        vote = self._vote(rv, got, {"ended_ms": self.now_ms()})
        row = {
            "score": vote["score"],
            "value": vote.get("value"),
            "samples": vote.get("samples"),
            "device": device,
            "judge": rv.digest,
            "at_ms": self.now_ms(),
        }
        self.kv.put(REF_NS, key, row)
        self.log(f"improvement: delivered version {device} voted {vote['score']} (cached)")
        return {**row, "cached": False}

    def _levels(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "base": int(row["base_score"]),
            "result": int(row["score"]),
            "judge": str(row.get("judge") or ""),
        }

    def _tell(self, row: Mapping[str, Any], kind: str) -> None:
        if self.on_outcome is not None:
            self.on_outcome(row, kind, self._levels(row))

    def _again(self, row: dict[str, Any], e: PlatformError) -> str | None:
        """A report the platform did not take now: when it is sent again (None once the tries
        are spent)."""
        row["tries"] = int(row.get("tries") or 0) + 1
        if row["tries"] >= REPORT_TRIES:
            return None
        wait = min(RETRY_MAX_S, RETRY_FIRST_S * 2 ** max(0, row["tries"] - 1))
        row["next_ms"] = self.now_ms() + int(wait * 1000)
        row["report_error"] = f"{e.status} {e.code} {str(e)[:200]}"
        self._save(row)
        return "pending"

    def _reject(self, row: dict[str, Any]) -> str:
        try:
            self.platform.improvement_reject(row["try_id"], str(row.get("hotkey") or ""))
        except PlatformError as e:
            if e.status not in (400, 403, 404, 409, 410, 422) and self._again(row, e):
                return "pending"
            row["report_error"] = f"{e.status} {e.code} {str(e)[:200]}"
            self.log(f"improvement {row['try_id']}: rejection not taken: {row['report_error']}")
        row.update(state="done", reported_ms=self.now_ms(), next_ms=0)
        self._save(row)
        return "rejected"

    def _report(self, row: dict[str, Any]) -> str:
        outcome = str(row.get("outcome") or FAILED)
        body = {
            "hotkey": row.get("hotkey"),
            "outcome": outcome,
            "workspace": row.get("workspace") or "",
            "digest": row.get("digest") or "",
            "base_score": row.get("base_score"),
            "score": row.get("score"),
            "judge": row.get("judge") or "",
            "watt": row.get("watt") or "",
            "minutes": row.get("minutes"),
            "why": row.get("why") or "",
        }
        judged = row.get("score") is not None and row.get("base_score") is not None
        kept = False
        try:
            answer = self.platform.improvement_result(row["try_id"], body)
            row["result"] = {
                k: answer.get(k) for k in ("offer_id", "job", "price", "state") if k in answer
            }
            if outcome == OFFER:
                kept = True  # the validator's copy is the offered version now
                self._tell(row, "pass")
            elif outcome == NOT_BETTER and judged:
                self._tell(row, "not_better")
        except PlatformError as e:
            row["report_error"] = f"{e.status} {e.code} {str(e)[:200]}"
            if outcome == OFFER and e.status == 422:
                # the platform's own check: not a level higher
                row["outcome"] = NOT_BETTER
                self._tell(row, "not_better")
            elif outcome == OFFER and e.code in REFUSED_DELIVERY:
                row["outcome"] = "refused"
                self._tell(row, "refused")
            elif outcome == OFFER and (e.status == 410 or e.code in NOT_THE_MINERS):
                row["outcome"] = "none"  # not the miner's doing: no outcome
            elif e.status in (400, 403, 404, 409, 410, 422):
                if outcome == NOT_BETTER and judged:
                    self._tell(row, "not_better")  # judged here: the 0 stands, reported or not
                self.log(f"improvement {row['try_id']}: report refused: {row['report_error']}")
            elif self._again(row, e):
                return "pending"
            else:
                self.log(f"improvement {row['try_id']}: report given up: {row['report_error']}")
        row.update(state="done", reported_ms=self.now_ms(), next_ms=0)
        self._save(row)
        ws = str(row.get("workspace") or "")
        if ws and not kept:
            try:
                self.platform.delete(ws)
            except PlatformError as e:
                self.log(f"improvement {row['try_id']}: its copy not deleted: {e}")
        self.log(
            f"improvement {row['try_id']} reported: {row.get('outcome')}, level "
            f"{row.get('score')} against {row.get('base_score')}"
        )
        return "reported"
