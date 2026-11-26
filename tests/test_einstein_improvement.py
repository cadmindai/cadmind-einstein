"""Improvement jobs (DESIGN.md "Open lanes"): a free miner of the improvement lane is served a
delivered device a judge scored low; it may reject the device; the try is gated as any job, then
the judge compares it with the delivered version and only a result a level higher is offered to
the client. The try's outcome follows the platform's answer, and the client's answer to the offer
arrives as a dated signal.

And the platform's refusals of a client job's delivered result for what the builder did
(``parts_dropped``, ``change_unfounded``): the attempt fails, counted, and the job goes to another
miner with the platform's words in the builder's failures."""

from __future__ import annotations

import json
import random
import unittest
from types import SimpleNamespace
from typing import Any

from cadmind_einstein.agent_miner import outcome
from cadmind_einstein.desk import IMPROVEMENT_ASK_S, IMPROVEMENT_PAUSE_S, Desk, DeskError, Job
from cadmind_einstein.improvement import RETRY_FIRST_S
from cadmind_einstein.models import Prices, Spend
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.scoring import HOUR_MS, JobOutcome, Ledger, changes_of
from cadmind_einstein.stats import MinerStats
from cadmind_einstein.votes import Reviewer, _Kv, judge_notes
from tests.test_einstein import GOOD, A, B, C, Clock, wait_state
from tests.test_einstein_votes_stats import VALIDATOR, BundlePlatform, FakeModel

DELIVERED = {
    "brief/brief.md": "A tray under a desk for cables.",
    "spec/sources.toml": "[source.pla]\nurl = 'x'\n",
    "spec/requirements.toml": "[requirement.holds]\nstatement = 'holds the cables'\n",
    "tests/test_tray.py": "def test_holds():\n    pass\n",
    "src/parts/tray.py": "\n".join(f"line {i} of the tray as delivered" for i in range(40)),
}
BETTER = {"src/parts/clip.py": "\n".join(f"line {i} of a clip that holds" for i in range(40))}
FINDING = {"id": "U2", "line": "the tray sags", "quote": "1 mm wall", "nudge": "thicken it"}


class ImprovementPlatform(BundlePlatform):
    """The bundle platform plus the improvement routes, and a result route that can refuse."""

    def __init__(self) -> None:
        super().__init__()
        self.devices: list[dict[str, Any]] = []
        self.copies: dict[str, str] = {}  # device -> the operator's copy of its delivery
        self.open: dict[str, dict[str, Any]] = {}  # hotkey -> its open try
        self.asked_next: list[str] = []
        self.next_error: PlatformError | None = None
        self.improvement_grants: list[dict[str, Any]] = []
        self.grant_errors: list[PlatformError] = []
        self.rejects: list[tuple[str, str]] = []
        self.bundled: list[tuple[str, str]] = []
        self.results: dict[str, list[dict[str, Any]]] = {}
        self.result_errors: list[PlatformError] = []
        self.refuse: list[PlatformError] = []
        self.n_try = 0

    def device(
        self,
        job: str = "dev-1",
        *,
        level: int | None = 1,
        files: dict[str, str] | None = None,
        **kw: Any,
    ) -> str:
        """A delivered version on the lane; answers its job id."""
        self.copies[job] = self._new(
            files=dict(files or DELIVERED), frozen=True, hotkey="validator"
        )
        self.devices.append({"job": job, "level": level, **kw})
        return job

    def improvement_next(self, hotkey: str) -> dict[str, Any]:
        self.asked_next.append(hotkey)
        if self.next_error is not None:
            raise self.next_error
        if hotkey in self.open:
            return {**self.open[hotkey], "existing": True}
        if not self.devices:
            raise PlatformError("no device is left for you", 404, "no_device")
        d = self.devices.pop(0)
        self.n_try += 1
        self.open[hotkey] = {
            "try_id": f"imp-{self.n_try:012x}",
            "job": d["job"],
            "brief": "A tray under a desk for cables.",
            "level": d["level"],
            "stars": None,
            "finding": d.get("finding"),
            "lineage": d.get("lineage", []),
        }
        return {**self.open[hotkey], "existing": False}

    def grant_improvement(self, hotkey: str, try_id: str) -> tuple[str, str]:
        if self.grant_errors:
            raise self.grant_errors.pop(0)
        files = self.ws[self.copies[self.open[hotkey]["job"]]]["files"]
        ws = self._new(hotkey=hotkey, files=dict(files))
        reference = self._new(hotkey=hotkey, files=dict(files), frozen=True)
        self.improvement_grants.append({"hotkey": hotkey, "try": try_id, "workspace": ws})
        return ws, reference

    def _closed(self, try_id: str) -> None:
        for hk in [h for h, t in self.open.items() if t["try_id"] == try_id]:
            del self.open[hk]

    def improvement_reject(self, try_id: str, hotkey: str) -> dict[str, Any]:
        self.rejects.append((try_id, hotkey))
        self._closed(try_id)
        return {"try_id": try_id, "rejected": True}

    def improvement_bundle(self, try_id: str, workspace: str) -> dict[str, Any]:
        self.bundled.append((try_id, workspace))
        assert self.ws[workspace]["frozen"], "the bundle is made from a frozen copy"
        return self.review_bundle(try_id)

    def improvement_result(self, try_id: str, report: dict[str, Any]) -> dict[str, Any]:
        self.results.setdefault(try_id, []).append(dict(report))
        if self.result_errors:
            raise self.result_errors.pop(0)
        self._closed(try_id)
        if report["outcome"] == "offer":
            return {
                "try_id": try_id,
                "offer_id": "off-000000000001",
                "job": "0123456789ab",
                "price": 12,
                "state": "pending",
            }
        return {"try_id": try_id, "recorded": True}

    def complete(self, job: str, result: dict[str, Any]) -> None:
        if self.refuse and result.get("label") == "delivered":
            raise self.refuse.pop(0)
        super().complete(job, result)


