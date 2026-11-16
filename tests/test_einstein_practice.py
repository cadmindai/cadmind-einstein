"""Practice jobs: a miner practises on a public brief, gated as any job, voted beside the delivered
version (whose vote is cached per judge), reported to the platform -- and never in the ledger,
points, weights or routing, never handed to another miner, never handed out while a client job
the miner could take waits. And the platform's refusals of a result for what the builder did
(``parts_dropped``, ``change_unfounded``): the attempt fails, counted, and the job goes to another
miner with the platform's words in the builder's failures."""

from __future__ import annotations

import json
import random
import sys
import threading
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bittensor.http_auth as btauth
from bittensor.sp_core import Keypair
from test_einstein import GOOD, A, B, C, Clock, wait_state
from test_einstein_votes_stats import VALIDATOR, BundlePlatform, FakeModel

from cadmind_einstein.agent_miner import outcome, practice_lines
from cadmind_einstein.desk import Desk, DeskError
from cadmind_einstein.fake_miner import signed
from cadmind_einstein.models import Prices, Spend
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.scoring import HOUR_MS, Ledger
from cadmind_einstein.service import serve
from cadmind_einstein.votes import Reviewer, practice_notes


class PracticePlatform(BundlePlatform):
    """The bundle platform plus the practice routes, and a result route that can refuse."""

    def __init__(self) -> None:
        super().__init__()
        self.briefs: list[dict[str, Any]] = []
        self.brief_error: PlatformError | None = None
        self.asked_brief: list[str] = []
        self.practice_grants: list[dict[str, Any]] = []
        self.practice_bundles: list[tuple[str, str]] = []
        self.reports: dict[str, dict[str, Any]] = {}
        self.refuse: list[PlatformError] = []
        self.n_practice = 0
        self.grant_fails = 0

    def practice_brief(self, hotkey: str) -> dict[str, Any]:
        self.asked_brief.append(hotkey)
        if self.brief_error is not None:
            raise self.brief_error
        self.n_practice += 1
        ref = self.briefs[0]["reference_job"] if self.briefs else "ref-1"
        return {
            "practice_id": f"practice-{self.n_practice:012x}",
            "title": "a wall key rack",
            "brief": "A tray under a desk for cables.",
            "reference_job": ref,
        }

    def grant_practice(self, hotkey: str, *, brief: str, name: str) -> str:
        if self.grant_fails > 0:
            self.grant_fails -= 1
            raise PlatformError("busy", 503, "busy")
        self.practice_grants.append({"hotkey": hotkey, "name": name})
        return self._new(hotkey=hotkey, files={"brief/brief.md": brief}, lane="practice")

    def practice_bundle(self, practice_id: str, workspace: str) -> dict[str, Any]:
        self.practice_bundles.append((practice_id, workspace))
        assert self.ws[workspace]["frozen"], "the bundle is made from a frozen copy"
        return self.review_bundle(practice_id)

    def practice_result(self, practice_id: str, report: dict[str, Any]) -> dict[str, Any]:
        self.reports[practice_id] = dict(report)
        return {"ok": True}

    def complete(self, job: str, result: dict[str, Any]) -> None:
        if self.refuse and result.get("label") == "delivered":
            raise self.refuse.pop(0)
        super().complete(job, result)


