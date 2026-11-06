"""A remix's claim (``remix_credit``): checked and recorded with the job, never scored. The 25%
remix credit is gone (DESIGN.md section 5): a delivered remix scores its own builder only."""

from __future__ import annotations

import random
import unittest
from dataclasses import asdict
from typing import Any

from cadmind_einstein.desk import CREDIT_MAX, Desk, _credit_of
from cadmind_einstein.scoring import HOUR_MS, Ledger
from cadmind_einstein.sealing import new_keypair
from tests.test_einstein import GOOD, A, B, C, Clock, FakePlatform, wait_state

ORIGINAL = {"job": "original-1", "hotkey": A}


class TestClaimForm(unittest.TestCase):
    def test_only_well_formed_entries_are_kept(self) -> None:
        self.assertEqual(_credit_of([ORIGINAL]), [ORIGINAL])
        for bad in (
            "x",
            {"hotkey": A},
            [{"job": "j"}],
            [{"job": "", "hotkey": A}],
            [{"job": "j", "hotkey": "../x"}],
        ):
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
    def test_a_delivered_remix_scores_its_builder_only(self) -> None:
        self.desk.work(A), self.desk.work(B), self.desk.work(C)
        self.desk.miners[C].free_ms = self.desk.now_ms()  # C waited least: A or B builds it
        credit = [{"job": "original-1", "hotkey": C}]
        self.enqueue(
            {"job": "r1", "brief": "a change", "seed": self.seed(), "remix_credit": credit}
        )
        self.assertEqual(self.desk.jobs["r1"].credit, credit)  # recorded with the job
        hk, w = self.assigned()
        self.assertNotIn("credit", w)  # the miner never sees who the job names
        self.assertNotEqual(hk, C)
        self.desk.accept(hk, "r1")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, "r1", w["workspace"], "d")
        wait_state(self.desk, "r1", ("done",))
        self.assertEqual(self.weight(), {hk: 0.5})  # unrated: 0.5, and nothing for C

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
            {
                "job": "forged2",
                "brief": "x",
                "seed": self.seed(),
                "remix_credit": [{"job": "open", "hotkey": A}],
            }
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
        self.assertTrue(all("credit" not in asdict(o) for o in self.ledger.outcomes))