class FakeReviewer:
    """A judge whose level of each bundle is scripted by the job (or try) it is of."""

    active = True
    spend = None
    digest = "judge-digest-1"
    me = VALIDATOR.ss58_address

    def __init__(self) -> None:
        self.kv = _Kv(None)
        self.judge_cfg = SimpleNamespace(version="1.3x")
        self.levels: dict[str, int] = {}
        self.default = 2
        self.errors: list[Exception] = []
        self.judged: list[str] = []

    def judge_bundle(self, got: dict[str, Any]) -> dict[str, Any]:
        job = str(got["bundle_url"]).rsplit("/", 1)[1]
        if self.errors:
            raise self.errors.pop(0)
        self.judged.append(job)
        level = self.levels.get(job, self.default)
        return {"score": level, "value": level / 3, "scores": [level] * 3, "samples": 3}


class Rig(unittest.TestCase):
    """Lanes open; A, B and C keyed. B has a production rating; A and C are in the improvement
    lane."""

    max_inflight = 8

    def setUp(self) -> None:
        self.p = ImprovementPlatform()
        self.clock = Clock()
        self.ledger = Ledger(hour_ms=HOUR_MS)
        self.reg = [A, B, C]
        self.said: list[str] = []
        self.desk = Desk(
            self.p,
            self.ledger,
            registered=lambda: self.reg,
            clock=self.clock,
            rng=random.Random(3),  # noqa: S311 -- a test's seed
            log=self.said.append,
            max_inflight=self.max_inflight,
        )
        self.rv = FakeReviewer()
        self.desk.improvement_book.reviewer = self.rv
        for hk in (A, B, C):
            self.desk.agent_key(hk)
        for i in range(10):
            self.ledger.add(
                JobOutcome(
                    job=f"h{i}",
                    hotkey=B,
                    ended_ms=self.desk.now_ms() - HOUR_MS,
                    deliverable=True,
                    kind="pass",
                )
            )
        self.desk.set_lanes_open(True)

    def tearDown(self) -> None:
        self.desk.stop()

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def poll(self, hk: str = A) -> dict[str, Any]:
        """``hk`` polls; whatever the poll started (an ask, a grant) has finished."""
        out = self.desk.work(hk)
        self.assertTrue(self.desk.wait_idle())
        return out

    def served(self, hk: str = A) -> dict[str, Any]:
        """``hk`` polls and is served an improvement job: its ``work`` answer."""
        self.poll(hk)
        w = self.poll(hk)
        self.assertTrue(w["job"], f"nothing served: {self.said[-3:]}")
        return w

    def tried(
        self, hk: str = A, *, deliverable: bool = True, files: dict[str, str] | None = None
    ) -> str:
        """``hk`` is served a device, improves it and submits; answers the try's id."""
        w = self.served(hk)
        self.desk.accept(hk, w["job"])
        self.p.work(w["workspace"], BETTER if files is None else files, deliverable=deliverable)
        self.desk.submit(hk, w["job"], w["workspace"], "d")
        wait_state(self.desk, w["job"], ("done",))
        self.assertTrue(self.desk.wait_idle())
        return str(w["job"])

    def drain(self) -> list[str]:
        out = []
        for _ in range(10):
            got = self.desk.improvement_book.pass_once()
            if got == "idle":
                break
            out.append(got)
        return out

    def outcome_of(self, try_id: str) -> JobOutcome:
        (o,) = [o for o in self.ledger.outcomes if o.job == try_id]
        return o

    def tries(self) -> list[JobOutcome]:
        return [o for o in self.ledger.outcomes if o.lane == "improvement"]

    def offered(self, hk: str = A, device: str = "dev-1") -> str:
        """A try judged a level higher and taken as an offer; answers its id."""
        self.p.device(device)
        self.rv.levels[device] = 1
        tid = self.tried(hk)
        self.rv.levels[tid] = 2
        self.assertEqual(self.drain(), ["reported"])
        return tid