class Rig(unittest.TestCase):
    per_day = 3

    def setUp(self) -> None:
        self.p = PracticePlatform()
        self.clock = Clock()
        self.ledger = Ledger(hour_ms=HOUR_MS)
        self.desk = Desk(
            self.p,
            self.ledger,
            registered=lambda: [A, B, C],
            clock=self.clock,
            rng=random.Random(3),  # noqa: S311 -- a test's seed
            practice_per_day=self.per_day,
        )
        self.model = FakeModel()
        self.reviewer = Reviewer(
            self.p,
            self.model,
            me=VALIDATOR.ss58_address,
            mode="counted",
            clock=self.clock,
            spend=Spend(5.0, Prices(input=1.0, output=1.0), clock=self.clock),
        )
        self.desk.on_delivered = self.reviewer.enqueue
        self.desk.practice_book.reviewer = self.reviewer
        for hk in (A, B, C):
            self.desk.agent_key(hk)
            self.desk.miners[hk].admitted = True

    def tearDown(self) -> None:
        self.desk.stop()

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def practise(self, hk: str = A, *, deliverable: bool = True, setup: str = "s1") -> str:
        """``hk`` asks for a practice job, builds it and submits; answers its id."""
        for h in (A, B, C):
            self.desk.work(h)
        pid = self.desk.practice(hk)["practice"]
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(hk)
        self.assertEqual((w["job"], w["practice"], w["no_questions"]), (pid, True, True))
        self.desk.accept(hk, pid)
        self.p.work(w["workspace"], GOOD, deliverable=deliverable)
        self.desk.submit(hk, pid, w["workspace"], "d", setup=setup)
        wait_state(self.desk, pid, ("done",))
        self.assertTrue(self.desk.wait_idle())
        return pid

    def drain(self) -> list[str]:
        out = []
        for _ in range(10):
            got = self.desk.practice_book.pass_once()
            if got == "idle":
                break
            out.append(got)
        return out


