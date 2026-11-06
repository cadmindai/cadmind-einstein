"""Practice jobs (docs/MINER.md "Trying a new setup"): a miner tries a new model or setup on a
real public brief and compares its result with the version CADmind delivered, at no risk to its
standing.

The desk (:meth:`.desk.Desk.practice`) asks the platform for a practice brief -- a delivered public
gallery project's brief, exactly as its gallery page shows it, which the miner has not practised
on -- and runs it as a job of kind ``practice``: assigned to that miner alone, ``no_questions``
(the gallery shows no client answers, so the builder decides every open question), granted on the
platform's practice lane and quota, gated as any job (the validator's own guard draw). It is
**never** in the ledger, the miner's points, the weights or the routing, and never handed out
while a client job the miner could take waits.

When a practice job ends, the desk hands its record here (:meth:`PracticeBook.ended`, store
namespace ``practice``). This book then, in its own thread (:meth:`PracticeBook.pass_once`):

1. **votes** on a deliverable result with the validators' judge, exactly as a delivery is voted
   (same judge, prompt, rubric and sample count; :meth:`.votes.Reviewer.judge_bundle`), on the
   bundle the platform makes from the validator's frozen copy; and on the **reference** -- the
   delivered version of the same brief -- under today's judge. The reference vote is cached per
   (reference job, judge digest), so later practice on the same brief reuses it. A result that
   failed the gate gets no vote: it is reported "not deliverable" with the gate's reason;
2. **reports** to the platform (``practice/<id>/result``), which keeps it for the operator;
3. deletes the validator's copy.

The miner reads its results with ``GET /v1/practice`` (:meth:`PracticeBook.status`), grouped by
the free-text ``setup`` label it sent with its submit. Votes off (or no model): results are
reported with no score.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from .platform import PlatformError

PRACTICE_PER_DAY = 3
"""practice jobs a miner may start per UTC day (the platform enforces the same cap)"""
SETUP_MAX = 80
"""the longest ``setup`` label kept (characters)"""
KEEP = 200
"""practice records kept per miner (the oldest dropped past it)"""
MAX_TRIES = 8
RETRY_FIRST_S = 60.0
RETRY_MAX_S = 3600.0
BUNDLE_WAIT_S = 24 * 3600.0
"""how long a practice bundle may stay unready before the result is reported without a vote"""
REPORT_TRIES = 30
NS, REF_NS = "practice", "practice-ref"


def clean_setup(value: Any) -> str:
    """A miner's ``setup`` label: one line of printable text, at most :data:`SETUP_MAX`."""
    words = " ".join(str(value or "").split())
    return "".join(ch for ch in words if ch.isprintable())[:SETUP_MAX]


class _LaterError(Exception):
    """A vote that cannot be made now: asked again later (``count``: it counts as a try)."""

    def __init__(self, why: str, *, count: bool = True, wait_s: float = 0.0) -> None:
        super().__init__(why)
        self.count, self.wait_s = count, wait_s