class TestServing(Rig):
    def test_a_free_miner_of_the_improvement_lane_is_served_the_next_device(self) -> None:
        self.p.device("dev-1", level=1, finding=FINDING, lineage=["older-1"])
        first = self.poll(A)
        self.assertEqual((first["job"], first["lane"]), (None, "improvement"))
        w = self.poll(A)
        tid = w["job"]
        self.assertEqual(tid, "imp-000000000001")
        self.assertEqual(
            (w["improvement"], w["no_questions"], w["level"], w["lane"], w["rating"]),
            (True, True, 1, "improvement", 0.0),
        )
        self.assertEqual(w["brief"], "A tray under a desk for cables.")
        grant = self.p.improvement_grants[0]
        self.assertEqual(
            (grant["hotkey"], grant["try"], grant["workspace"]), (A, tid, w["workspace"])
        )
        # the workspace starts as the delivered version; ``base`` is a read-only copy of it
        self.assertEqual(self.p.ws[w["workspace"]]["files"], DELIVERED)
        self.assertEqual(w["base"]["kind"], "delivered")
        self.assertTrue(self.p.ws[w["base"]["workspace"]]["frozen"])
        self.assertNotEqual(w["base"]["workspace"], w["workspace"])
        # the judge's notes: the platform's finding, as one line
        self.assertEqual(w["notes"]["nudges"], ["thicken it"])
        self.assertEqual(
            [(n["line"], n["seen"], n["main"]) for n in w["notes"]["lines"]],
            [("the tray sags", "1 mm wall", True)],
        )
        job = self.desk.jobs[tid]
        self.assertEqual(
            (job.kind, job.target, job.improvement["device"]), ("improvement", A, "dev-1")
        )
        self.assertEqual(job.lineage, ["dev-1", "older-1"])
        # no client grant, no client telemetry, nothing sent to the job queue
        self.assertEqual((self.p.grants, self.desk.assigned, self.desk.trial_count), ([], {}, 0))
        # the same deadline as any job, from its acceptance
        got = self.desk.accept(A, tid)
        self.assertEqual(got["deadline_ms"], self.desk.now_ms() + self.desk.deadline_s * 1000)

    def test_a_production_miner_is_never_served_one(self) -> None:
        self.p.device()
        for _ in range(3):
            w = self.poll(B)
            self.assertEqual((w["job"], w["lane"]), (None, "production"))
        self.assertEqual(self.p.asked_next, [])

    def test_the_notes_are_those_of_the_validators_own_vote_when_it_has_one(self) -> None:
        sample = {
            "score": 1,
            "derived": {"useless_if": [{"id": "U1", "line": "it tips", "main": True, "quote": ""}]},
            "lines": [{"id": "U1", "verdict": "fails", "seen": "a narrow base"}],
            "nudges": [{"text": "widen the base"}],
        }
        self.rv.kv.put("vote", f"dev-1|{self.rv.me}", {"samples": [sample]})
        self.p.device("dev-1", finding=FINDING)
        w = self.served(A)
        self.assertEqual(w["notes"], judge_notes({"samples": [sample]}))
        self.assertEqual(w["notes"]["lines"][0]["seen"], "a narrow base")

    def test_none_is_served_with_the_lanes_closed_without_a_judge_or_on_hold(self) -> None:
        self.p.device()
        self.desk.set_lanes_open(False)
        self.poll(A)
        self.desk.set_lanes_open(True)
        self.desk.improvement_book.reviewer = None  # no judge, no filter
        self.poll(A)
        self.rv.active = False
        self.desk.improvement_book.reviewer = self.rv
        self.poll(A)
        self.rv.active = True
        self.desk.hold = True
        self.poll(A)
        self.assertEqual(self.p.asked_next, [])
        self.desk.hold = False
        self.assertTrue(self.served(A)["improvement"])

    def test_no_device_is_asked_again_only_after_a_minute(self) -> None:
        for _ in range(3):
            self.poll(A)
        self.assertEqual(self.p.asked_next, [A])
        self.poll(C)  # per hotkey
        self.assertEqual(self.p.asked_next, [A, C])
        self.clock.t += IMPROVEMENT_ASK_S + 1
        self.p.device()
        self.assertTrue(self.served(A)["job"])
        self.assertEqual(self.p.asked_next, [A, C, A])

    def test_one_job_at_a_time(self) -> None:
        self.p.device("dev-1")
        self.p.device("dev-2")
        w = self.served(A)
        for _ in range(3):
            self.assertEqual(self.poll(A)["job"], w["job"])
        self.assertEqual(self.p.asked_next, [A])
        self.assertEqual(self.desk.miners[A].job, w["job"])

    def test_improvement_runs_on_spare_places_only(self) -> None:
        self.desk.max_inflight = 1
        self.poll(B)
        self.p.queue.append({"job": "j1", "brief": "A wall hook."})
        self.tick()
        self.assertEqual(self.poll(B)["job"], "j1")
        self.p.device()
        self.poll(A)
        self.poll(A)
        self.assertEqual(self.p.asked_next, [])  # the one place is a client job's

    def test_a_try_the_platform_still_holds_open_is_not_started_twice(self) -> None:
        self.p.device()
        tid = self.tried(A)  # being judged: the platform still answers this try
        self.poll(A)
        self.poll(A)
        self.assertEqual(self.p.asked_next, [A, A])
        self.assertEqual([j.job for j in self.desk.jobs.values() if j.kind == "improvement"], [tid])
        self.assertEqual(len(self.p.improvement_grants), 1)

    def test_a_grant_the_platform_refuses_drops_the_job_and_pauses_the_miner(self) -> None:
        self.p.device()
        self.p.grant_errors.append(
            PlatformError("the device left the lane", 410, "improvement_gone")
        )
        self.poll(A)
        (job,) = [j for j in self.desk.jobs.values() if j.kind == "improvement"]
        self.assertEqual((job.state, job.result["outcome"]), ("done", "none"))
        self.assertEqual((self.desk.miners[A].job, self.tries(), self.p.results), ("", [], {}))
        self.p.open.clear()
        self.p.device("dev-2")
        self.poll(A)
        self.assertEqual(self.p.asked_next, [A])  # asked nothing for ten minutes
        self.clock.t += IMPROVEMENT_PAUSE_S + 1
        self.assertTrue(self.served(A)["job"])

    def test_with_no_production_miner_at_all_a_waiting_client_job_comes_first(self) -> None:
        for o in list(self.ledger.outcomes):
            self.ledger.withdraw(o.job)  # B has no rating either: nobody is in production
        self.p.device()
        self.desk.jobs["j1"] = Job(job="j1", brief="A hook.", state="assigning", granting=B)
        self.desk.jobs["j1"].state = "queued"
        self.desk.work(A)  # no device is asked for while the client job waits
        self.assertEqual(self.p.asked_next, [])
        self.tick()
        self.assertEqual(self.desk.jobs["j1"].state, "assigned")