class TestPractice(Rig):
    def test_practice_stays_open_while_the_subnet_is_on_hold(self) -> None:
        # owner 2026-11-16: the hold holds back client jobs and weights, never practice
        self.desk.hold = True
        pid = self.practise(A, setup="on-hold")
        st = self.desk.practice_status(A)
        self.drain()
        st = self.desk.practice_status(A)
        self.assertEqual([r["practice"] for r in st["results"]], [pid])
        self.assertEqual(st["results"][0]["setup"], "on-hold")
        self.assertIn(pid, self.p.reports)

    def test_the_miner_reads_the_judges_notes(self) -> None:
        pid = self.practise(A)
        self.drain()
        r = self.desk.practice_status(A)["results"][0]
        self.assertEqual(r["practice"], pid)
        self.assertEqual(set(r["notes"]), {"lines", "nudges"})
        for ln in r["notes"]["lines"]:
            self.assertNotEqual(ln["verdict"], "ok")

    def test_end_to_end_voted_beside_the_delivered_version_and_never_scored(self) -> None:
        pid = self.practise(A, setup="  model b  v2 ")
        # never routed to anyone else, granted on the practice lane
        self.assertEqual(self.p.practice_grants, [{"hotkey": A, "name": pid}])
        self.assertEqual(self.p.grants, [], "no client grant")
        self.assertIsNone(self.desk.work(B)["job"])
        # never in the ledger, the weights, the delivered tallies or the platform's job queue
        self.assertEqual(self.ledger.outcomes, [])
        self.assertEqual(self.desk.delivered, {})
        self.assertEqual(self.desk.assigned, {})
        self.assertEqual(self.p.completed, {})
        self.assertNotIn(pid, self.desk._outbox)
        self.assertEqual(self.drain(), ["reported"])
        rep = self.p.reports[pid]
        self.assertEqual(
            (rep["hotkey"], rep["setup"], rep["deliverable"], rep["score"], rep["reference_score"]),
            (A, "model b v2", True, 2, 2),
        )
        self.assertEqual(rep["judge"], self.reviewer.judge_cfg.version)
        self.assertEqual(
            set(rep),
            {
                "hotkey",
                "setup",
                "deliverable",
                "why",
                "score",
                "reference_score",
                "judge",
                "watt",
                "minutes",
            },
        )
        # one vote on the practice result, one on the reference: same sample count as live votes
        self.assertEqual(self.model.samples, 2 * self.reviewer.samples)
        self.assertEqual(self.p.asked, [(pid, "ours"), ("ref-1", "practice")])
        self.assertEqual(self.reviewer.votes_of(pid), [], "a practice vote is recorded nowhere")
        # the validator's copy is deleted once reported
        fork = self.desk.jobs[pid].attempts[-1].fork
        self.assertIn(fork, self.p.deleted)
        st = self.desk.practice_status(A)
        self.assertEqual(st["by_setup"]["model b v2"]["runs"], 1)
        self.assertEqual(st["by_setup"]["model b v2"]["mean_score"], 2.0)
        (row,) = st["results"]
        self.assertNotIn("reference_job", row)
        self.assertTrue(any("yours 2" in line for line in practice_lines(st)))
        self.assertEqual(self.ledger.outcomes, [])

    def test_the_reference_vote_is_cached_per_judge(self) -> None:
        self.practise(A)
        self.drain()
        self.assertEqual(self.model.samples, 2 * self.reviewer.samples)
        pid2 = self.practise(A)
        self.drain()
        self.assertEqual(self.model.samples, 3 * self.reviewer.samples, "the reference reused")
        self.assertEqual([j for j, _ in self.p.asked].count("ref-1"), 1)
        self.assertEqual(self.p.reports[pid2]["reference_score"], 2)
        # another judge digest votes the reference again
        self.reviewer.digest = "sha256:another-judge"
        self.practise(A)
        self.drain()
        self.assertEqual([j for j, _ in self.p.asked].count("ref-1"), 2)

    def test_a_gate_failure_is_reported_without_a_vote(self) -> None:
        pid = self.practise(A, deliverable=False)
        self.assertEqual(self.drain(), ["reported"])
        rep = self.p.reports[pid]
        self.assertEqual(
            (rep["deliverable"], rep["score"], rep["reference_score"]), (False, None, None)
        )
        self.assertTrue(rep["why"].startswith("not deliverable"))
        self.assertEqual(self.model.samples, 0)
        self.assertEqual(self.p.practice_bundles, [])
        self.assertIn(self.desk.jobs[pid].attempts[-1].fork, self.p.deleted)
        # a failed practice costs nothing: no 0, no failure on the receipt, no requeue
        self.assertEqual(self.ledger.outcomes, [])
        self.assertEqual(self.desk.failed, {})
        self.assertEqual(self.desk.failures(A), [])
        self.assertEqual(self.desk.status(pid)["state"], "done")
        self.assertEqual(len(self.desk.jobs[pid].attempts), 1)

    def test_votes_off_reports_without_a_score(self) -> None:
        self.desk.practice_book.reviewer = None
        pid = self.practise(A)
        self.assertEqual(self.drain(), ["reported"])
        self.assertEqual(
            (self.p.reports[pid]["deliverable"], self.p.reports[pid]["score"]), (True, None)
        )

    def test_a_rejection_or_a_missed_accept_ends_it_for_nobody_else(self) -> None:
        for h in (A, B, C):
            self.desk.work(h)
        pid = self.desk.practice(A)["practice"]
        self.desk.wait_idle()
        self.desk.reject(A, pid, "not-capable", "not today")
        self.assertEqual(self.desk.status(pid)["state"], "done")
        self.assertEqual(self.desk.rejected, {}, "practice is in no telemetry")
        self.assertEqual(self.drain(), ["reported"])
        self.assertFalse(self.p.reports[pid]["deliverable"])
        pid = self.desk.practice(A)["practice"]
        self.desk.wait_idle()
        self.clock.t += 600  # never accepted
        self.tick()
        self.assertEqual(self.desk.status(pid)["state"], "done")
        self.assertEqual(self.ledger.outcomes, [])
        self.assertEqual(len(self.desk.jobs[pid].attempts), 1)

    def test_none_is_handed_out_while_a_client_job_it_could_take_is_queued(self) -> None:
        # A's practice grant fails once: its practice job waits in the queue
        self.p.grant_fails = 1
        pid = self.desk.practice(A)["practice"]
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status(pid)["state"], "queued")
        # a client job arrives; only A is alive: it takes the client job, not its practice
        self.clock.t += 120
        self.p.queue.append({"job": "j1", "brief": "A wall hook."})
        self.desk.work(A)
        self.tick()
        self.assertEqual(self.desk.work(A)["job"], "j1")
        self.assertEqual(self.desk.status(pid)["state"], "queued")
        # a client job queued that A could take: no new practice is asked of the platform
        self.desk.cancel("j1")
        self.desk.jobs[pid].state = "done"  # (its earlier practice out of the way)
        self.desk.create({"job": "j2", "brief": "A shelf."})
        asked = len(self.p.asked_brief)
        with self.assertRaises(DeskError) as e:
            self.desk.practice(A)
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(len(self.p.asked_brief), asked)
        # a miner not admitted takes no client job, so it may practise meanwhile
        self.desk.miners[B].admitted = False
        self.assertTrue(self.desk.practice(B)["practice"].startswith("practice-"))

    def test_the_daily_cap_one_open_at_a_time_and_the_platforms_refusals(self) -> None:
        for h in (A, B, C):
            self.desk.work(h)
        first = self.desk.practice(A)["practice"]
        self.assertEqual(self.desk.practice(A)["practice"], first, "one open at a time")
        self.assertEqual(len(self.p.asked_brief), 1)
        for _ in range(self.per_day):
            pid = self.desk.practice(A)["practice"]
            self.desk.wait_idle()
            self.desk.reject(A, pid, "not-capable", "no")
            self.desk.work(A)
        with self.assertRaises(DeskError) as e:
            self.desk.practice(A)
        self.assertEqual(e.exception.status, 429)
        self.assertEqual(self.desk.practice_status(A)["today"], {"started": 3, "cap": 3})
        self.clock.t += 86_400  # the next UTC day
        self.p.brief_error = PlatformError("none left", 404, "no_practice_left")
        with self.assertRaises(DeskError) as e:
            self.desk.practice(A)
        self.assertEqual(e.exception.status, 404)
        self.p.brief_error = PlatformError("cap", 429, "practice_cap")
        with self.assertRaises(DeskError) as e:
            self.desk.practice(A)
        self.assertEqual(e.exception.status, 429)

    def test_an_unapproved_or_keyless_hotkey_cannot_practise(self) -> None:
        self.desk.miners[C].key_id = ""
        with self.assertRaises(DeskError):
            self.desk.practice(C)
        self.assertEqual(self.p.asked_brief, [])


