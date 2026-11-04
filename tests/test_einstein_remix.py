"""The remix credit (DESIGN.md "Weights"): a delivered remix adds 25% of its score to the original
miners' weight, once per job, decaying with it; the remix miner keeps its full score; nobody invents
a credit; a miner who left changes nothing else."""

from __future__ import annotations

import random
import tempfile
import unittest
from typing import Any

from cadmind_einstein.desk import CREDIT_MAX, Desk, _credit_of
from cadmind_einstein.scoring import (
    HALF_LIFE_H,
    HOUR_MS,
    REMIX_CREDIT,
    JobOutcome,
    Ledger,
    recompute,
    report_bytes,
    score_all,
    standing,
    vector,
)
from cadmind_einstein.sealing import new_keypair
from tests.test_einstein import GOOD, OWNER, A, B, C, Clock, D, FakePlatform, passing, wait_state
from tests.test_einstein_durable import Rig

NOW = 1_000 * HOUR_MS
ORIGINAL = {"job": "original-1", "hotkey": A}


def scored(*jobs: JobOutcome) -> list[JobOutcome]:
    return score_all(jobs)


class TestStanding(unittest.TestCase):
    def test_the_original_miner_gets_a_quarter_and_the_remix_miner_keeps_all(self) -> None:
        plain = scored(passing("r", B, ended_ms=NOW))
        credited = scored(passing("r", B, ended_ms=NOW, credit=[A]))
        self.assertEqual(plain[0].s, credited[0].s)
        weight, quality = standing(credited, NOW)
        self.assertAlmostEqual(weight[B], credited[0].s)
        self.assertAlmostEqual(weight[A], REMIX_CREDIT * credited[0].s)
        self.assertEqual(REMIX_CREDIT, 0.25)
        self.assertNotIn(A, quality)  # the credit is weight, never quality

    def test_several_original_miners_share_the_quarter(self) -> None:
        jobs = scored(passing("r", B, ended_ms=NOW, credit=[A, C, C]))
        weight, _ = standing(jobs, NOW)
        self.assertAlmostEqual(weight[A], REMIX_CREDIT * jobs[0].s / 2)
        self.assertAlmostEqual(weight[C], REMIX_CREDIT * jobs[0].s / 2)

    def test_it_decays_like_the_job(self) -> None:
        jobs = scored(passing("r", B, ended_ms=NOW, credit=[A]))
        later = NOW + int(HALF_LIFE_H) * HOUR_MS
        weight, _ = standing(jobs, later)
        self.assertAlmostEqual(weight[A], 0.5 * REMIX_CREDIT * jobs[0].s)

    def test_a_credited_miner_keeps_its_own_quality(self) -> None:
        jobs = scored(
            passing("own", A, ended_ms=NOW),
            JobOutcome(job="bad", hotkey=A, ended_ms=NOW, kind="gate"),
            passing("r", B, ended_ms=NOW, credit=[A]),
        )
        _, without = standing(jobs[:2], NOW)
        _, quality = standing(jobs, NOW)
        self.assertAlmostEqual(quality[A], without[A])

    def test_self_remix_is_allowed_and_adds_up(self) -> None:
        jobs = scored(passing("r", A, ended_ms=NOW, credit=[A]))
        weight, _ = standing(jobs, NOW)
        self.assertAlmostEqual(weight[A], 1.25 * jobs[0].s)

    def test_a_job_that_did_not_pass_credits_no_one(self) -> None:
        failed = JobOutcome(job="r", hotkey=B, ended_ms=NOW, kind="gate", credit=[A])
        copied = passing("r2", B, ended_ms=NOW, credit=[A], copy_of="other")
        weight, _ = standing(scored(failed, copied), NOW)
        self.assertNotIn(A, weight)

    def test_a_job_counts_once_even_with_two_passing_outcomes(self) -> None:
        jobs = scored(
            passing("r", B, ended_ms=NOW, credit=[A]),
            passing("r", C, ended_ms=NOW + 1, credit=[A]),
        )
        weight, _ = standing(jobs, NOW + 1)
        self.assertAlmostEqual(weight[A], REMIX_CREDIT * jobs[0].s, places=6)

    def test_a_late_signal_moves_the_credit(self) -> None:
        ledger = Ledger(hour_ms=HOUR_MS)
        ledger.add(passing("r", B, ended_ms=NOW, credit=[A]))
        before = ledger.snapshot(NOW)[1][A]
        ledger.signal("r", {"kind": "stars", "stars": 1, "trust": "paid", "at_ms": NOW})
        after = ledger.snapshot(NOW)[1][A]
        self.assertLess(after, before)

    def test_a_withdrawn_job_takes_its_credit(self) -> None:
        ledger = Ledger(hour_ms=HOUR_MS)
        ledger.add(passing("r", B, ended_ms=NOW, credit=[A]))
        ledger.withdraw("r")
        self.assertNotIn(A, ledger.snapshot(NOW)[1])

    def test_it_leaves_with_the_window(self) -> None:
        ledger = Ledger(hour_ms=HOUR_MS)
        ledger.add(passing("r", B, ended_ms=NOW, credit=[A]))
        self.assertNotIn(A, ledger.snapshot(NOW + 169 * HOUR_MS)[1])