class TestRejectAndAcceptWindow(Rig):
    def test_a_rejected_device_is_final_for_the_miner_and_counts_for_nothing(self) -> None:
        self.p.device("dev-1")
        self.p.device("dev-2")
        w = self.served(A)
        out = self.desk.reject(A, w["job"], "", "")  # no category, no message needed
        self.assertEqual(out, {"job": w["job"], "state": "done", "rejected": True})
        # free at once, and served another device on its next polls
        self.assertEqual(self.desk.miners[A].job, "")
        self.assertEqual(self.drain(), ["rejected"])
        self.assertEqual(self.p.rejects, [(w["job"], A)])
        self.assertEqual((self.tries(), self.desk.rejected, self.p.results), ([], {}, {}))
        self.assertEqual(self.desk.improvement_book.rejected(A, 0), 1)
        self.assertEqual(self.desk.failures(A), [])
        second = self.served(A)
        self.assertNotEqual(second["job"], w["job"])
        self.assertEqual(self.desk.jobs[second["job"]].improvement["device"], "dev-2")

    def test_a_device_may_be_rejected_while_it_is_worked(self) -> None:
        self.p.device()
        w = self.served(A)
        self.desk.accept(A, w["job"])
        self.desk.reject(A, w["job"], "not-capable", "nothing to improve here")
        self.assertEqual((self.tries(), self.desk.miners[A].job), ([], ""))
        self.assertTrue(self.desk.wait_idle())
        self.assertTrue(self.p.ws[w["workspace"]]["frozen"])
        with self.assertRaises(DeskError) as e:
            self.desk.reject(A, w["job"], "", "")
        self.assertEqual(e.exception.status, 404)

    def test_a_rejection_is_sent_again_until_the_platform_takes_it(self) -> None:
        self.p.device()
        w = self.served(A)
        self.desk.reject(A, w["job"], "", "")
        taken = self.p.improvement_reject
        calls: list[str] = []

        def down(try_id: str, hotkey: str) -> dict[str, Any]:
            calls.append(try_id)
            if len(calls) == 1:
                raise PlatformError("try later", 503, "busy")
            return taken(try_id, hotkey)

        self.p.improvement_reject = down  # type: ignore[method-assign]
        self.assertEqual(self.drain(), ["pending"])
        self.clock.t += RETRY_FIRST_S + 1
        self.assertEqual(self.drain(), ["rejected"])
        self.assertEqual(self.p.rejects, [(w["job"], A)])

    def test_a_missed_acceptance_window_is_no_outcome_and_a_ten_minute_pause(self) -> None:
        self.p.device("dev-1")
        self.p.device("dev-2")
        w = self.served(A)
        self.clock.t += self.desk.accept_s + 1
        self.tick()
        self.assertEqual(self.desk.status(w["job"])["state"], "done")
        self.assertEqual((self.tries(), self.desk.miners[A].job), ([], ""))
        self.assertEqual(self.drain(), ["reported"])
        self.assertEqual(self.p.rejects, [])  # not a rejection: it may be served the device again
        (rep,) = self.p.results[w["job"]]
        self.assertEqual((rep["outcome"], rep["hotkey"]), ("failed", A))
        self.poll(A)
        self.poll(A)
        self.assertEqual(self.p.asked_next, [A])  # asked nothing for ten minutes
        self.clock.t += IMPROVEMENT_PAUSE_S + 1
        self.assertTrue(self.served(A)["job"])

    def test_a_revoked_or_deregistered_miner_leaves_no_outcome(self) -> None:
        self.p.device()
        w = self.served(A)
        self.desk.accept(A, w["job"])
        self.reg.remove(A)
        self.desk.sync_registered(self.reg)
        self.assertEqual(self.desk.status(w["job"])["state"], "done")
        self.assertEqual(self.tries(), [])
        self.drain()
        self.assertEqual(self.p.results[w["job"]][0]["outcome"], "failed")


class TestTheGate(Rig):
    def test_a_try_that_fails_the_gate_scores_zero(self) -> None:
        self.p.device()
        tid = self.tried(A, deliverable=False)
        o = self.outcome_of(tid)
        self.assertEqual(
            (o.kind, o.lane, o.device, o.s, o.w), ("gate", "improvement", "dev-1", 0, 0)
        )
        self.assertEqual(o.parts, {"failed": "gate", "event": "improvement_failed"})
        self.assertEqual(self.ledger.ratings(self.desk.now_ms())[A], 0.0)
        self.assertEqual(self.drain(), ["reported"])
        (rep,) = self.p.results[tid]
        self.assertEqual((rep["outcome"], rep["score"], rep["base_score"]), ("failed", None, None))
        self.assertIn("did not pass the Watt gate", rep["why"])
        self.assertEqual(self.rv.judged, [])  # a failed gate gets no vote
        self.assertEqual(self.desk.failures(A)[0]["kind"], "gate")
        # never sent to another miner by the validator: the platform puts it back on the lane
        self.assertEqual(self.desk.status(tid)["state"], "done")
        self.assertEqual(self.desk.failed, {})  # the client work's gate tally is not touched

    def test_accepted_and_not_delivered_by_the_deadline_scores_zero(self) -> None:
        self.p.device()
        w = self.served(A)
        self.desk.accept(A, w["job"])
        self.clock.t += self.desk.deadline_s + 1
        self.tick()
        wait_state(self.desk, w["job"], ("done",))
        self.assertTrue(self.desk.wait_idle())
        o = self.outcome_of(w["job"])
        self.assertEqual((o.kind, o.parts["event"]), ("timeout", "improvement_failed"))
        self.assertEqual(self.desk.miners[A].job, "")

    def test_a_project_handed_in_unchanged_is_not_an_improvement(self) -> None:
        files = {**DELIVERED, "build/report.json": json.dumps({"digest": "sha256:same"})}
        self.p.device(files=files)
        self.p.digest = lambda ws: "sha256:same"  # type: ignore[method-assign]
        tid = self.tried(A, files={})
        o = self.outcome_of(tid)
        self.assertEqual((o.kind, o.stage, o.s), ("gate", "unchanged", 0.0))
        self.assertIn("nothing was improved", self.desk.failures(A)[0]["why"])

    def test_the_miner_is_free_at_its_submit_and_the_copy_is_kept_for_the_judge(self) -> None:
        self.p.device()
        tid = self.tried(A)
        self.assertEqual(self.desk.miners[A].job, "")
        self.assertEqual(self.tries(), [])  # no outcome before the judge and the platform said
        (row,) = self.desk.improvement_book.rows(A)
        self.assertEqual((row["try_id"], row["state"], row["device"]), (tid, "judging", "dev-1"))
        self.assertIn(row["workspace"], self.p.ws)  # the validator's copy, not deleted

    def test_two_tries_on_one_device_are_not_copies_of_each_other(self) -> None:
        self.p.device("dev-1")
        first = self.tried(A)
        self.drain()
        self.p.device("dev-1")
        second = self.tried(C)  # the same improvement: the natural answer, not a copy
        self.drain()
        self.assertEqual(
            [self.outcome_of(t).copy_of for t in (first, second)], ["", ""], self.said[-5:]
        )
        self.assertEqual(self.desk.copied, {})


