"""Our validator's votes (DESIGN.md section 13): every delivered job is reviewed once by the
configured vision model, on the published rubric (:mod:`.review`), from the platform's review
bundle.

**Modes** (``--votes``):

- ``counted`` (the default): the vote enters the score as the baseline ``B = u*V + (1 - u)*I``
  with ``u`` from :func:`.review.aggregate` (0.4, our vote alone or with third parties), so it
  moves the weights; a client's stars still outweigh it. ``live`` is the same mode's old name;
- ``shadow``: votes are made, recorded, published in the hourly report and shown to miners, and
  enter the score with ``u = 0`` -- so they change no score and no weight;
- ``off``: nothing is reviewed.

A switch of mode (a restart with another ``--votes``) re-applies every recorded vote
(:meth:`Reviewer.apply_all`), so the window's scores follow the mode at once.

**The queue.** The desk tells the reviewer each delivery the platform took and charged
(:attr:`.desk.Desk.on_delivered`). One review runs at a time (:meth:`Reviewer.pass_once`, called
by the daemon's review thread): ask the platform for the bundle (it may still be rendering: asked
again later), fetch it from its signed link, check its digests, ask the model for its samples,
sign the vote with the validator's hotkey and record it; the job's score is updated as a late
signal's is. A failure on our side (the platform, the model, the spend cap) is retried with a
back-off; an item older than the scoring window is dropped.

**Spend.** :class:`.models.Spend` caps the model's daily spend: past it no call is made that day
and an ALERT is logged once.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .platform import PlatformError
from .review import (
    MAX_BUNDLE_BYTES,
    RUBRIC_VERSION,
    SAMPLES,
    BundleError,
    Vote,
    Voter,
    aggregate,
    ask_samples,
    read_bundle,
    rubric_digest,
    vote_from,
)
from .scoring import HOUR_MS, WINDOW_H

MODES = ("off", "shadow", "counted")
ALIASES = {"live": "counted"}
"""older names of a mode, still accepted"""
MIN_SAMPLES = 2
"""valid samples a vote needs (of :data:`.review.SAMPLES` asked)"""
MAX_TRIES = 8
RETRY_FIRST_S = 60.0
RETRY_MAX_S = 3600.0
BUNDLE_WAIT_S = 24 * 3600.0
"""how long a delivery's bundle may stay unready before its review is dropped"""


