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
from cadmind_einstein.votes import Reviewer


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