class TestTheJudgeFilter(Rig):
    def test_a_level_higher_is_offered_and_is_a_pending_improvement(self) -> None:
        self.p.device("dev-1", level=1)
        self.rv.levels["dev-1"] = 1
        tid = self.tried(A)
        self.rv.levels[tid] = 2
        self.assertEqual(self.drain(), ["reported"])
        # the try, then the delivered version under today's judge, on its compare copy
        self.assertEqual(self.rv.judged, [tid, "dev-1"])
        self.assertEqual(self.p.asked[-1], ("dev-1", "compare"))
        (rep,) = self.p.results[tid]
        (row,) = self.desk.improvement_book.rows(A)
        self.assertEqual(
            rep,
            {
                "hotkey": A,
                "outcome": "offer",
                "workspace": row["workspace"],
                "digest": row["digest"],
                "base_score": 1,
                "score": 2,
                "judge": "1.3x",
                "watt": "",
                "minutes": 0.0,
                "why": "",
            },
        )
        self.assertTrue(rep["workspace"] and rep["digest"])
        o = self.outcome_of(tid)
        self.assertEqual(
            (o.kind, o.passed, o.lane, o.device), ("pass", True, "improvement", "dev-1")
        )
        self.assertEqual((o.s, o.w, o.parts["event"]), (0.5, 0.25, "improvement_pending"))
        self.assertEqual(o.levels, {"base": 1, "result": 2, "judge": "1.3x"})
        self.assertEqual(row["result"]["offer_id"], "off-000000000001")
        # the validator's copy is the offered version now: kept
        self.assertIn(row["workspace"], self.p.ws)
        self.assertNotIn(row["workspace"], self.p.deleted)
        # a judged improvement proves a miner for genesis, as a delivery does
        self.assertEqual((self.desk.improved, self.desk.proven()), ({A: 1}, sorted([A])))
        self.assertEqual(self.desk.delivered, {})

    def test_two_levels_higher_is_offered_too(self) -> None:
        self.p.device()
        self.rv.levels["dev-1"] = 0
        tid = self.tried(A)
        self.rv.levels[tid] = 2
        self.drain()
        self.assertEqual(self.outcome_of(tid).levels["result"], 2)
        self.assertEqual(self.p.results[tid][0]["outcome"], "offer")

    def test_not_a_level_higher_is_no_offer_and_scores_zero(self) -> None:
        self.p.device()
        tid = self.tried(A)  # both judged level 2
        self.assertEqual(self.drain(), ["reported"])
        (rep,) = self.p.results[tid]
        self.assertEqual((rep["outcome"], rep["score"], rep["base_score"]), ("not_better", 2, 2))
        self.assertIn("not a level higher", rep["why"])
        o = self.outcome_of(tid)
        self.assertEqual((o.kind, o.passed, o.s, o.w), ("not_better", False, 0.0, 0.0))
        self.assertEqual(o.parts, {"failed": "not_better", "event": "improvement_failed"})
        (row,) = self.desk.improvement_book.rows(A)
        self.assertIn(row["workspace"], self.p.deleted)  # nothing to keep
        self.assertEqual(self.desk.improved, {})

    def test_the_delivered_versions_vote_is_cached_per_device_and_judge(self) -> None:
        self.p.device("dev-1")
        self.p.device("dev-1")
        self.tried(A)
        self.drain()
        self.tried(C)
        self.drain()
        self.assertEqual(self.rv.judged.count("dev-1"), 1)
        self.rv.digest = "judge-digest-2"  # another judge: voted again
        self.p.device("dev-1")
        self.clock.t += IMPROVEMENT_ASK_S + 1
        self.tried(A)
        self.drain()
        self.assertEqual(self.rv.judged.count("dev-1"), 2)

    def test_a_vote_that_cannot_be_made_is_tried_again_then_given_up_for_nobody(self) -> None:
        self.p.device()
        tid = self.tried(A)
        self.rv.errors = [RuntimeError("the provider is down")] * 20
        got = []
        for _ in range(12):
            got += self.drain()
            self.clock.t += 2 * 3600
        self.assertEqual(got.count("pending"), 7)
        self.assertEqual(got[-1], "reported")
        self.assertEqual(self.tries(), [])  # the validator's side: nobody is scored
        self.assertEqual(self.p.results[tid][0]["outcome"], "failed")
        self.assertIn("could not judge", self.p.results[tid][0]["why"])

    def test_a_bundle_not_ready_yet_waits_without_counting(self) -> None:
        self.p.device()
        self.tried(A)
        self.p.not_ready = 2
        self.assertEqual(self.drain(), ["pending"])
        self.clock.t += 31
        self.assertEqual(self.drain(), ["pending"])
        self.clock.t += 31
        self.assertEqual(self.drain(), ["reported"])

    def test_the_real_judge_votes_both_sides(self) -> None:
        model = FakeModel()  # every sample scores 2: not a level higher
        self.desk.improvement_book.reviewer = Reviewer(
            self.p,
            model,
            me=VALIDATOR.ss58_address,
            mode="shadow",
            clock=self.clock,
            spend=Spend(5.0, Prices(input=1.0, output=1.0), clock=self.clock),
        )
        self.p.device()
        tid = self.tried(A)
        self.assertEqual(self.drain(), ["reported"])
        (rep,) = self.p.results[tid]
        self.assertEqual((rep["outcome"], rep["score"], rep["base_score"]), ("not_better", 2, 2))
        self.assertEqual(self.outcome_of(tid).kind, "not_better")
        self.assertGreater(model.samples, 0)