class TestVector(unittest.TestCase):
    def rig(self, *credit: str) -> dict[str, float]:
        jobs = scored(
            passing("own", B, ended_ms=NOW),
            passing("other", C, ended_ms=NOW),
            passing("r", B, ended_ms=NOW, credit=list(credit)),
        )
        return standing(jobs, NOW)[0]

    def test_a_miner_who_has_left_is_simply_out(self) -> None:
        weight = self.rig(D)
        self.assertIn(D, weight)
        uids = {B: 1, C: 2, D: 3}
        gone = {B: 1, C: 2}
        kw = {"owner_hotkey": OWNER, "reachable": [B, C, D]}
        vec, rule = vector(weight, gone, admitted=[B, C], **kw)
        self.assertEqual(rule, "history")
        self.assertEqual({u for u, _ in vec}, {1, 2})
        # the others stand exactly as if there had been no credit at all
        without = {k: v for k, v in weight.items() if k != D}
        self.assertEqual(vec, vector(without, gone, admitted=[B, C], **kw)[0])
        # revoked but still registered: out too
        self.assertEqual(vec, vector(weight, uids, admitted=[B, C], **kw)[0])

    def test_the_owner_is_never_credited_into_the_vector(self) -> None:
        weight = self.rig(OWNER)
        vec, _ = vector(
            weight,
            {B: 1, C: 2, OWNER: 9},
            owner_hotkey=OWNER,
            admitted=[B, C, OWNER],
            reachable=[B, C, OWNER],
        )
        self.assertEqual({u for u, _ in vec}, {1, 2})

    def test_a_credited_miner_earns_only_while_it_is_admitted_and_reachable(self) -> None:
        weight = self.rig(A)
        vec, _ = vector(
            weight, {A: 0, B: 1, C: 2}, owner_hotkey=OWNER, admitted=[A, B, C], reachable=[A, B, C]
        )
        self.assertEqual({u for u, _ in vec}, {0, 1, 2})


class TestReports(unittest.TestCase):
    def report(self, **kw: Any) -> dict[str, Any]:
        ledger = Ledger(hour_ms=HOUR_MS)
        ledger.add(passing("own", B, ended_ms=NOW))
        ledger.add(passing("r", B, ended_ms=NOW, **kw))
        return ledger.report(
            NOW, {A: 0, B: 1}, owner_hotkey=OWNER, admitted=[A, B], reachable=[A, B]
        )

    def test_recompute_rebuilds_the_credit_from_the_report(self) -> None:
        rep = self.report(credit=[A])
        self.assertEqual({u for u, _ in rep["vector"]}, {0, 1})
        self.assertEqual(rep["constants"]["remix_credit"], 0.25)
        again = recompute(rep)
        self.assertTrue(again["matches_published"])
        self.assertEqual(again["mismatches"], [])

    def test_recompute_sees_a_credit_edited_in_a_report(self) -> None:
        rep = self.report(credit=[A])
        for row in rep["jobs"]:
            row.pop("credit", None)
        self.assertFalse(recompute(rep)["matches_published"])

    def test_a_report_without_a_remix_says_nothing_of_it(self) -> None:
        rep = self.report()
        self.assertTrue(all("credit" not in row for row in rep["jobs"]))
        self.assertTrue(recompute(rep)["matches_published"])
        self.assertIn(b'"remix_credit":0.25', report_bytes(rep))


