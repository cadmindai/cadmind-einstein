"""A job's life at its edges (platform audit 2026-10-22): a job every miner that could take it has
tried ends (uncharged) instead of being held and leased forever, without cutting the second-opinion
wait short; and a job the platform took back after a lapsed lease is worked again when the
validator claims it again, instead of being found cancelled and leased for days."""

from __future__ import annotations

import tempfile
import unittest

from cadmind_einstein.desk import SecondOpinion
from tests import test_einstein as base
from tests.test_einstein import GOOD, A, B, C, wait_state
from tests.test_einstein_durable import Rig


class TestDesk(unittest.TestCase):
    """:class:`tests.test_einstein.TestDesk`'s rig, without its tests."""

    setUp = base.TestDesk.setUp
    tearDown = base.TestDesk.tearDown
    tick = base.TestDesk.tick
    enqueue = base.TestDesk.enqueue
    poll = base.TestDesk.poll


class _Fleet(TestDesk):
    def two(self, cold: dict[str, str] | None = None, **opinion: float) -> None:
        """A two-miner fleet (A, B), with these coldkeys and second-opinion settings."""
        self.reg = [A, B]
        del self.desk.miners[C]
        if cold is not None:
            self.desk.coldkeys = lambda: cold
        if opinion:
            self.desk.second_opinion = SecondOpinion(**opinion)  # type: ignore[arg-type]

    def holder(self, job: str) -> str:
        return next(h for h, x in self.poll(*self.reg).items() if x["job"] == job)

    def leased_now(self, job: str) -> bool:
        seen: list[list[str]] = []
        real = self.p.lease
        self.p.lease = lambda jobs: (seen.append(list(jobs)), real(jobs))[1]  # type: ignore[method-assign]
        try:
            self.desk._leased_ms = 0
            self.desk._lease(self.desk.now_ms())
        finally:
            self.p.lease = real  # type: ignore[method-assign]
        return any(job in x for x in seen)


class TestAllTried(_Fleet):
    def test_repro_a_rejection_then_a_missed_accept_ends_the_job_uncharged(self) -> None:
        self.two()
        self.poll(A, B)
        self.enqueue({"job": "z1", "brief": "A wall hook."})
        hk = self.holder("z1")
        other = B if hk == A else A
        self.desk.reject(hk, "z1", "not-capable", "too hard for me")
        self.assertTrue(self.desk.wait_idle())
        self.poll(A, B)
        self.tick()
        self.assertEqual(self.desk.work(other)["job"], "z1")  # another owner: at once
        self.clock.t += 2 * 3600  # the second miner never accepts
        self.poll(A, B)
        self.tick()
        self.tick()
        st = self.desk.status("z1")
        self.assertEqual(st["state"], "done")
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("didn't finish", False))
        self.assertEqual(self.p.completed["z1"]["label"], "didn't finish")
        self.assertFalse(self.leased_now("z1"))

    def test_every_attempt_a_rejection_ends_declined_with_the_messages(self) -> None:
        # one owner's two hotkeys: no independent opinion, so the old rule held the job forever
        self.two({A: "owner", B: "owner"}, coldkeys=2, wait_s=1800.0)
        self.poll(A, B)
        self.enqueue({"job": "z2", "brief": "A wall hook."})
        first = self.holder("z2")
        second = B if first == A else A
        self.desk.reject(first, "z2", "not-allowed", "a weapon part")
        self.assertTrue(self.desk.wait_idle())
        self.clock.t += 31 * 60
        self.poll(A, B)
        self.tick()
        self.desk.reject(second, "z2", "not-capable", "beyond my tools")
        self.assertTrue(self.desk.wait_idle())
        self.tick()
        st = self.desk.status("z2")
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("declined", False))
        self.assertEqual(
            sorted(m["message"] for m in st["result"]["messages"]),
            ["a weapon part", "beyond my tools"],
        )

    def test_the_second_opinion_wait_is_kept(self) -> None:
        # A rejects; the only untried miner is the same owner's B: the job waits out the
        # second-opinion window for a miner of another coldkey, then B takes it -- never ended
        self.two({A: "owner", B: "owner"}, coldkeys=2, wait_s=1800.0)
        self.desk.miners[B].blocked_until_ms = self.desk.now_ms() + 10**9
        self.poll(A, B)
        self.enqueue({"job": "z3", "brief": "A wall hook."})
        self.assertEqual(self.holder("z3"), A)
        self.desk.miners[B].blocked_until_ms = 0
        self.desk.reject(A, "z3", "not-capable", "too hard for me")
        self.assertTrue(self.desk.wait_idle())
        for _ in range(3):
            self.clock.t += 5 * 60
            self.poll(A, B)
            self.tick()
            self.assertEqual(self.desk.status("z3")["state"], "queued")
        self.clock.t += 20 * 60
        self.poll(A, B)
        self.tick()
        self.assertEqual(self.desk.work(B)["job"], "z3")

    def test_a_busy_untried_miner_of_another_owner_keeps_the_job_open(self) -> None:
        self.desk.miners[C].blocked_until_ms = self.desk.now_ms() + 10**9  # busy for now
        self.desk.miners[B].blocked_until_ms = self.desk.now_ms() + 10**9
        self.poll(A, B, C)
        self.enqueue({"job": "z4", "brief": "A wall hook."})
        self.assertEqual(self.holder("z4"), A)
        self.desk.miners[B].blocked_until_ms = 0
        self.desk.reject(A, "z4", "not-capable", "too hard for me")
        self.assertTrue(self.desk.wait_idle())
        b = self.holder("z4")
        self.assertEqual(b, B)
        self.clock.t += 3 * 60  # B never accepts; C, held off, has not tried it yet
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.desk.status("z4")["state"], "queued")
        self.desk.miners[C].blocked_until_ms = 0
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.desk.work(C)["job"], "z4")