class TestThePlatformsAnswer(Rig):
    """Each answer to an offer, and the outcome it makes."""

    def answer(self, error: PlatformError | None) -> str:
        self.p.device()
        self.rv.levels["dev-1"] = 1
        tid = self.tried(A)
        self.rv.levels[tid] = 2
        if error is not None:
            self.p.result_errors.append(error)
        self.drain()
        return tid

    def test_200_is_a_pass(self) -> None:
        o = self.outcome_of(self.answer(None))
        self.assertEqual((o.kind, o.parts["event"]), ("pass", "improvement_pending"))

    def test_422_is_not_better(self) -> None:
        tid = self.answer(PlatformError("not a level higher", 422, "not_better"))
        o = self.outcome_of(tid)
        self.assertEqual((o.kind, o.parts["event"]), ("not_better", "improvement_failed"))
        (row,) = self.desk.improvement_book.rows(A)
        self.assertIn(row["workspace"], self.p.deleted)

    def test_409_delivery_is_refused(self) -> None:
        for code in ("delivery_not_of_job", "delivery_digest_mismatch"):
            with self.subTest(code=code):
                self.setUp()
                o = self.outcome_of(self.answer(PlatformError("no", 409, code)))
                self.assertEqual((o.kind, o.parts["event"]), ("refused", "improvement_failed"))
                self.assertEqual(self.desk.improved, {})
                self.tearDown()

    def test_410_and_offer_exists_make_no_outcome(self) -> None:
        for error in (
            PlatformError("the device left the lane", 410, "improvement_gone"),
            PlatformError("an offer is pending", 409, "offer_exists"),
        ):
            with self.subTest(code=error.code):
                self.setUp()
                self.answer(error)
                self.assertEqual(self.tries(), [])  # not the miner's doing
                (row,) = self.desk.improvement_book.rows(A)
                self.assertEqual((row["state"], row["outcome"]), ("done", "none"))
                self.tearDown()

    def test_503_is_sent_again_and_the_same_try_answers_the_same(self) -> None:
        self.p.device()
        self.rv.levels["dev-1"] = 1
        tid = self.tried(A)
        self.rv.levels[tid] = 2
        self.p.result_errors.append(PlatformError("cannot tell now", 503, "delivery_unverified"))
        self.assertEqual(self.drain(), ["pending"])
        self.assertEqual(self.tries(), [])
        self.clock.t += RETRY_FIRST_S + 1
        self.assertEqual(self.drain(), ["reported"])
        self.assertEqual(len(self.p.results[tid]), 2)
        self.assertEqual(self.rv.judged.count(tid), 1)  # voted once
        self.assertEqual(self.outcome_of(tid).kind, "pass")

    def test_an_outcome_is_written_once(self) -> None:
        tid = self.answer(None)
        (row,) = self.desk.improvement_book.rows(A)
        self.desk._improvement_outcome(row, "pass", {"base": 1, "result": 2, "judge": "x"})
        self.assertEqual(len([o for o in self.ledger.outcomes if o.job == tid]), 1)
        self.assertEqual(self.desk.improved, {A: 1})


class TestTheFourSignals(Rig):
    def signal(self, tid: str, kind: str, **more: Any) -> dict[str, Any]:
        self.clock.t += 60
        return self.desk.signal(tid, {"job": tid, "kind": kind, **more})

    def test_accepted(self) -> None:
        tid = self.offered()
        out = self.signal(tid, "accepted")
        self.assertEqual(
            out, {"job": tid, "score": 1.0, "weight": 1.0, "event": "improvement_accepted"}
        )
        self.assertEqual(self.desk.improvements_accepted, {A: 1})
        o = self.outcome_of(tid)
        self.assertEqual(
            [(c["event"], c["d_rating"], c["d_weight"]) for c in changes_of(o)],
            [("improvement_pending", 0.5, 0.25), ("improvement_accepted", 0.5, 0.75)],
        )
        # the accepted version's own signals come with the try's id
        self.assertEqual(self.signal(tid, "download")["event"], "improvement_accepted")
        self.assertEqual(self.signal(tid, "change")["score"], 1.0)
        self.assertEqual(self.signal(tid, "stars", stars=2)["event"], "improvement_disapproved")
        self.assertEqual((o.s, o.w), (0.0, 0.0))
        self.assertEqual(self.signal(tid, "stars", stars=5)["score"], 1.0)
        self.assertEqual(
            self.signal(tid, "refund", quality=True)["event"], "improvement_disapproved"
        )
        self.assertEqual(self.desk.improvements_accepted, {A: 1})  # counted once

    def test_try_again(self) -> None:
        tid = self.offered()
        out = self.signal(tid, "try_again")
        self.assertEqual(
            (out["score"], out["weight"], out["event"]), (0.0, 0.0, "improvement_try_again")
        )
        self.assertEqual(self.ledger.weights(self.desk.now_ms())[A], 0.0)  # 0.25 - 0.25

    def test_not_interested_keeps_the_pending_value(self) -> None:
        tid = self.offered()
        out = self.signal(tid, "not_interested")
        self.assertEqual(
            (out["score"], out["weight"], out["event"]), (0.5, 0.25, "improvement_not_interested")
        )
        self.assertEqual(len(changes_of(self.outcome_of(tid))), 1)  # no change of points

    def test_withdrawn_keeps_the_pending_value(self) -> None:
        tid = self.offered()
        out = self.signal(tid, "withdrawn")
        self.assertEqual(
            (out["score"], out["weight"], out["event"]), (0.5, 0.25, "improvement_withdrawn")
        )

    def test_the_first_answer_is_final(self) -> None:
        tid = self.offered()
        self.signal(tid, "not_interested")
        out = self.signal(tid, "accepted")
        self.assertEqual(out["event"], "improvement_not_interested")
        self.assertEqual(self.desk.improvements_accepted, {})
        self.assertEqual([x["kind"] for x in self.outcome_of(tid).signals], ["not_interested"])

    def test_an_unknown_try_is_404_and_a_wrong_lane_400(self) -> None:
        with self.assertRaises(DeskError) as e:
            self.desk.signal("imp-00000000ffff", {"kind": "accepted"})
        self.assertEqual(e.exception.status, 404)
        self.p.device()
        failed = self.tried(A, deliverable=False)
        with self.assertRaises(DeskError) as e:  # a try that made no offer has none to answer
            self.desk.signal(failed, {"kind": "accepted"})
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(DeskError) as e:  # a client job takes no offer's answer
            self.desk.signal("h0", {"kind": "accepted"})
        self.assertEqual(e.exception.status, 400)
        self.assertEqual(self.desk.signal("h0", {"kind": "download"})["event"], "approved")
        with self.assertRaises(DeskError) as e:
            self.desk.signal("h0", {"kind": "liked"})
        self.assertEqual(e.exception.status, 400)

    def test_a_late_answer_is_paid_in_the_week_it_arrives(self) -> None:
        tid = self.offered()
        self.clock.t += 30 * 24 * 3600  # a month later: out of the rating's window
        now = self.desk.now_ms()
        self.assertEqual(self.ledger.weights(now).get(A, 0.0), 0.0)
        self.desk.signal(tid, {"kind": "accepted"})
        self.assertEqual(self.ledger.weights(now)[A], 0.75)
        self.assertEqual(self.ledger.ratings(now).get(A, 0.0), 0.0)  # the pay, not the rating