class TestClaimForm(unittest.TestCase):
    def test_only_well_formed_entries_are_kept(self) -> None:
        self.assertEqual(_credit_of([ORIGINAL]), [ORIGINAL])
        for bad in ("x", {"hotkey": A}, [{"job": "j"}], [{"job": "", "hotkey": A}], [{"job": "j", "hotkey": "../x"}]):
            self.assertEqual(_credit_of(bad), [])
        self.assertEqual(_credit_of([{"job": "j", "hotkey": A}] * (CREDIT_MAX + 1)), [])


class DeskRig(unittest.TestCase):
    def setUp(self) -> None:
        self.p = FakePlatform()
        self.ledger = Ledger(hour_ms=HOUR_MS)
        self.desk = Desk(
            self.p,
            self.ledger,
            registered=lambda: [A, B, C],
            clock=Clock(),
            rng=random.Random(7),  # noqa: S311 -- a test's seed
        )
        for hk in (A, B, C):
            self.desk.agent_key(hk, new_keypair()[1])
            self.desk.miners[hk].admitted = True

    def tearDown(self) -> None:
        self.desk.stop()

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def enqueue(self, body: dict[str, Any]) -> None:
        self.p.queue.append(dict(body))
        self.tick()

    def assigned(self) -> tuple[str, dict[str, Any]]:
        for hk in (A, B, C):
            w = self.desk.work(hk)
            if w["job"]:
                return hk, w
        raise AssertionError("nobody has the job")

    def deliver(self, job: str) -> str:
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, job, w["workspace"], "d")
        st = wait_state(self.desk, job, ("done",))
        self.assertEqual(st["result"]["label"], "delivered")
        self.tick()
        return hk

    def seed(self) -> str:
        return self.p._new(frozen=True, files={"src/parts/x.txt": "the original's design\n"})

    def weight(self) -> dict[str, float]:
        return self.ledger.snapshot(self.desk.now_ms())[1]