class TestReclaimedAfterALostLease(TestDesk):
    def test_repro_a_job_claimed_again_is_worked_again_with_its_history(self) -> None:
        self.reg = [A]
        for hk in (B, C):
            del self.desk.miners[hk]
        self.poll(A)
        self.enqueue({"job": "l1", "brief": "A wall hook."})
        self.assertEqual(self.desk.work(A)["job"], "l1")
        self.p.lost.add("l1")  # the lease lapsed: the platform queued l1 again
        self.clock.t += 60
        self.tick()
        self.assertEqual(self.desk.status("l1")["result"]["label"], "cancelled")
        self.assertEqual(self.desk.work(A).get("cancelled"), "l1")  # A stops its agent
        self.p.lost.discard("l1")
        self.p.queue.append({"job": "l1", "brief": "A wall hook."})
        self.clock.t += 60  # past the cancel cool-off
        self.poll(A)
        self.tick()
        st = self.desk.status("l1")
        self.assertEqual(st["state"], "assigned")
        self.assertEqual([x["kind"] for x in st["attempts"]], ["lost", ""])  # history kept
        w = self.desk.work(A)
        self.assertEqual((w["job"], w.get("cancelled")), ("l1", None))
        self.desk.accept(A, "l1")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(A, "l1", w["workspace"], "d")
        st = wait_state(self.desk, "l1", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")
        self.tick()
        self.assertEqual(self.p.completed["l1"]["label"], "delivered")

    def test_a_client_cancel_is_not_reopened(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "l2", "brief": "A wall hook."})
        self.desk.cancel("l2")
        self.desk.create({"job": "l2", "brief": "A wall hook."})
        self.assertEqual(self.desk.status("l2")["result"]["label"], "cancelled")

    def test_the_reopened_job_survives_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            r = Rig(d)
            r.admit(A, B, C)
            r.poll(A, B, C)
            r.p.queue.append({"job": "l3", "brief": "A wall hook."})
            r.tick()
            r.p.lost.add("l3")
            r.clock.t += 60
            r.tick()
            assert r.desk is not None
            self.assertTrue(r.desk.status("l3")["result"].get("lost"))
            r.p.lost.discard("l3")
            r.p.queue.append({"job": "l3", "brief": "A wall hook."})
            r.clock.t += 60
            r.poll(A, B, C)
            r.tick()
            self.assertEqual(r.desk.status("l3")["state"], "assigned")
            desk = r.restart()
            try:
                st = desk.status("l3")
                self.assertEqual(st["state"], "assigned")
                self.assertEqual([x["kind"] for x in st["attempts"]], ["lost", ""])
            finally:
                desk.stop()


if __name__ == "__main__":
    unittest.main()
