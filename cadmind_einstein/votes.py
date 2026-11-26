"""Our validator's votes (DESIGN.md section 13): every delivered job is judged once by the
configured vision model with CADmind's device judge (:mod:`.judge`, the validators' judge in
``rate`` mode), from the platform's review bundle (:mod:`.review`).

**The judge.** A validator votes with the released judge without tools: two-turn samples (one by
default), the
lower median of their 0-3 scores, ``v = V_MAP[median]``. ``--judge 1.2b-tools`` is refused at
start (:func:`.judge.for_validator`): that version reads the delivered project through Watt tools
and is for CADmind's own pipeline and the miner-side reviewer.

**The vote has no role in a client job's score, weight or route** (DESIGN.md section 5: the
client's signals alone decide a job's points). It is recorded, published in the hourly report,
shown to miners and archived for calibration; nothing in :mod:`.scoring` or the router reads it.
The same judge is the filter of the improvement lane (:mod:`.improvement`): a try is offered to
the client only when it is judged at least one level above the delivered version.

**Modes** (``--votes``):

- ``shadow`` (the default): votes are made, recorded and published. ``counted`` and ``live``,
  older modes that scored the vote, are accepted as its names;
- ``off``: nothing is reviewed.

**A delivery waiting for its parts.** A main project delivered at its interface stage (its
sub-projects' interfaces defined, each still a placeholder) is not judged yet: the platform says
so on its bundle (``parts``: ``awaiting``, ``built``, ``total``, ``delivered_ms``) and the vote
waits, asking again every :data:`PARTS_POLL_S`, until every part is built -- the platform then
makes the bundle again with the parts assembled in view, and the device is judged whole -- or
:data:`PARTS_WAIT_S` after its delivery, when it is judged as it stands. A platform that says
nothing of parts gets the vote at once, as before.

**An assembly's parts** (judge 1.3c). The bundle's ``parts.json`` gives each vendored part's own
evidence; to each, the vote adds this validator's own vote on that part -- the delivered job whose
digest is the part's, its latest vote: its value, the lines of the sample its score came from with
their verdicts, and its kept nudges (``judged``) -- so that a function proved inside a part is not
ruled "cannot tell" at the assembly (:func:`part_votes`).

**A failing main line reaches the client** (:func:`main_failure`). A vote whose deciding sample
rules a ``main`` useless_if line ``fails`` is sent to the platform as a finding on that job
(:meth:`.platform.Platform.finding`): the line, its quote of the brief and the nudge raised on it.
The platform shows it to the client as a notice before Assemble; nothing is refused or gated on it.
A finding the platform could not take is sent again on later passes
(:meth:`Reviewer.send_findings`).

**The vote's score reaches the platform** (:func:`vote_out`, :meth:`.platform.Platform.vote`). Each
of our own votes is also sent to the platform as ``{score, value, count, judge_version}``, the
votes recorded before this was sent included (:meth:`Reviewer.send_votes`). The platform shows the
scores averaged per gallery category and gates nothing with them: this gives the vote no role in
scores, weights or routing.

**The queue.** The desk tells the reviewer each delivery the platform took and charged
(:attr:`.desk.Desk.on_delivered`). One review runs at a time (:meth:`Reviewer.pass_once`, called
by the daemon's review thread): ask the platform for the bundle (it may still be rendering: asked
again later), fetch it from its signed link, check its digests, ask the judge for its samples,
sign the vote with the validator's hotkey and record it. A failure on our side (the platform, the
model, the spend cap) is retried with a back-off; an item older than the scoring window is
dropped.

**Spend.** :class:`.models.Spend` caps the model's daily spend: past it no call is made that day
and an ALERT is logged once.

**The archive** (:mod:`.archive`): every vote is also appended to the
job's calibration archive as it is recorded -- the parsed samples, the kept nudges, and every model
conversation made for it with its raw answers, the model that answered, tokens, dollars, latency,
instrument calls and their Watt compute (none for 1.2b), the judge's version, digest, mode and
samples, and the bundle read -- and so are the conversations that made no vote.
:meth:`Reviewer.forget_before` then drops only what the archive holds (a vote recorded before the
archive existed is archived as it is dropped).
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .desk import job_event
from .judge import V_MAP, VALIDATOR_VERSION, Judge, for_validator, inputs_from_bundle
from .platform import PlatformError
from .review import (
    MAX_BUNDLE_BYTES,
    SAMPLES,
    BundleError,
    Vote,
    Voter,
    aggregate,
    read_bundle,
    vote_from,
)
from .scoring import HOUR_MS, WINDOW_H

FINDING_CHARS = 300
"""a finding's line, quote and nudge, each at most this long"""
FINDING_TRIES = 8
VOTE_OUT_TRIES = 8
"""how many times a vote is offered to the platform (:meth:`Reviewer.send_votes`)"""