class TestPracticeBeside(Rig):
    """A practice job the miner asks to build beside its client work (``parallel``): it holds no
    job slot, so the miner practises while it serves, and serves while it practises."""

    def client_job(self, job: str = "j1") -> dict[str, Any]:
        """A client job arrives while only A is alive: A is given it and accepts."""
        self.clock.t += 120
        self.p.queue.append({"job": job, "brief": "A wall hook."})
        self.desk.work(A)
        self.tick()
        w = self.desk.work(A)
        self.assertEqual(w["job"], job)
        self.desk.accept(A, job)
        return w

    def test_a_miner_at_work_practises_beside_its_client_job(self) -> None:
        w = self.client_job()
        with self.assertRaises(DeskError) as e:  # the plain request waits for the job's end
            self.desk.practice(A)
        self.assertEqual(e.exception.status, 409)
        pid = self.desk.practice(A, parallel=True)["practice"]
        self.assertTrue(self.desk.wait_idle())
        got = self.desk.work(A)
        self.assertEqual(got["job"], "j1", "its client job stays its job")
        side = got["practice_job"]
        self.assertEqual(
            (side["job"], side["practice"], side["no_questions"], side["accepted"]),
            (pid, True, True, False),
        )
        self.assertEqual(self.p.practice_grants, [{"hotkey": A, "name": pid}])
        self.desk.accept(A, pid)
        self.assertTrue(self.desk.work(A)["practice_job"]["accepted"])
        # the practice result is handed over first: the client job is still A's, untouched
        self.p.work(side["workspace"], GOOD, deliverable=True)
        self.desk.submit(A, pid, side["workspace"], "d", setup="s2")
        wait_state(self.desk, pid, ("done",))
        self.assertTrue(self.desk.wait_idle())
        got = self.desk.work(A)
        self.assertEqual((got["job"], got["accepted"]), ("j1", True))
        self.assertNotIn("practice_job", got)
        self.assertEqual(self.desk.status("j1")["state"], "working")
        self.assertEqual(self.ledger.outcomes, [], "practice is never scored")
        self.assertEqual(self.desk.assigned, {A: 1}, "only the client job is counted")
        # and the client job is delivered as ever
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(A, "j1", w["workspace"], "d")
        wait_state(self.desk, "j1", ("done",))
        self.assertEqual([o.job for o in self.ledger.outcomes], ["j1"])

    def test_a_miner_practising_beside_is_still_given_client_jobs(self) -> None:
        for h in (A, B, C):
            self.desk.work(h)
        pid = self.desk.practice(A, parallel=True)["practice"]
        self.assertTrue(self.desk.wait_idle())
        got = self.desk.work(A)
        self.assertIsNone(got["job"], "a practice job built beside is never the miner's job")
        self.assertEqual(got["practice_job"]["job"], pid)
        self.desk.accept(A, pid)
        self.assertEqual(self.desk.miners[A].job, "")
        self.client_job()
        got = self.desk.work(A)
        self.assertEqual((got["job"], got["practice_job"]["job"]), ("j1", pid))
        # a client job waiting does not stop the request either
        self.desk.reject(A, pid, "not-capable", "no")
        self.desk.create({"job": "j2", "brief": "A shelf."})
        self.assertTrue(self.desk.practice(A, parallel=True)["practice"].startswith("practice-"))
        self.assertEqual(self.desk.rejected, {}, "practice is in no telemetry")

    def test_one_open_at_a_time_and_the_daily_cap_still_hold(self) -> None:
        for h in (A, B, C):
            self.desk.work(h)
        first = self.desk.practice(A, parallel=True)["practice"]
        self.assertEqual(self.desk.practice(A, parallel=True)["practice"], first)
        self.assertEqual(self.desk.practice(A)["practice"], first)
        for _ in range(self.per_day):
            pid = self.desk.practice(A, parallel=True)["practice"]
            self.desk.wait_idle()
            self.desk.reject(A, pid, "not-capable", "no")
            self.desk.work(A)
        with self.assertRaises(DeskError) as e:
            self.desk.practice(A, parallel=True)
        self.assertEqual(e.exception.status, 429)

    def test_a_missed_accept_or_a_disable_ends_it_and_leaves_the_client_job_rules_alone(
        self,
    ) -> None:
        for h in (A, B, C):
            self.desk.work(h)
        pid = self.desk.practice(A, parallel=True)["practice"]
        self.assertTrue(self.desk.wait_idle())
        self.clock.t += 600  # never accepted
        self.tick()
        self.assertEqual(self.desk.status(pid)["state"], "done")
        self.assertEqual(self.ledger.outcomes, [])
        self.desk.work(A)
        pid = self.desk.practice(A, parallel=True)["practice"]
        self.assertTrue(self.desk.wait_idle())
        self.desk.accept(A, pid)
        ws = self.desk.work(A)["practice_job"]["workspace"]
        self.desk.disable(A, reason="test")
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status(pid)["state"], "done")
        self.assertTrue(self.p.ws[ws]["frozen"])
        self.assertEqual(self.ledger.outcomes, [])

    def test_a_restart_keeps_the_job_slot_free(self) -> None:
        for h in (A, B, C):
            self.desk.work(h)
        pid = self.desk.practice(A, parallel=True)["practice"]
        self.assertTrue(self.desk.wait_idle())
        self.desk.accept(A, pid)
        self.desk._recover()
        self.assertEqual(self.desk.miners[A].job, "")
        self.assertEqual(self.desk.work(A)["practice_job"]["job"], pid)

    def beside(self, hk: str = A, *, accept: bool = True) -> tuple[str, str]:
        """``hk`` (every miner alive) asks to practise beside; answers its id and workspace."""
        for h in (A, B, C):
            self.desk.work(h)
        pid = self.desk.practice(hk, parallel=True)["practice"]
        self.assertTrue(self.desk.wait_idle())
        side = self.desk.work(hk)["practice_job"]
        if accept:
            self.desk.accept(hk, pid)
        return pid, side["workspace"]

    def test_a_take_back_of_the_client_job_leaves_the_practice_alone(self) -> None:
        self.client_job()
        pid, _ = self.beside()
        for h in (B, C):
            self.desk.work(h)
        self.desk.release("j1", "the miner cannot work it")
        self.assertTrue(self.desk.wait_idle())
        got = self.desk.work(A)
        self.assertEqual(got["cancelled"], "j1", "told to stop the client job")
        self.assertEqual(got["practice_job"]["job"], pid, "the practice job is still A's")
        self.assertEqual(self.desk.status(pid)["state"], "working")
        self.tick()
        st = self.desk.status("j1")
        self.assertEqual(st["state"], "assigned")
        self.assertNotEqual(self.desk.jobs["j1"].current.hotkey, A)
        # a practice accept does not end what the take-back told A (stop the client job)
        self.assertEqual(self.desk.work(A)["cancelled"], "j1")
        self.assertEqual(self.ledger.outcomes, [])

    def test_a_revoke_or_a_deregistration_ends_it(self) -> None:
        pid, ws = self.beside()
        self.desk.revoke(A, "test")
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status(pid)["state"], "done")
        self.assertTrue(self.p.ws[ws]["frozen"])
        pid, ws = self.beside(B)
        self.desk.sync_registered([A, C])
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status(pid)["state"], "done")
        self.assertTrue(self.p.ws[ws]["frozen"])
        self.assertEqual(self.ledger.outcomes, [])

    def test_a_submitted_one_is_judged_whatever_the_miner_does_next(self) -> None:
        pid, ws = self.beside()
        self.p.work(ws, GOOD, deliverable=True)
        block = threading.Event()
        real = self.p.fork

        def slow(*a: Any, **k: Any) -> Any:  # the gate is still copying the result
            block.wait(5)
            return real(*a, **k)

        self.p.fork = slow  # type: ignore[method-assign]
        self.desk.submit(A, pid, ws, "d", setup="s2")
        self.desk.disable(A, reason="test")
        self.assertEqual(self.desk.status(pid)["state"], "judging")
        block.set()
        wait_state(self.desk, pid, ("done",))
        self.assertTrue(self.desk.jobs[pid].result["deliverable"])

    def test_practice_never_takes_a_place_a_client_job_could_use(self) -> None:
        self.desk.max_inflight = 1
        pid, _ = self.beside()
        self.assertEqual(self.desk.status(pid)["state"], "working")
        self.assertEqual(self.desk._want(self.desk.now_ms()), 1, "its one place is free")
        # a client job is handed out though the practice job fills the one place
        for h in (A, B, C):
            self.desk.work(h)
        self.desk.create({"job": "j1", "brief": "A wall hook."})
        self.tick()
        self.assertEqual(self.desk.status("j1")["state"], "assigned")
        # and a practice job waits while client work fills every place
        self.desk.submit(A, pid, self.desk.work(A)["practice_job"]["workspace"], "d")
        wait_state(self.desk, pid, ("done",))
        holder = self.desk.jobs["j1"].current.hotkey
        other = next(h for h in (A, B, C) if h != holder)
        for h in (A, B, C):
            self.desk.work(h)
        second = self.desk.practice(other, parallel=True)["practice"]
        self.assertTrue(self.desk.wait_idle())
        self.tick()
        self.assertEqual(self.desk.status(second)["state"], "queued")

    def test_on_hold_it_is_handed_out_and_no_client_job_is(self) -> None:
        self.desk.hold = True
        self.desk.create({"job": "j1", "brief": "A wall hook."})
        pid, _ = self.beside()
        self.assertEqual(self.desk.status(pid)["state"], "working")
        self.tick()
        self.assertEqual(self.desk.status("j1")["state"], "queued")

    def test_capacity_counts_a_miner_practising_beside_as_free(self) -> None:
        for h in (A, B, C):
            self.desk.work(h)
        before = self.desk.capacity()
        self.beside()
        self.assertEqual(self.desk.capacity(), before)
        self.assertEqual(self.desk._want(self.desk.now_ms()), 3)