class TestStats(Rig):
    def setUp(self) -> None:
        super().setUp()
        self.stats = MinerStats(self.desk, uids=lambda: {A: 1, B: 2, C: 3})

    def doc(self, hk: str) -> dict[str, Any]:
        return self.stats.docs([hk])["miners"][hk]

    def test_a_miners_doc_names_its_lane_rating_weight_changes_and_improvement_work(self) -> None:
        tid = self.offered(A)
        self.clock.t += 60
        self.desk.signal(tid, {"kind": "accepted"})
        self.p.device("dev-2")
        failed = self.tried(A, deliverable=False)
        self.drain()
        self.p.device("dev-3")
        live = self.served(A)
        self.desk.reject(A, live["job"], "", "")
        self.drain()
        self.p.device("dev-4")
        live = self.served(A)
        d = self.doc(A)
        self.assertEqual((d["lane"], d["lanes_open"]), ("improvement", True))
        r = d["rating"]
        self.assertEqual(
            (r["value"], r["threshold"], r["distance"], r["counted"], r["padded"]),
            (0.1, 0.4, -0.3, 2, 8),
        )
        self.assertEqual(
            r["jobs"],
            [
                {
                    "job": failed,
                    "ended_ms": self.outcome_of(failed).ended_ms,
                    "lane": "improvement",
                    "kind": "gate",
                    "event": "improvement_failed",
                    "s": 0.0,
                    "w": 0.0,
                },
                {
                    "job": tid,
                    "ended_ms": self.outcome_of(tid).ended_ms,
                    "lane": "improvement",
                    "kind": "pass",
                    "event": "improvement_accepted",
                    "s": 1.0,
                    "w": 1.0,
                },
            ],
        )
        self.assertEqual(d["weight"], {"points": 1.0, "share": 0.0, "rule": ""})
        # the dated changes, newest first
        self.assertEqual(
            [(c["job"], c["event"]) for c in d["changes"]],
            [
                (failed, "improvement_failed"),
                (tid, "improvement_accepted"),
                (tid, "improvement_pending"),
            ],
        )
        self.assertEqual(
            d["improvement"],
            {
                "open": {"job": live["job"], "device": "dev-4", "level": 1},
                "tries_7d": 2,
                "pending": 0,
                "accepted_7d": 1,
                "rejected_devices_7d": 1,
            },
        )
        # the earlier numbers stay; the client work's counters are not the improvement lane's
        self.assertEqual(d["standing"]["weight"], 1.0)
        self.assertEqual((d["windows"]["7d"]["delivered"], d["windows"]["7d"]["assigned"]), (0, 0))
        self.assertEqual(d["jobs"][0]["lane"], "improvement")
        self.assertIn("admitted", d)

    def test_a_production_miner(self) -> None:
        d = self.doc(B)
        self.assertEqual(
            (d["lane"], d["rating"]["value"], d["rating"]["padded"]), ("production", 0.5, 0)
        )
        self.assertEqual(d["rating"]["distance"], 0.1)
        self.assertEqual(d["improvement"]["open"], None)
        self.assertEqual(d["weight"]["points"], 5.0)
        self.assertEqual(len(d["changes"]), 10)

    def test_the_formula_is_the_table_and_the_reports_constants(self) -> None:
        f = self.stats.formula()
        self.assertEqual(f["constants"]["points"]["improvement_pending"], [0.5, 0.25])
        self.assertEqual(f["constants"]["rating"]["threshold"], 0.4)
        for words in ("0.5 and 0.25", "try again", "not counted at all"):
            self.assertIn(words, f["text"])
        self.assertIn("fewer than 10 jobs counted as 10", f["rating"])

    def test_the_public_leaderboard(self) -> None:
        tid = self.offered(A)
        self.clock.t += 60
        self.desk.signal(tid, {"kind": "accepted"})
        net = self.stats.network()
        self.assertEqual((net["lanes_open"], net["launch_equal"]), (True, False))
        rows = {r["hotkey"]: r for r in net["miners"]}
        self.assertEqual(set(rows), {A, B, C})  # every eligible miner once the lanes are open
        self.assertEqual(
            {
                k: rows[A][k]
                for k in (
                    "lane",
                    "rating",
                    "delivered_7d",
                    "improvements_accepted_7d",
                    "improvements_accepted",
                )
            },
            {
                "lane": "improvement",
                "rating": 0.1,
                "delivered_7d": 0,
                "improvements_accepted_7d": 1,
                "improvements_accepted": 1,
            },
        )
        self.assertEqual(
            (rows[B]["lane"], rows[B]["rating"], rows[B]["delivered_7d"]), ("production", 0.5, 10)
        )
        self.assertEqual((rows[C]["lane"], rows[C]["improvements_accepted"]), ("improvement", 0))