class TestDesk(DeskRig):
    def test_a_delivered_remix_credits_its_original_miner(self) -> None:
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        self.enqueue(
            {"job": "r1", "brief": "a change", "seed": self.seed(), "remix_credit": [ORIGINAL]}
        )
        self.assertEqual(self.desk.jobs["r1"].credit, [ORIGINAL])
        hk, w = self.assigned()
        self.assertNotIn("remix_credit", str(w))  # the miner never sees who the job credits
        self.assertNotIn("credit", w)
        self.desk.accept(hk, "r1")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, "r1", w["workspace"], "d")
        wait_state(self.desk, "r1", ("done",))
        passed = [o for o in self.ledger.outcomes if o.job == "r1" and o.passed]
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0].credit, [A])
        weight = self.weight()
        own = passed[0].s + (0 if hk != A else REMIX_CREDIT * passed[0].s)
        self.assertAlmostEqual(weight[hk], own)
        self.assertGreaterEqual(weight[A], REMIX_CREDIT * passed[0].s - 1e-9)

    def test_a_job_with_no_credit_in_its_claim_credits_no_one(self) -> None:
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        self.enqueue({"job": "r2", "brief": "a change", "seed": self.seed()})
        hk = self.deliver("r2")
        self.assertEqual(set(self.weight()), {hk})

    def test_a_failed_attempt_and_its_retry_count_once(self) -> None:
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        credit = [{"job": "original-1", "hotkey": C}]
        self.enqueue({"job": "r3", "brief": "a change", "seed": self.seed(), "remix_credit": credit})
        # the first builder declines; the job goes to another, whose delivery is the one credited
        first, _ = self.assigned()
        self.desk.reject(first, "r3", "not-capable", "not for me")
        self.tick()
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        second = self.deliver("r3")
        self.assertNotEqual(first, second)
        # the validator sends the result again, and the claim comes round again: nothing more
        self.p.completed.pop("r3", None)
        self.desk.create({"job": "r3", "brief": "a change", "remix_credit": credit})
        self.tick()
        passed = [o for o in self.ledger.outcomes if o.job == "r3" and o.passed]
        self.assertEqual(len(passed), 1)
        weight = self.weight()
        expected = REMIX_CREDIT * passed[0].s
        if second == C:
            expected += passed[0].s
        self.assertAlmostEqual(weight[C], expected)

    def test_a_credit_the_validator_saw_otherwise_is_refused(self) -> None:
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        self.enqueue({"job": "o1", "brief": "the original"})
        builder = self.deliver("o1")
        other = next(h for h in (A, B, C) if h != builder)
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        self.enqueue(
            {
                "job": "forged",
                "brief": "a change",
                "seed": self.seed(),
                "remix_credit": [{"job": "o1", "hotkey": other}],
            }
        )
        self.assertEqual(self.desk.jobs["forged"].credit, [])
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        self.enqueue(
            {
                "job": "true",
                "brief": "a change",
                "seed": self.seed(),
                "remix_credit": [{"job": "o1", "hotkey": builder}],
            }
        )
        self.assertEqual(self.desk.jobs["true"].credit, [{"job": "o1", "hotkey": builder}])

    def test_a_job_not_delivered_credits_no_one(self) -> None:
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        self.enqueue({"job": "open", "brief": "still queued, or taken"})
        self.enqueue(
            {"job": "forged2", "brief": "x", "seed": self.seed(), "remix_credit": [{"job": "open", "hotkey": A}]}
        )
        self.assertEqual(self.desk.jobs["forged2"].credit, [])

    def test_a_miner_cannot_add_a_credit_when_it_submits(self) -> None:
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        self.enqueue({"job": "r4", "brief": "a change", "seed": self.seed()})
        hk, w = self.assigned()
        self.desk.accept(hk, "r4")
        self.p.work(w["workspace"], {**GOOD, "brief/remix_credit": str(ORIGINAL)}, deliverable=True)
        with self.assertRaises(TypeError):
            self.desk.submit(hk, "r4", w["workspace"], "d", remix_credit=[ORIGINAL])  # type: ignore[call-arg]
        self.desk.submit(hk, "r4", w["workspace"], "d")
        wait_state(self.desk, "r4", ("done",))
        self.assertTrue(all(not o.credit for o in self.ledger.outcomes))


class TestRestart(unittest.TestCase):
    def test_the_credit_survives_a_restart_and_counts_once(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            try:
                desk = rig.desk
                assert desk is not None
                for hk in (A, B, C):
                    desk.agent_key(hk, new_keypair()[1])
                    desk.miners[hk].admitted = True
                    desk.work(hk)
                credit = [{"job": "original-1", "hotkey": C}]
                rig.p.queue.append({"job": "r5", "brief": "a change", "remix_credit": credit})
                desk.tick()
                desk.wait_idle()
                # restarted with the job assigned: the claim's credit comes back from the store
                desk = rig.restart()
                self.assertEqual(desk.jobs["r5"].credit, credit)
                for hk in (A, B, C):
                    desk.work(hk)
                hk, w = next((h, x) for h in (A, B, C) if (x := desk.work(h))["job"])
                desk.accept(hk, "r5")
                rig.p.work(w["workspace"], GOOD, deliverable=True)
                desk.submit(hk, "r5", w["workspace"], "d")
                wait_state(desk, "r5", ("done",))
                desk.tick()
                desk.wait_idle()
                now = desk.now_ms()
                weight = desk.ledger.snapshot(now)[1]
                desk = rig.restart()
                self.assertEqual(desk.ledger.snapshot(now)[1], weight)
                passed = [o for o in desk.ledger.outcomes if o.job == "r5" and o.passed]
                self.assertEqual((len(passed), passed[0].credit), (1, [C]))
                expected = REMIX_CREDIT * passed[0].s + (passed[0].s if hk == C else 0)
                self.assertAlmostEqual(weight[C], expected)
            finally:
                assert rig.desk is not None
                rig.desk.stop()
                assert rig.store is not None
                rig.store.close()


if __name__ == "__main__":
    unittest.main()