class TestRefusedBuild(Rig):
    """The platform refuses a delivered result for what the builder did."""

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
                (o,) = self.ledger.outcomes
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
                self.assertEqual(sorted(o.kind for o in self.ledger.outcomes), ["pass", "refused"])
                self.tearDown()

    def test_past_the_attempt_cap_the_job_ends_didnt_finish(self) -> None:
        self.desk.max_attempts = 1
        self.deliver_refused("parts_dropped")
        st = self.desk.status("j1")
        self.assertEqual((st["state"], st["result"]["label"]), ("done", "didn't finish"))
        self.tick()
        self.assertEqual(self.p.completed["j1"]["label"], "didn't finish")


class TestPracticeService(unittest.TestCase):
    def test_signed_post_and_get(self) -> None:
        p = PracticePlatform()
        desk = Desk(p, Ledger(), registered=lambda: [A])
        desk.agent_key(A)
        server = serve(
            desk,
            "127.0.0.1",
            0,
            token="t" * 32,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
            signer=VALIDATOR,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        alice = Keypair.create_from_uri("//Alice")
        try:
            code, got = signed(alice, base, "/v1/practice", {}, VALIDATOR.ss58_address)
            self.assertEqual(code, 200, got)
            self.assertTrue(got["practice"].startswith("practice-"))
            code, st = signed(alice, base, "/v1/practice", {}, VALIDATOR.ss58_address, method="GET")
            self.assertEqual(code, 200, st)
            self.assertEqual([o["practice"] for o in st["open"]], [got["practice"]])
            self.assertNotIn("parallel", desk.jobs[got["practice"]].practice)
            self.assertTrue(desk.wait_idle())
            desk.reject(A, got["practice"], "not-capable", "no")  # over: another may be asked
            code, side = signed(
                alice, base, "/v1/practice", {"parallel": True}, VALIDATOR.ss58_address
            )
            self.assertEqual(code, 200, side)
            self.assertIs(desk.jobs[side["practice"]].practice.get("parallel"), True)
            code, _ = signed(
                Keypair.create_from_uri("//Eve"),
                base,
                "/v1/practice",
                {},
                VALIDATOR.ss58_address,
                method="GET",
            )
            self.assertEqual(code, 403, "an unregistered hotkey")
        finally:
            server.shutdown()
            desk.stop()


def test_the_submit_carries_the_setup_label() -> None:
    kind, body = outcome("j", "ws", "built it\nDONE abc", setup="opus-v3")
    assert kind == "submit"
    assert body["setup"] == "opus-v3"
    assert "setup" not in outcome("j", "ws", "DONE abc")[1]
    assert json.dumps(body)


if __name__ == "__main__":
    unittest.main()


def test_practice_notes_keep_the_lines_not_ruled_ok_main_first() -> None:
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
    got = practice_notes({"samples": [sample]})
    assert [(n["id"], n["verdict"], n["main"]) for n in got["lines"]] == [
        ("U2", "fails", True),
        ("U1", "cannot_tell", False),
    ]
    assert got["lines"][0]["seen"] == "the base is 20 mm deep"
    assert got["nudges"] == ["widen the base"]
    assert practice_notes({"samples": []}) == {"lines": [], "nudges": []}


def test_the_status_lines_show_the_notes() -> None:
    got = {
        "results": [
            {
                "practice": "practice-1",
                "title": "a rack",
                "deliverable": True,
                "score": 1,
                "reference_score": 2,
                "state": "reported",
                "notes": {
                    "lines": [
                        {
                            "id": "U2",
                            "main": True,
                            "verdict": "fails",
                            "line": "it tips over",
                            "seen": "a 20 mm base",
                        }
                    ],
                    "nudges": ["widen the base"],
                },
            }
        ]
    }
    text = "\n".join(practice_lines(got))
    assert "U2 main fails: it tips over" in text
    assert "seen: a 20 mm base" in text
    assert "nudge: widen the base" in text