MODES = ("off", "shadow")
ALIASES = {"live": "shadow", "counted": "shadow"}
"""older names of a mode, still accepted (both scored the vote; nothing does now)"""
MIN_SAMPLES = 2
"""valid samples a vote needs (of those asked): the judge's own minimum"""
MAX_TRIES = 8
RETRY_FIRST_S = 60.0
RETRY_MAX_S = 3600.0
BUNDLE_WAIT_S = 24 * 3600.0
"""how long a delivery's bundle may stay unready before its review is dropped"""
PARTS_WAIT_S = 5 * 24 * 3600.0
"""how long the vote on a delivery that waits for its parts is deferred: past it, the device is
judged as it stands"""
PARTS_POLL_S = 1800.0
"""how often the platform is asked again whether a deferred delivery's parts are built"""


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
        platform: Any,
        model: Any,
        *,
        me: str,
        signer: Any = None,
        mode: str = "shadow",
        samples: int = SAMPLES,
        spend: Any = None,
        store: Any = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
        archive: Any = None,
        judge_version: str = VALIDATOR_VERSION,
    ) -> None:
        mode = ALIASES.get(mode, mode)
        if mode not in MODES:
            raise ValueError(f"votes mode is one of {MODES}")
        self.platform, self.model = platform, model
        self.me, self.signer, self.mode, self.samples = me, signer, mode, samples
        self.spend, self.clock = spend, clock
        self.log = log or (lambda _line: None)
        self.kv = _Kv(store)
        self.archive = archive
        """the calibration archive (:class:`.archive.Archive`), or None"""
        self.judge_cfg = Judge(for_validator(judge_version).name)
        """the device judge votes are made with (a validator never runs the tools judge)"""
        self.digest = self.judge_cfg.digest()
        self._busy = threading.Lock()
        self._votes_queued = False
        """whether the votes recorded before they were sent to the platform have been queued
        (:meth:`Reviewer.queue_unsent_votes`: once per process)"""

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
            self.send_findings()
            self.send_votes()
            now = self.now_ms()
            due = [r for r in self.queue() if r["state"] == "pending" and r["next_ms"] <= now]
            if not due:
                return "idle"
            row = dict(due[0])
            # a vote deferred for its parts may come after the scoring window: it is recorded and
            # published, and scores nothing once the job has left the window
            life_ms = WINDOW_H * HOUR_MS
            if row.get("deferred"):
                life_ms = max(life_ms, int((PARTS_WAIT_S + 2 * BUNDLE_WAIT_S) * 1000))
            if now - int(row["queued_ms"]) > life_ms:
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
        parts = got.get("parts") if isinstance(got.get("parts"), Mapping) else None
        if parts is not None and parts.get("awaiting") is True:
            # delivered at its interface stage: judged once its parts are built, assembled in view
            since = int(parts.get("delivered_ms") or 0) or int(row["queued_ms"])
            if self.now_ms() - since < PARTS_WAIT_S * 1000:
                if not row.get("deferred"):
                    self.log(job_event("vote-deferred", job, hotkey=row["hotkey"], why="parts"))
                row["deferred"] = "parts"
                return self._later(
                    row,
                    f"awaiting its parts: {parts.get('built')} of {parts.get('total')} built",
                    count=False,
                    wait_s=PARTS_POLL_S,
                )
            row["parts"] = "as it stands"
        else:
            if row.get("deferred") and not row.get("parts"):
                row["parts"] = "built"
                row["parts_ms"] = self.now_ms()
            remade_since = int(row.get("parts_ms") or row["queued_ms"])
            if (
                parts is not None
                and parts.get("assembled") is False
                and got.get("ready")
                and self.now_ms() - remade_since < BUNDLE_WAIT_S * 1000
            ):
                # its parts are built and its bundle is made again with them in view: that one is
                # judged
                return self._later(
                    row,
                    "its bundle is made again with its parts",
                    count=False,
                    wait_s=RETRY_FIRST_S,
                )
        if not got.get("ready"):
            waited_from = max(int(row["queued_ms"]), int(row.get("parts_ms") or 0))
            if self.now_ms() - waited_from > BUNDLE_WAIT_S * 1000:
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
        calls: list[dict[str, Any]] = []
        inputs = inputs_from_bundle(bundle)
        judged = self.with_part_votes(inputs.facts)
        try:
            got_vote = self.judge_cfg.run(
                "rate",
                inputs,
                self.model,
                samples=self.samples,
                spend=self.spend,
                calls=calls,
                parallel=False,
            )
        except Exception as e:  # noqa: BLE001 -- the provider (network, quota): our side, retried
            why = f"model: {type(e).__name__}: {str(e)[:200]}"
            self._archive(job, "vote-attempt", row, bundle, got, calls, outcome=why)
            return self._later(row, why)
        failed = list(got_vote["unavailable"])
        samples = list(got_vote["samples"])
        if got_vote["abstain"] or len(samples) < min(MIN_SAMPLES, self.samples):
            capped = any("spend cap" in f for f in failed)
            if calls:
                self._archive(
                    job,
                    "vote-attempt",
                    row,
                    bundle,
                    got,
                    calls,
                    outcome=f"{len(samples)} valid sample(s)",
                    unavailable=failed,
                )
            return self._later(
                row,
                f"{len(samples)} valid sample(s): {'; '.join(failed)[:250]}",
                count=not capped,
                wait_s=3600 if capped else 0.0,
            )
        vote = Vote(
            review=job,
            image_set=bundle.image_set,
            judge=self.digest,
            model=str(getattr(self.model, "name", "") or "unknown"),
            validator=self.me,
            samples=samples,
            at_ms=self.now_ms(),
            judge_version=self.judge_cfg.version,
            nudges=list(got_vote.get("nudges") or []),
        )
        if self.signer is not None:
            vote.sig = "0x" + bytes(self.signer.sign(vote.payload())).hex()
        archived = self._archive(
            job,
            "vote",
            row,
            bundle,
            got,
            calls,
            vote=vote.as_json(),
            unavailable=failed,
            nudge_merge=got_vote.get("nudge_merge"),
            usage=got_vote.get("usage"),
            part_votes=judged,
        )
        self.kv.put(
            "vote",
            f"{job}|{self.me}",
            {
                **vote.as_json(),
                "job": job,
                "hotkey": row["hotkey"],
                "unavailable": failed,
                "cost": cost_of(calls),
                "archived": archived,
                # a delivery that waited for its parts: judged once they were "built", or "as it
                # stands" after PARTS_WAIT_S; '' for any other
                "parts": str(row.get("parts") or ""),
            },
        )
        row.update(state="done", why="", done_ms=self.now_ms())
        self.kv.put("review", job, row)
        finding = main_failure(vote.as_json())
        if finding is not None:
            self.kv.put("finding", job, {"job": job, "finding": finding, "tries": 0, "sent": False})
            self.send_findings()
        out = vote_out(vote.as_json(), int(self.standing_of(job)["count"]))
        if out is not None:
            self.kv.put("voteout", job, {"job": job, "vote": out, "tries": 0, "sent": False})
            self.send_votes()
        self.log(
            job_event(
                "vote-recorded",
                job,
                hotkey=row["hotkey"],
                value=vote.value,
                samples=len(samples),
                unavailable=len(failed),
                mode=self.mode,
            )
        )
        return "voted"

    def with_part_votes(self, facts: dict[str, Any] | None) -> dict[str, str]:
        """Add to each vendored part of the fact sheet (``subprojects``, judge 1.3c) this
        validator's own latest vote on it, as ``judged``. Answers ``{part key: job}`` of the
        votes added; nothing for a judge that does not show the parts."""
        if not self.judge_cfg.spec.parts or not isinstance(facts, dict):
            return {}
        listed = facts.get("subprojects")
        if not isinstance(listed, list) or not listed:
            return {}
        found = part_votes(
            [str(p.get("digest") or "") for p in listed if isinstance(p, Mapping)],
            self.kv.items("job"),
            self.kv.items("vote"),
            self.me,
        )
        added: dict[str, str] = {}
        for p in listed:
            if not isinstance(p, dict):
                continue
            got = found.get(str(p.get("digest") or ""))
            if got is not None:
                p["judged"] = got[1]
                added[str(p.get("key") or "")] = got[0]
        return added

    def send_findings(self) -> int:
        """Send the findings the platform has not taken yet (:func:`main_failure`). A platform
        that refuses one for good (4xx other than 429: an older platform, a job it does not hold)
        or one tried :data:`FINDING_TRIES` times is not tried again. Answers how many it sent."""
        sent = 0
        for job, kept in self.kv.items("finding").items():
            if kept.get("sent") or int(kept.get("tries") or 0) >= FINDING_TRIES:
                continue
            send = getattr(self.platform, "finding", None)
            if send is None:
                return sent
            row = dict(kept)
            row["tries"] = int(row.get("tries") or 0) + 1
            try:
                send(job, row["finding"])
                row["sent"] = True
                sent += 1
                self.log(job_event("finding-sent", job, line=row["finding"].get("id")))
            except PlatformError as e:
                if 400 <= int(e.status or 0) < 500 and int(e.status or 0) != 429:
                    row["tries"] = FINDING_TRIES
                row["why"] = f"{e.status} {e.code}"[:120]
            except Exception as e:  # noqa: BLE001 -- the platform unreachable: tried again later
                row["why"] = f"{type(e).__name__}"[:120]
            self.kv.put("finding", job, row)
        return sent

    def queue_unsent_votes(self) -> int:
        """Queue for the platform every vote of ours that has no ``voteout`` row: the votes
        recorded before they were sent (a vote recorded since is queued as it is recorded). One
        read of the votes; answers how many it queued."""
        held = self.kv.items("voteout")
        votes = self.kv.items("vote")
        counts: dict[str, int] = {}
        for r in votes.values():
            if isinstance(r, Mapping) and r.get("value") is not None and not r.get("abstain"):
                job = str(r.get("job") or "")
                counts[job] = counts.get(job, 0) + 1
        queued = 0
        for k, r in votes.items():
            if not isinstance(r, Mapping) or r.get("validator") != self.me:
                continue
            job = str(r.get("job") or k.split("|", 1)[0])
            if not job or job in held:
                continue
            out = vote_out(r, counts.get(job, 1))
            if out is None:
                continue
            self.kv.put("voteout", job, {"job": job, "vote": out, "tries": 0, "sent": False})
            queued += 1
        return queued

    def send_votes(self) -> int:
        """Send the votes the platform has not taken yet (:func:`vote_out`), those recorded before
        they were sent included (:meth:`queue_unsent_votes`, once per process). The rules are
        :meth:`send_findings`': a platform that refuses one for good (4xx other than 429: an older
        platform, a job it does not hold as a delivered version) or one tried
        :data:`VOTE_OUT_TRIES` times is not tried again. Answers how many it sent."""
        send = getattr(self.platform, "vote", None)
        if send is None:
            return 0
        if not self._votes_queued:
            queued = self.queue_unsent_votes()
            self._votes_queued = True
            if queued:
                self.log(f"votes: {queued} recorded earlier queued for the platform")
        sent = 0
        for job, kept in self.kv.items("voteout").items():
            if kept.get("sent") or int(kept.get("tries") or 0) >= VOTE_OUT_TRIES:
                continue
            row = dict(kept)
            row["tries"] = int(row.get("tries") or 0) + 1
            try:
                send(job, row["vote"])
                row["sent"] = True
                row.pop("why", None)
                sent += 1
                self.log(job_event("vote-sent", job, score=row["vote"].get("score")))
            except PlatformError as e:
                if 400 <= int(e.status or 0) < 500 and int(e.status or 0) != 429:
                    row["tries"] = VOTE_OUT_TRIES
                row["why"] = f"{e.status} {e.code}"[:120]
            except Exception as e:  # noqa: BLE001 -- the platform unreachable: tried again later
                row["why"] = f"{type(e).__name__}"[:120]
            self.kv.put("voteout", job, row)
        return sent

    def judge_bundle(self, got: Mapping[str, Any]) -> dict[str, Any]:
        """One vote's samples on a ready bundle (``got``: the platform's answer naming it), made
        exactly as a delivery's vote is -- the same judge, prompt, rubric and sample count -- and
        recorded nowhere: an improvement try and the delivered version it improves
        (:mod:`.improvement`). Answers
        ``{score, value, scores, samples, image_set, notes}`` (``score``: the 0-3 the vote's value
        is made from; ``notes``: :func:`judge_notes`). Raises ``ValueError`` when the bundle is
        unreadable or not the one named, or too few samples are valid; anything the model raises
        goes through."""
        data = self.platform.fetch_bundle(str(got.get("bundle_url") or ""), MAX_BUNDLE_BYTES)
        try:
            bundle = read_bundle(data)
        except BundleError as e:
            raise ValueError(f"bundle unreadable: {e}") from None
        if got.get("bundle_sha256") and got["bundle_sha256"] != bundle.bundle_sha256:
            raise ValueError("the bundle's bytes are not the digest the platform named")
        if got.get("image_set") and got["image_set"] != bundle.image_set:
            raise ValueError("the bundle's image set is not the one the platform named")
        made = self.judge_cfg.run(
            "rate",
            inputs_from_bundle(bundle),
            self.model,
            samples=self.samples,
            spend=self.spend,
            calls=[],
            parallel=False,
        )
        samples = list(made["samples"])
        if made["abstain"] or len(samples) < min(MIN_SAMPLES, self.samples):
            why = "; ".join(made["unavailable"])[:250]
            raise ValueError(f"{len(samples)} valid sample(s): {why}")
        vote = Vote(
            review="compare",
            image_set=bundle.image_set,
            judge=self.digest,
            model=str(getattr(self.model, "name", "") or "unknown"),
            validator=self.me,
            samples=samples,
            at_ms=self.now_ms(),
            judge_version=self.judge_cfg.version,
        )
        scores = vote.scores
        return {
            "score": statistics.median_low(scores) if scores else None,
            "value": vote.value,
            "scores": scores,
            "samples": len(samples),
            "image_set": bundle.image_set,
            "notes": judge_notes({"samples": samples}),
        }

    def judge(self) -> dict[str, Any]:
        """The judge a vote is made under: what the archive and the platform's stamp name (the
        judge's version and digest, its mode, the samples asked, the model, the votes mode)."""
        return {
            "judge_version": self.judge_cfg.version,
            "judge": self.digest,
            "judge_mode": "rate",
            "model": str(getattr(self.model, "name", "") or ""),
            "samples": self.samples,
            "mode": self.mode,
        }

    def _archive(
        self,
        job: str,
        kind: str,
        row: Mapping[str, Any],
        bundle: Any,
        got: Mapping[str, Any],
        calls: list[dict[str, Any]],
        **extra: Any,
    ) -> bool:
        """One vote (or a vote's calls that made none) into the job's calibration archive."""
        if self.archive is None:
            return False
        try:
            return bool(
                self.archive.append(
                    job,
                    kind,
                    {
                        "validator": self.me,
                        "hotkey": row.get("hotkey"),
                        "tries": row.get("tries"),
                        "queued_ms": row.get("queued_ms"),
                        "parts": row.get("parts"),
                        "judge": self.judge(),
                        "bundle": {
                            "sha256": bundle.bundle_sha256,
                            "image_set": bundle.image_set,
                            "manifest": bundle.manifest,
                            "said": {
                                k: got.get(k) for k in ("bundle_sha256", "image_set", "public")
                            },
                        },
                        "calls": calls,
                        "cost": cost_of(calls),
                        **extra,
                    },
                )
            )
        except Exception as e:  # noqa: BLE001 -- the archive never stops a vote
            self.log(f"archive: {job} {kind}: {type(e).__name__}: {e}")
            return False

    # -- what a vote does --------------------------------------------------------------------------

    def votes_of(self, job: str) -> list[dict[str, Any]]:
        return [v for k, v in self.kv.items("vote").items() if k.split("|", 1)[0] == job]

    def votes_by_job(self) -> dict[str, list[dict[str, Any]]]:
        """Every vote grouped by its job, from one read of the store (``votes_of`` reads all of
        them for one job: a caller asking about many jobs asks this once instead)."""
        out: dict[str, list[dict[str, Any]]] = {}
        for k, v in self.kv.items("vote").items():
            out.setdefault(k.split("|", 1)[0], []).append(v)
        return out

    def standing_of(self, job: str) -> dict[str, Any]:
        """``{V, count}`` of a job's votes: their average and how many (never scored)."""
        return self.standing_from(self.votes_of(job))

    def standing_from(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """``{V, count}`` of these votes (one job's, as :meth:`votes_by_job` groups them)."""
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
        return {"V": aggregate(voters), "count": len(voters)}

    # -- publication -------------------------------------------------------------------------------

    def report(self, jobs: Iterable[str]) -> dict[str, Any]:
        """The hourly report's ``votes``: the mode, the judge, every vote on a window job in its
        public form (scores and signature; never the words), and each job's ``V`` (never
        scored)."""
        wanted = set(jobs)
        items = [
            vote_from(v).public() | {"job": v["job"]}
            for v in self.kv.items("vote").values()
            if v.get("job") in wanted
        ]
        per_job = {j: self.standing_of(j) for j in sorted({i["job"] for i in items})}
        return {
            "mode": self.mode,
            "judge": self.digest,
            "judge_version": self.judge_cfg.version,
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
                if self.archive is not None and row.get("state") != "done":
                    # a delivery that got no vote (failed, expired): the archive says why
                    self.archive.append(str(row.get("job") or k), "review-queue", {"row": row})
                self.kv.delete("review", k)
        for k, row in self.kv.items("vote").items():
            if int(row.get("at_ms") or 0) < before_ms:
                if self.archive is not None and not row.get("archived"):
                    # recorded before the archive was: kept as it is (its raw replies were never
                    # stored), before it goes
                    self.archive.append(
                        str(row.get("job") or k.split("|", 1)[0]),
                        "vote",
                        {
                            "vote": row,
                            "backfill": True,
                            "judge": {
                                "judge": row.get("judge") or row.get("rubric"),
                                "judge_version": row.get("judge_version"),
                            },
                        },
                    )
                self.kv.delete("vote", k)
                if row.get("validator") == self.me:
                    # what was (or was not) sent to the platform of it goes with it
                    self.kv.delete("voteout", str(row.get("job") or k.split("|", 1)[0]))


def deciding_sample(vote: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The sample a vote's score came from: the first whose score is the lower median."""
    samples = [
        s
        for s in vote.get("samples") or []
        if isinstance(s, Mapping) and isinstance(s.get("score"), int)
    ]
    if not samples:
        return None
    low = statistics.median_low([int(s["score"]) for s in samples])
    return next(s for s in samples if int(s["score"]) == low)


def vote_out(vote: Mapping[str, Any], count: int = 1) -> dict[str, Any] | None:
    """A recorded vote as the platform takes it (:meth:`.platform.Platform.vote`): ``{score,
    value, count, judge_version}`` -- ``score`` the lower median of its samples' 0-3 scores (the
    one its value came from; a vote that kept its value alone gives the score :data:`V_MAP` maps
    to that value), ``value`` the vote's, ``count`` how many votes stand on the job (at least 1),
    ``judge_version`` when the vote names one. None for an abstaining vote, one with no value, or
    one whose value is no score's (a v1 vote)."""
    value = vote.get("value")
    if vote.get("abstain") or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not 0 <= value <= 1:
        return None
    scores = [
        int(s["score"])
        for s in vote.get("samples") or []
        if isinstance(s, Mapping)
        and isinstance(s.get("score"), int)
        and not isinstance(s.get("score"), bool)
    ]
    if scores:
        score = statistics.median_low(scores)
    else:
        score = next((k for k, v in V_MAP.items() if abs(v - float(value)) < 1e-9), None)
    if score is None or score not in V_MAP:
        return None
    out: dict[str, Any] = {"score": int(score), "value": float(value), "count": max(1, int(count))}
    version = str(vote.get("judge_version") or "")
    if version:
        out["judge_version"] = version
    return out


def _lines(sample: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A sample's useless_if lines, each with its ruling."""
    derived = sample.get("derived") if isinstance(sample.get("derived"), Mapping) else {}
    rulings = {str(r.get("id")): r for r in sample.get("lines") or [] if isinstance(r, Mapping)}
    out = []
    for u in derived.get("useless_if") or []:
        if not isinstance(u, Mapping):
            continue
        r = rulings.get(str(u.get("id")), {})
        out.append(
            {
                "id": str(u.get("id") or ""),
                "line": str(u.get("line") or ""),
                "main": bool(u.get("main")),
                "quote": str(u.get("quote") or ""),
                "verdict": str(r.get("verdict") or ""),
                "seen": str(r.get("seen") or ""),
            }
        )
    return out


def main_failure(vote: Mapping[str, Any]) -> dict[str, Any] | None:
    """A vote's finding for the client: the first ``main`` useless_if line its deciding sample
    rules ``fails`` -- ``{id, line, quote, nudge, value, judge_version}``, the nudge being the
    sample's (else the vote's) nudge raised on that line or quoting the same words; None when no
    main line fails."""
    sample = deciding_sample(vote)
    if sample is None:
        return None
    failed = next((u for u in _lines(sample) if u["main"] and u["verdict"] == "fails"), None)
    if failed is None:
        return None
    nudges = [n for n in sample.get("nudges") or [] if isinstance(n, Mapping) and not n.get("void")]
    nudge = next((n for n in nudges if str(n.get("line") or "") == failed["id"]), None)
    if nudge is None:
        kept = [n for n in vote.get("nudges") or [] if isinstance(n, Mapping)]
        nudge = next(
            (n for n in nudges + kept if failed["quote"] and n.get("quote") == failed["quote"]),
            None,
        )
    return {
        "id": failed["id"],
        "line": failed["line"][:FINDING_CHARS],
        "quote": failed["quote"][:FINDING_CHARS],
        "nudge": str((nudge or {}).get("text") or "")[:FINDING_CHARS],
        "value": vote.get("value"),
        "judge_version": str(vote.get("judge_version") or ""),
    }


NOTES_MAX = 8
"""lines the judge's notes keep (the main ones first)"""


def judge_notes(vote: Mapping[str, Any]) -> dict[str, Any]:
    """What the judge said of a version, for the miner that improves it (docs/MINER.md
    "Improvement jobs"): the deciding sample's lines of the brief it did not rule ``ok`` -- ``{id,
    line, main, verdict, seen}``, the main lines first, at most :data:`NOTES_MAX` -- and its
    nudges' text. A client delivery's finding, shown to its client, stays :func:`main_failure`.
    Empty lists when no sample decided."""
    sample = deciding_sample(vote)
    if sample is None:
        return {"lines": [], "nudges": []}
    bad = [u for u in _lines(sample) if u["verdict"] and u["verdict"] != "ok"]
    bad.sort(key=lambda u: not u["main"])
    nudges = [
        str(n.get("text") or "")[:FINDING_CHARS]
        for n in sample.get("nudges") or []
        if isinstance(n, Mapping) and not n.get("void") and n.get("text")
    ]
    return {
        "lines": [
            {
                "id": u["id"],
                "line": u["line"][:FINDING_CHARS],
                "main": u["main"],
                "verdict": u["verdict"],
                "seen": u["seen"][:FINDING_CHARS],
            }
            for u in bad[:NOTES_MAX]
        ],
        "nudges": nudges[:NOTES_MAX],
    }


def part_votes(
    digests: Iterable[str],
    jobs: Mapping[str, Any],
    votes: Mapping[str, Any],
    me: str,
) -> dict[str, tuple[str, dict[str, Any]]]:
    """``{digest: (job, judged)}``: for each part digest, this validator's (``me``) vote on the
    latest delivered job whose result is at that digest, as an assembly's judge reads it --
    ``{value, lines, nudges}``, the lines those of the vote's deciding sample with their verdicts.
    A digest no vote of ours is on is left out."""
    wanted = {d for d in digests if d}
    best: dict[str, tuple[int, str]] = {}
    for job, row in jobs.items():
        if not isinstance(row, Mapping):
            continue
        result = row.get("result") if isinstance(row.get("result"), Mapping) else {}
        digest = str(result.get("digest") or "")
        if digest not in wanted or f"{job}|{me}" not in votes:
            continue
        at = int(row.get("created_ms") or 0)
        if digest not in best or at > best[digest][0]:
            best[digest] = (at, str(job))
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for digest, (_, job) in best.items():
        vote = votes[f"{job}|{me}"]
        sample = deciding_sample(vote) if isinstance(vote, Mapping) else None
        if sample is None:
            continue
        out[digest] = (
            job,
            {
                "value": vote.get("value"),
                "judge_version": str(vote.get("judge_version") or ""),
                "lines": [
                    {k: u[k] for k in ("id", "line", "main", "verdict", "seen")}
                    for u in _lines(sample)
                ],
                "nudges": [
                    str(n.get("text") or "")
                    for n in vote.get("nudges") or []
                    if isinstance(n, Mapping) and n.get("text")
                ][:3],
            },
        )
    return out


def cost_of(calls: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """A vote's model conversations, summed: ``{calls, usd, input_tokens, output_tokens,
    cached_tokens, latency_ms, tool_calls, watt_compute_ms}`` (``usd`` None when none was
    priced)."""
    rows = list(calls)
    priced = [float(c["usd"]) for c in rows if isinstance(c.get("usd"), (int, float))]
    return {
        "calls": len(rows),
        "usd": round(sum(priced), 6) if priced else None,
        "input_tokens": sum(int(c.get("input_tokens") or 0) for c in rows),
        "output_tokens": sum(int(c.get("output_tokens") or 0) for c in rows),
        "cached_tokens": sum(int(c.get("cached_tokens") or 0) for c in rows),
        "latency_ms": sum(int(c.get("latency_ms") or 0) for c in rows),
        "tool_calls": sum(len(c.get("tool_calls") or []) for c in rows),
        "watt_compute_ms": sum(int(c.get("watt_compute_ms") or 0) for c in rows),
    }


def full_votes(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Votes as a miner sees them on its own job (``/v1/miners/stats``): every sample with its
    evidence (the derived lines and their rulings) and the kept nudges -- the feedback a builder
    can act on."""
    out = []
    for r in rows:
        out.append(
            {
                "validator": r.get("validator"),
                "model": r.get("model"),
                "judge": r.get("judge") or r.get("rubric"),
                "judge_version": r.get("judge_version"),
                "image_set": r.get("image_set"),
                "at_ms": r.get("at_ms"),
                "value": r.get("value"),
                "noisy": bool(r.get("noisy")),
                "samples": list(r.get("samples") or []),
                "nudges": list(r.get("nudges") or []),
            }
        )
    return out