class TestRefusedBuild(Rig):
    """The platform refuses a client job's delivered result for what the builder did."""

    def setUp(self) -> None:
        super().setUp()
        self.desk.set_lanes_open(False)
        for hk in (A, B, C):
            self.desk.miners[hk].admitted = True
        self.reviewer = Reviewer(
            self.p,
            FakeModel(),
            me=VALIDATOR.ss58_address,
            mode="counted",
            clock=self.clock,
            spend=Spend(5.0, Prices(input=1.0, output=1.0), clock=self.clock),
        )
        self.desk.on_delivered = self.reviewer.enqueue

    def deliver_refused(self, code: str) -> tuple[str, str]:
        self.p.refuse.append(
            PlatformError(
                "drive_unit was imported and is no longer, its contract unchanged",
                409,
                code,
            )
        )
        self.p.refuse[-1].detail = {"code": code, "parts": ["drive_unit"]}
        for h in (A, B, C):
            self.desk.work(h)
        self.p.queue.append({"job": "j1", "brief": "A rover."})
        self.tick()
        first, w = next((h, x) for h in (A, B, C) if (x := self.desk.work(h))["job"])
        self.desk.accept(first, "j1")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(first, "j1", w["workspace"], "d")
        # judged, delivered, its result sent at once and refused
        self.assertTrue(self.desk.wait_idle())
        wait_state(self.desk, "j1", ("queued", "done"))
        self.assertTrue(self.desk.wait_idle())
        return first, w["workspace"]

    def test_parts_dropped_and_change_unfounded_fail_the_attempt_and_reassign(self) -> None:
        for code in ("parts_dropped", "change_unfounded"):
            with self.subTest(code=code):
                self.setUp()
                first, _ = self.deliver_refused(code)
                job = self.desk.jobs["j1"]
                self.assertEqual(job.attempts[0].kind, "refused")
                self.assertIn(code, job.attempts[0].outcome)
                self.assertIn("drive_unit", job.attempts[0].outcome)
                self.assertNotIn("j1", self.p.completed)
                self.assertEqual(self.desk.delivered.get(first, 0), 0)
                (o,) = [o for o in self.ledger.outcomes if o.job == "j1"]
                self.assertEqual((o.hotkey, o.kind, o.s), (first, "refused", 0.0))
                (f,) = self.desk.failures(first)
                self.assertEqual(f["kind"], "refused")
                self.assertIn("contract unchanged", f["why"])
                self.assertEqual(self.reviewer.queue(), [], "no vote on a refused delivery")
                # reassigned, from scratch, to another miner
                for h in (A, B, C):
                    self.desk.work(h)
                self.tick()
                self.assertEqual(self.desk.status("j1")["state"], "assigned")
                second = job.attempts[-1].hotkey
                self.assertNotEqual(second, first)
                self.assertEqual(self.p.grants[-1]["brief"], "A rover.")
                w = self.desk.work(second)
                self.desk.accept(second, "j1")
                own = {"src/parts/hook.py": "\n".join(f"rover part {i}" for i in range(40))}
                self.p.work(w["workspace"], {**GOOD, **own}, deliverable=True)
                self.desk.submit(second, "j1", w["workspace"], "d")
                wait_state(self.desk, "j1", ("done",))
                self.tick()
                self.assertEqual(self.p.completed["j1"]["hotkey"], second)
                self.assertEqual(self.desk.delivered[second], 1)
                self.assertEqual(
                    sorted(o.kind for o in self.ledger.outcomes if o.job == "j1"),
                    ["pass", "refused"],
                )
                self.tearDown()

    def test_past_the_attempt_cap_the_job_ends_didnt_finish(self) -> None:
        self.desk.max_attempts = 1
        self.deliver_refused("parts_dropped")
        st = self.desk.status("j1")
        self.assertEqual((st["state"], st["result"]["label"]), ("done", "didn't finish"))
        self.tick()
        self.assertEqual(self.p.completed["j1"]["label"], "didn't finish")


def test_the_submit_carries_the_setup_label() -> None:
    kind, body = outcome("j", "ws", "built it\nDONE abc", setup="opus-v3")
    assert kind == "submit"
    assert body["setup"] == "opus-v3"
    assert "setup" not in outcome("j", "ws", "DONE abc")[1]
    assert json.dumps(body)


def test_judge_notes_keep_the_lines_not_ruled_ok_main_first() -> None:
    sample = {
        "score": 1,
        "derived": {
            "useless_if": [
                {"id": "U1", "line": "it does not hold a phone", "main": False, "quote": ""},
                {"id": "U2", "line": "it tips over", "main": True, "quote": ""},
                {"id": "U3", "line": "it scratches the desk", "main": False, "quote": ""},
            ]
        },
        "lines": [
            {"id": "U1", "verdict": "cannot_tell", "seen": ""},
            {"id": "U2", "verdict": "fails", "seen": "the base is 20 mm deep"},
            {"id": "U3", "verdict": "ok", "seen": "felt pads"},
        ],
        "nudges": [{"text": "widen the base"}, {"text": "void", "void": True}],
    }
    got = judge_notes({"samples": [sample]})
    assert [(n["id"], n["verdict"], n["main"]) for n in got["lines"]] == [
        ("U2", "fails", True),
        ("U1", "cannot_tell", False),
    ]
    assert got["lines"][0]["seen"] == "the base is 20 mm deep"
    assert got["nudges"] == ["widen the base"]
    assert judge_notes({"samples": []}) == {"lines": [], "nudges": []}


if __name__ == "__main__":
    unittest.main()