class PracticeBook:
    """The practice records (store namespace ``practice``) and the cached reference votes
    (``practice-ref``), with the steps that vote on and report each result."""

    def __init__(
        self,
        platform: Any,
        *,
        store: Any = None,
        reviewer: Any = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        from .votes import _Kv  # noqa: PLC0415 -- votes imports the desk, which makes this book

        self.platform, self.reviewer, self.clock = platform, reviewer, clock
        self.log = log or (lambda _line: None)
        self.kv = _Kv(store)
        self._busy = threading.Lock()

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    # -- records -----------------------------------------------------------------------------------

    def ended(self, record: Mapping[str, Any]) -> None:
        """A practice job ended on the desk: its record, to vote on (deliverable) or to report."""
        row = dict(record)
        row.setdefault("ended_ms", self.now_ms())
        row.update(
            state="judging" if row.get("deliverable") else "reporting",
            tries=0,
            next_ms=self.now_ms(),
            score=None,
            reference_score=None,
            why_unscored="",
        )
        self.kv.put(NS, str(row["practice_id"]), row)
        mine = sorted(
            (r for r in self.kv.items(NS).values() if r.get("hotkey") == row.get("hotkey")),
            key=lambda r: int(r.get("ended_ms") or 0),
        )
        for old in mine[:-KEEP]:
            if old.get("state") == "done":
                self.kv.delete(NS, str(old["practice_id"]))

    def rows(self, hotkey: str = "") -> list[dict[str, Any]]:
        rows = [r for r in self.kv.items(NS).values() if not hotkey or r.get("hotkey") == hotkey]
        return sorted(rows, key=lambda r: int(r.get("ended_ms") or 0))

    def status(self, hotkey: str, open_jobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """A miner's practice: its results (newest last) and, per ``setup`` label, how many it
        ran, how many were deliverable and the mean scores of the voted ones beside the delivered
        versions'. Nothing here is scored."""
        results = [self._public(r) for r in self.rows(hotkey)]
        by_setup: dict[str, dict[str, Any]] = {}
        for r in results:
            g = by_setup.setdefault(r["setup"], {"runs": 0, "deliverable": 0, "_s": [], "_r": []})
            g["runs"] += 1
            g["deliverable"] += 1 if r["deliverable"] else 0
            if r["score"] is not None and r["reference_score"] is not None:
                g["_s"].append(float(r["score"]))
                g["_r"].append(float(r["reference_score"]))
        for g in by_setup.values():
            s, ref = g.pop("_s"), g.pop("_r")
            g["voted"] = len(s)
            g["mean_score"] = round(statistics.fmean(s), 2) if s else None
            g["mean_reference_score"] = round(statistics.fmean(ref), 2) if ref else None
        return {
            "hotkey": hotkey,
            "open": list(open_jobs or []),
            "results": results,
            "by_setup": by_setup,
            "note": "practice is never scored: it changes no points, weight, routing or pay. The "
            "delivered version had its client's answers; a practice run has none. Switch a client "
            "setup only after several results, and watch its first client jobs.",
        }

    @staticmethod
    def _public(r: Mapping[str, Any]) -> dict[str, Any]:
        """A record as its miner reads it (never the reference job's id nor the workspace)."""
        return {
            "practice": r.get("practice_id"),
            "title": r.get("title") or "",
            "setup": r.get("setup") or "",
            "state": "reported" if r.get("state") == "done" else "judging",
            "deliverable": bool(r.get("deliverable")),
            "why": r.get("why") or "",
            "score": r.get("score"),
            "reference_score": r.get("reference_score"),
            "unscored": r.get("why_unscored") or "",
            "judge": r.get("judge") or "",
            "minutes": r.get("minutes"),
            "on_time": r.get("on_time"),
            "ended_ms": r.get("ended_ms"),
        }

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
                if r.get("state") in ("judging", "reporting") and int(r.get("next_ms") or 0) <= now
            ]
            if not due:
                return "idle"
            row = dict(due[0])
            if row["state"] == "judging":
                return self._judge(row)
            return self._report(row)
        finally:
            self._busy.release()

    def _later(self, row: dict[str, Any], why: str, *, count: bool, wait_s: float) -> str:
        if count:
            row["tries"] = int(row.get("tries") or 0) + 1
        row["why_unscored"] = why[:300]
        if count and row["tries"] >= MAX_TRIES:
            # no vote after this many tries: reported without one
            row.update(state="reporting", tries=0, next_ms=self.now_ms())
            self.kv.put(NS, row["practice_id"], row)
            return "unscored"
        wait = wait_s or min(RETRY_MAX_S, RETRY_FIRST_S * 2 ** max(0, int(row["tries"]) - 1))
        row["next_ms"] = self.now_ms() + int(wait * 1000)
        self.kv.put(NS, row["practice_id"], row)
        return "pending"

    def _judge(self, row: dict[str, Any]) -> str:
        rv = self.reviewer
        if rv is None or not getattr(rv, "active", False):
            row.update(state="reporting", why_unscored="the validator makes no votes")
            self.kv.put(NS, row["practice_id"], row)
            return self._report(row)
        row["judge"] = str(rv.judge_cfg.version)
        try:
            if row.get("score") is None:
                if not row.get("frozen"):
                    # the bundle is made from a frozen workspace: the validator's own copy
                    self.platform.freeze(row["workspace"])
                    row["frozen"] = True
                    self.kv.put(NS, row["practice_id"], row)
                got = self.platform.practice_bundle(row["practice_id"], row["workspace"])
                vote = self._vote(rv, got, row)
                row.update(score=vote["score"], value=vote["value"], samples=vote["samples"])
                self.kv.put(NS, row["practice_id"], row)
            if row.get("reference_score") is None:
                ref = self.reference_vote(str(row.get("reference_job") or ""))
                row["reference_score"] = ref["score"]
                row["reference_cached"] = bool(ref.get("cached"))
        except _LaterError as e:
            return self._later(row, str(e), count=e.count, wait_s=e.wait_s)
        except PlatformError as e:
            return self._later(row, f"bundle: {e.status} {e.code} {e}", count=True, wait_s=0.0)
        row.update(state="reporting", tries=0, why_unscored="", next_ms=self.now_ms())
        self.kv.put(NS, row["practice_id"], row)
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
            return dict(rv.judge_bundle(got))
        except PlatformError:
            raise
        except Exception as e:  # noqa: BLE001 -- the provider, a bundle: retried, counted
            raise _LaterError(f"vote: {type(e).__name__}: {str(e)[:200]}") from None

    def reference_vote(self, reference_job: str) -> dict[str, Any]:
        """Today's judge on the delivered version of the brief (``reference_job``), cached per
        (reference job, judge digest): later practice on the same brief reuses it."""
        rv = self.reviewer
        if not reference_job:
            raise _LaterError("the practice brief named no delivered version", count=True)
        key = f"{reference_job}|{rv.digest}"
        cached = self.kv.get(REF_NS, key)
        if isinstance(cached, dict) and cached.get("score") is not None:
            return {**cached, "cached": True}
        # the "practice" copy: the reference without the pictures a practice bundle never has
        # (its studio render, the client's photos), so both sides are judged on the same kinds of pictures
        got = self.platform.review_bundle(reference_job, "practice")
        vote = self._vote(rv, got, {"ended_ms": self.now_ms()})
        row = {
            **vote,
            "reference_job": reference_job,
            "judge": rv.digest,
            "at_ms": self.now_ms(),
        }
        self.kv.put(REF_NS, key, row)
        self.log(f"practice: reference {reference_job} voted {vote['score']} (cached)")
        return {**row, "cached": False}

    def _report(self, row: dict[str, Any]) -> str:
        body = {
            "hotkey": row.get("hotkey"),
            "setup": row.get("setup") or "",
            "deliverable": bool(row.get("deliverable")),
            "why": row.get("why") or "",
            "score": row.get("score"),
            "reference_score": row.get("reference_score"),
            "judge": row.get("judge") or None,
            "watt": row.get("watt") or None,
            "minutes": row.get("minutes"),
        }
        try:
            self.platform.practice_result(row["practice_id"], body)
        except PlatformError as e:
            final = e.status in (400, 403, 404, 409, 410, 422)
            row["tries"] = int(row.get("tries") or 0) + 1
            if final or row["tries"] >= REPORT_TRIES:
                row["report_error"] = f"{e.status} {e.code} {str(e)[:200]}"
                self.log(f"practice {row['practice_id']}: report refused: {row['report_error']}")
            else:
                wait = min(RETRY_MAX_S, RETRY_FIRST_S * 2 ** max(0, row["tries"] - 1))
                row["next_ms"] = self.now_ms() + int(wait * 1000)
                self.kv.put(NS, row["practice_id"], row)
                return "pending"
        row.update(state="done", reported_ms=self.now_ms(), next_ms=0)
        self.kv.put(NS, row["practice_id"], row)
        ws = str(row.get("workspace") or "")
        if ws:
            try:
                self.platform.delete(ws)
            except PlatformError as e:
                self.log(f"practice {row['practice_id']}: its copy not deleted: {e}")
        self.log(
            f"practice {row['practice_id']} reported: deliverable {bool(row.get('deliverable'))}, "
            f"score {row.get('score')}, reference {row.get('reference_score')}"
        )
        return "reported"