class _Kv:
    """The store's namespaces, or memory without a store (tests)."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.mem: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()

    def put(self, ns: str, k: str, v: Any) -> None:
        if self.store is not None:
            self.store.put(ns, k, v)
        else:
            with self.lock:
                self.mem.setdefault(ns, {})[k] = v

    def get(self, ns: str, k: str) -> Any:
        if self.store is not None:
            return self.store.get(ns, k)
        with self.lock:
            return self.mem.get(ns, {}).get(k)

    def items(self, ns: str) -> dict[str, Any]:
        if self.store is not None:
            return dict(self.store.items(ns))
        with self.lock:
            return dict(self.mem.get(ns, {}))

    def delete(self, ns: str, k: str) -> None:
        if self.store is not None:
            self.store.delete(ns, k)
        else:
            with self.lock:
                self.mem.get(ns, {}).pop(k, None)


class Reviewer:
    def __init__(
        self,
        ledger: Any,
        platform: Any,
        model: Any,
        *,
        me: str,
        signer: Any = None,
        mode: str = "counted",
        samples: int = SAMPLES,
        spend: Any = None,
        store: Any = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        mode = ALIASES.get(mode, mode)
        if mode not in MODES:
            raise ValueError(f"votes mode is one of {MODES}")
        self.ledger, self.platform, self.model = ledger, platform, model
        self.me, self.signer, self.mode, self.samples = me, signer, mode, samples
        self.spend, self.clock = spend, clock
        self.log = log or (lambda _line: None)
        self.kv = _Kv(store)
        self.rubric = rubric_digest()
        self._busy = threading.Lock()

    @property
    def active(self) -> bool:
        return self.mode != "off" and self.model is not None

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    # -- the queue ---------------------------------------------------------------------------------

    def enqueue(self, job: str, hotkey: str) -> None:
        if self.mode == "off" or not job:
            return
        if self.kv.get("review", job) is not None:
            return
        now = self.now_ms()
        self.kv.put(
            "review",
            job,
            {
                "job": job,
                "hotkey": hotkey,
                "state": "pending",
                "tries": 0,
                "next_ms": now,
                "queued_ms": now,
                "why": "",
            },
        )

    def queue(self) -> list[dict[str, Any]]:
        return sorted(self.kv.items("review").values(), key=lambda r: r["queued_ms"])

    def _later(
        self, row: dict[str, Any], why: str, *, count: bool = True, wait_s: float = 0.0
    ) -> str:
        if count:
            row["tries"] = int(row["tries"]) + 1
        row["why"] = why[:300]
        if count and row["tries"] >= MAX_TRIES:
            row["state"] = "failed"
            self.log(f"review of {row['job']} dropped after {row['tries']} tries: {why[:200]}")
        else:
            wait = wait_s or min(RETRY_MAX_S, RETRY_FIRST_S * 2 ** max(0, int(row["tries"]) - 1))
            row["next_ms"] = self.now_ms() + int(wait * 1000)
        self.kv.put("review", row["job"], row)
        return row["state"]

    def pass_once(self) -> str:
        """Review the oldest due delivery, if any. Answers what happened, in a word."""
        if not self.active or not self._busy.acquire(blocking=False):
            return "idle"
        try:
            now = self.now_ms()
            due = [r for r in self.queue() if r["state"] == "pending" and r["next_ms"] <= now]
            if not due:
                return "idle"
            row = dict(due[0])
            if now - int(row["queued_ms"]) > WINDOW_H * HOUR_MS:
                row["state"] = "expired"
                self.kv.put("review", row["job"], row)
                return "expired"
            return self._review(row)
        finally:
            self._busy.release()

    def _review(self, row: dict[str, Any]) -> str:
        job = row["job"]
        if self.spend is not None and not self.spend.allow():
            return self._later(row, "the daily spend cap is reached", count=False, wait_s=3600)
        try:
            got = self.platform.review_bundle(job, "ours")
        except PlatformError as e:
            return self._later(row, f"bundle: {e.status} {e.code} {e}")
        if not got.get("ready"):
            if self.now_ms() - int(row["queued_ms"]) > BUNDLE_WAIT_S * 1000:
                return self._later(row, f"no bundle after {BUNDLE_WAIT_S / 3600:g} h")
            wait = float(got.get("retry_s") or RETRY_FIRST_S)
            return self._later(
                row,
                f"bundle {got.get('state') or 'not ready'}",
                count=False,
                wait_s=min(RETRY_MAX_S, max(15.0, wait)),
            )
        try:
            data = self.platform.fetch_bundle(str(got.get("bundle_url") or ""), MAX_BUNDLE_BYTES)
            bundle = read_bundle(data)
        except (PlatformError, BundleError, ValueError) as e:
            return self._later(row, f"bundle unreadable: {e}")
        if got.get("bundle_sha256") and got["bundle_sha256"] != bundle.bundle_sha256:
            return self._later(row, "the bundle's bytes are not the digest the platform named")
        if got.get("image_set") and got["image_set"] != bundle.image_set:
            return self._later(row, "the bundle's image set is not the one the platform named")
        try:
            samples, failed = ask_samples(
                self.model, bundle, samples=self.samples, spend=self.spend
            )
        except Exception as e:  # noqa: BLE001 -- the provider (network, quota): our side, retried
            return self._later(row, f"model: {type(e).__name__}: {str(e)[:200]}")
        if len(samples) < min(MIN_SAMPLES, self.samples):
            capped = any("spend cap" in f for f in failed)
            return self._later(
                row,
                f"{len(samples)} valid sample(s): {'; '.join(failed)[:250]}",
                count=not capped,
                wait_s=3600 if capped else 0.0,
            )
        vote = Vote(
            review=job,
            image_set=bundle.image_set,
            rubric=self.rubric,
            model=str(getattr(self.model, "name", "") or "unknown"),
            validator=self.me,
            samples=[s.as_json() for s in samples],
            at_ms=self.now_ms(),
        )
        if self.signer is not None:
            vote.sig = "0x" + bytes(self.signer.sign(vote.payload())).hex()
        self.kv.put(
            "vote",
            f"{job}|{self.me}",
            {**vote.as_json(), "job": job, "hotkey": row["hotkey"], "unavailable": failed},
        )
        row.update(state="done", why="", done_ms=self.now_ms())
        self.kv.put("review", job, row)
        self.apply(job)
        return "voted"

    # -- what a vote does --------------------------------------------------------------------------

    def votes_of(self, job: str) -> list[dict[str, Any]]:
        return [v for k, v in self.kv.items("vote").items() if k.split("|", 1)[0] == job]

    def standing_of(self, job: str) -> dict[str, Any]:
        """``{V, u, u_applied, count, shadow}`` of a job's votes (u_applied: 0 unless counted)."""
        rows = self.votes_of(job)
        voters = [
            Voter(
                coldkey="ours" if r["validator"] == self.me else f"hotkey:{r['validator']}",
                hotkey=r["validator"],
                value=float(r["value"]),
                ours=r["validator"] == self.me,
                noisy=bool(r.get("noisy")),
            )
            for r in rows
            if r.get("value") is not None and not r.get("abstain")
        ]
        v, u = aggregate(voters)
        applied = u if self.mode == "counted" and v is not None else 0.0
        return {
            "V": v,
            "u": u,
            "u_applied": applied,
            "count": len(voters),
            "shadow": self.mode != "counted",
        }

    def apply(self, job: str) -> None:
        st = self.standing_of(job)
        with contextlib.suppress(Exception):
            self.ledger.vote(job, st["V"], st["u_applied"])

    def apply_all(self) -> int:
        """Apply every recorded vote again under the current mode (at start: a vote recorded in
        shadow counts once the mode is ``counted``, and the other way round). Answers how many
        jobs were applied."""
        jobs = sorted({str(v.get("job") or "") for v in self.kv.items("vote").values()} - {""})
        for job in jobs:
            self.apply(job)
        return len(jobs)

    # -- publication -------------------------------------------------------------------------------

    def report(self, jobs: Iterable[str]) -> dict[str, Any]:
        """The hourly report's ``votes``: the mode, the rubric, every vote on a window job in its
        public form (scores and signature; never the words), and each job's ``V`` and ``u``."""
        wanted = set(jobs)
        items = [
            vote_from(v).public() | {"job": v["job"]}
            for v in self.kv.items("vote").values()
            if v.get("job") in wanted
        ]
        per_job = {j: self.standing_of(j) for j in sorted({i["job"] for i in items})}
        return {
            "mode": self.mode,
            "rubric": self.rubric,
            "rubric_version": RUBRIC_VERSION,
            "model": str(getattr(self.model, "name", "") or ""),
            "items": sorted(items, key=lambda i: (i["at_ms"], i["job"])),
            "per_job": per_job,
            "queue": {
                state: sum(1 for r in self.queue() if r["state"] == state)
                for state in ("pending", "done", "failed", "expired")
            },
            "spend": self.spend.today() if self.spend is not None else {},
        }

    def forget_before(self, before_ms: int) -> None:
        """Drop the queue rows and votes of deliveries older than ``before_ms`` (past the window:
        their votes are in the published reports)."""
        for k, row in self.kv.items("review").items():
            if int(row.get("queued_ms") or 0) < before_ms and row.get("state") != "pending":
                self.kv.delete("review", k)
        for k, row in self.kv.items("vote").items():
            if int(row.get("at_ms") or 0) < before_ms:
                self.kv.delete("vote", k)


def full_votes(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Votes as a miner sees them on its own job (``/v1/miners/stats``): every sample with its
    evidence."""
    out = []
    for r in rows:
        out.append(
            {
                "validator": r.get("validator"),
                "model": r.get("model"),
                "rubric": r.get("rubric"),
                "image_set": r.get("image_set"),
                "at_ms": r.get("at_ms"),
                "value": r.get("value"),
                "noisy": bool(r.get("noisy")),
                "samples": list(r.get("samples") or []),
            }
        )
    return out
