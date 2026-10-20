"""Continuing an unfinished round (DESIGN.md "Continuing an unfinished round"): one worked attempt
that does not deliver ends the round "not finished" with its project kept; a later round may
continue it (``continuation``), its builder free to start over from the base; rejections, scoring
and the copy check keep their rules."""

from __future__ import annotations

import random
import unittest
from typing import Any, ClassVar

from bittensor.sp_core import Keypair

from cadmind_einstein.desk import Desk
from cadmind_einstein.scoring import Ledger
from cadmind_einstein.sealing import new_keypair
from tests.test_einstein import GOOD, Clock, FakePlatform, wait_state

A, B, C, D = (
    Keypair.create_from_uri(u).ss58_address for u in ("//Alice", "//Bob", "//Charlie", "//Dave")
)

DELIVERED = {
    "brief/brief.md": "A shelf bracket.",
    "src/parts/bracket.txt": "\n".join(
        f"bracket rib {i} of the delivered design" for i in range(40)
    ),
}
"""A delivered project's distinctive lines (text features, one per line)."""


class ContinuePlatform(FakePlatform):
    """The fake platform, with the continuation grant: the miner's workspace from the seed and a
    read-only fork of the base beside it."""

    def __init__(self) -> None:
        super().__init__()
        self.continuations: list[dict[str, Any]] = []

    def grant_continuation(
        self, hotkey: str, *, seed: str, base: str = "", name: str = "job"
    ) -> tuple[str, str]:
        self.continuations.append({"hotkey": hotkey, "seed": seed, "base": base, "name": name})
        ws = self._new(hotkey=hotkey, files=dict(self.ws[seed]["files"]))
        ref = ""
        if base:
            ref = self._new(hotkey=hotkey, files=dict(self.ws[base]["files"]), frozen=True)
        return ws, ref

    def operator_ws(self, files: dict[str, str]) -> str:
        """One of the validator's own workspaces (a kept fork), holding ``files``."""
        return self._new(files=dict(files), hotkey="validator")


class _Desk(unittest.TestCase):
    coldkeys: ClassVar[dict[str, str]] = {}

    def setUp(self) -> None:
        self.p = ContinuePlatform()
        self.clock = Clock()
        self.ledger = Ledger(hour_ms=3_600_000)
        self.reg = [A, B, C, D]
        self.desk = Desk(
            self.p,
            self.ledger,
            registered=lambda: self.reg,
            clock=self.clock,
            rng=random.Random(7),  # noqa: S311 -- a test's seed
            coldkeys=lambda: self.coldkeys,
        )
        for hk in self.reg:
            self.desk.agent_key(hk, new_keypair()[1])
            self.desk.miners[hk].admitted = True

    def tearDown(self) -> None:
        self.desk.stop()

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def poll(self) -> dict[str, dict[str, Any]]:
        return {hk: self.desk.work(hk) for hk in self.reg}

    def enqueue(self, body: dict[str, Any]) -> None:
        self.poll()
        self.p.queue.append(dict(body))
        self.tick()

    def holder(self) -> tuple[str, dict[str, Any]]:
        for hk, w in self.poll().items():
            if w["job"]:
                return hk, w
        raise AssertionError("nobody has the job")

    def build(
        self,
        job: str,
        files: dict[str, str],
        *,
        deliverable: bool,
        summary: str = "",
    ) -> tuple[str, dict[str, Any]]:
        hk, w = self.holder()
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], files, deliverable=deliverable)
        self.desk.submit(hk, job, w["workspace"], "d", summary)
        wait_state(self.desk, job, ("queued", "done"))
        self.tick()
        return hk, w


class TestTheRoundEnds(_Desk):
    def test_a_gate_failure_ends_the_round_its_fork_kept_and_the_builder_summary_sent(
        self,
    ) -> None:
        self.enqueue({"job": "n1", "brief": "A shelf bracket."})
        hk, _ = self.build(
            "n1",
            {"src/parts/bracket.txt": "half a bracket"},
            deliverable=False,
            summary="  The rib\n count is   not yet proven  against the load. ",
        )
        st = self.desk.status("n1")
        res = st["result"]
        self.assertEqual(
            (st["state"], res["label"], res["charge"]), ("done", "not finished", False)
        )
        fork = self.desk.jobs["n1"].attempts[0].fork
        self.assertEqual(res["workspace"], fork)
        self.assertNotIn(fork, self.p.deleted)
        self.assertEqual(res["summary"], "The rib count is not yet proven against the load.")
        self.assertEqual(res["summary_by"], "builder")
        self.assertEqual(res["stage"], "check")
        self.assertIn("digest", res)
        self.assertNotIn("copy", res)
        self.assertEqual(len(self.p.grants), 1)  # nobody else was handed it
        self.assertEqual(self.p.completed["n1"]["label"], "not finished")
        # scoring as today: a held gate failure counts (0) when one owner alone failed the job
        self.assertEqual([(o.hotkey, o.s) for o in self.ledger.outcomes], [(hk, 0.0)])
        self.assertEqual(self.desk.failed[hk], 1)  # the abuse watch still sees the failure
        self.assertEqual(self.desk.failures(hk)[-1]["kind"], "gate")
        self.assertIsNone(self.desk.work(hk)["job"])  # the miner is free

    def test_without_a_builder_summary_the_gate_says_what_blocks_delivery(self) -> None:
        self.enqueue({"job": "n2", "brief": "A shelf bracket."})
        self.build("n2", {"x.md": "x"}, deliverable=False)
        res = self.desk.status("n2")["result"]
        self.assertEqual(res["summary_by"], "gate")
        self.assertIn("did not pass Watt's checks", res["summary"])
        self.assertIn("check stage", res["summary"])

    def test_a_late_attempt_that_fails_ends_the_round_and_its_timeout_scores_at_once(
        self,
    ) -> None:
        self.enqueue({"job": "n3", "brief": "A shelf bracket.", "effort": "low"})
        hk, w = self.holder()
        self.desk.accept(hk, "n3")
        self.p.work(w["workspace"], {"src/parts/bracket.txt": "a start"}, deliverable=False)
        self.clock.t += 31 * 60  # past the low effort's deadline, nothing submitted
        self.tick()
        st = wait_state(self.desk, "n3", ("done",))
        res = st["result"]
        self.assertEqual(res["label"], "not finished")
        a = self.desk.jobs["n3"].attempts[0]
        self.assertEqual(a.kind, "timeout")
        self.assertEqual(res["workspace"], a.fork)
        self.assertNotIn(a.fork, self.p.deleted)
        self.assertEqual(res["summary_by"], "gate")
        self.assertIn("time ran out", res["summary"])
        self.assertEqual(
            [(o.hotkey, o.kind, o.s) for o in self.ledger.outcomes], [(hk, "timeout", 0.0)]
        )
        self.assertEqual(self.desk.failures(hk)[-1]["kind"], "timeout")

    def test_an_assignment_never_accepted_still_goes_to_another_miner_from_scratch(self) -> None:
        self.enqueue({"job": "n4", "brief": "A shelf bracket."})
        first, _ = self.holder()
        self.clock.t += 121  # the acceptance window passed
        self.tick()
        self.assertEqual(self.desk.jobs["n4"].attempts[0].kind, "timeout")
        self.poll()  # the miners are alive again
        self.tick()
        second, _ = self.holder()
        self.assertNotEqual(second, first)
        self.assertEqual(self.desk.status("n4")["state"], "assigned")
        self.assertEqual(self.p.grants[-1]["seed"], "")
        self.assertEqual(len(self.p.grants), 2)

    def test_a_rejection_then_a_worked_failure_ends_not_finished_nobody_penalised(self) -> None:
        self.enqueue({"job": "n5", "brief": "A shelf bracket."})
        rejecter, _ = self.holder()
        self.desk.reject(rejecter, "n5", "not-capable", "beyond what can be checked")
        self.tick()
        builder, _ = self.build("n5", {"x.md": "x"}, deliverable=False)
        self.assertNotEqual(builder, rejecter)
        res = self.desk.status("n5")["result"]
        self.assertEqual(res["label"], "not finished")
        self.assertEqual(len(res["messages"]), 1)
        # two owners' opinions: the held rejection and failure are dropped, as for today's ending
        self.assertEqual(self.ledger.outcomes, [])


class TestRejectionsUnchanged(_Desk):
    coldkeys: ClassVar[dict[str, str]] = {A: "ck1", B: "ck1", C: "ck2", D: "ck3"}

    def test_two_rejections_from_two_coldkeys_decline_and_one_owner_cannot(self) -> None:
        self.enqueue({"job": "r1", "brief": "A suppressor.", "prefer": A})
        first, _ = self.holder()
        self.assertEqual(first, A)
        self.desk.reject(A, "r1", "not-allowed", "a weapon part")
        self.tick()
        second, _ = self.holder()
        self.assertIn(second, (C, D))  # a second opinion is another owner's, never B (ck1)
        self.desk.reject(second, "r1", "not-allowed", "a weapon part")
        self.tick()
        res = self.desk.status("r1")["result"]
        self.assertEqual((res["label"], res["charge"]), ("declined", False))
        self.assertEqual(self.ledger.outcomes, [])


class TestContinuation(_Desk):
    def test_a_continuation_is_granted_from_the_seed_with_the_delivered_base_beside_it(
        self,
    ) -> None:
        base = self.p.operator_ws(DELIVERED)
        seed = self.p.operator_ws({**DELIVERED, "src/parts/extra.txt": "half done"})
        self.enqueue(
            {"job": "c1", "brief": "Continue.", "seed": seed, "continuation": {"base": base}}
        )
        hk, w = self.holder()
        self.assertEqual(
            self.p.continuations, [{"hotkey": hk, "seed": seed, "base": base, "name": "c1"}]
        )
        self.assertEqual(self.p.grants, [])  # never a plain grant
        ref = self.desk.jobs["c1"].attempts[0].reference
        self.assertTrue(ref)
        self.assertIs(w["continuation"], True)
        self.assertEqual(w["base"], {"kind": "delivered", "workspace": ref})
        self.assertIn("src/parts/extra.txt", self.p.ws[w["workspace"]]["files"])
        self.assertEqual(self.p.ws[ref]["files"], DELIVERED)
        self.assertNotIn("prefer", self.desk.status("c1"))

    def test_a_continuation_without_a_delivered_version_starts_over_from_the_brief(self) -> None:
        seed = self.p.operator_ws({"brief/brief.md": "A bracket.", "x.md": "half"})
        self.enqueue({"job": "c2", "brief": "Continue.", "seed": seed, "continuation": {}})
        _, w = self.holder()
        self.assertEqual(self.p.continuations[-1]["base"], "")
        self.assertEqual(w["base"], {"kind": "brief"})
        self.assertIs(w["continuation"], True)

    def test_an_ordinary_job_says_nothing_of_a_continuation(self) -> None:
        self.enqueue({"job": "c3", "brief": "A bracket."})
        _, w = self.holder()
        self.assertNotIn("continuation", w)
        self.assertNotIn("base", w)
        self.assertEqual(self.p.continuations, [])


class TestCopyCheckWithAContinuationSeed(_Desk):
    """A continuation's builder hands in a project made largely of the seed (the unfinished
    project) or of the base (the last delivered version, when it started over): neither is a copy
    of the earlier delivery, while the same output on a job without them is."""

    def deliver_first(self) -> str:
        self.enqueue({"job": "d1", "brief": "A shelf bracket.", "prefer": B})
        hk, _ = self.build("d1", {**GOOD, **DELIVERED}, deliverable=True)
        self.assertEqual(hk, B)
        st = self.desk.status("d1")
        self.assertEqual(st["result"]["label"], "delivered")
        return str(st["result"]["workspace"])

    def continue_with(
        self, job: str, body: dict[str, Any], files: dict[str, str]
    ) -> dict[str, Any]:
        self.enqueue({"job": job, "brief": "Continue.", "prefer": A, **body})
        hk, _ = self.build(job, files, deliverable=True)
        self.assertEqual(hk, A)
        return self.desk.status(job)

    def test_continuing_the_seed_is_not_a_copy(self) -> None:
        delivered = self.deliver_first()
        seed = self.p.operator_ws(
            {**GOOD, **DELIVERED, "src/parts/extra.txt": "the unfinished part, half done"}
        )
        st = self.continue_with(
            "k1",
            {"seed": seed, "continuation": {"base": delivered}},
            {"src/parts/extra.txt": "the unfinished part, now finished and proven"},
        )
        self.assertEqual(st["result"]["label"], "delivered")
        self.assertEqual(self.desk.copied.get(A, 0), 0)

    def test_starting_over_from_the_base_is_not_a_copy(self) -> None:
        delivered = self.deliver_first()
        # the unfinished project went a wrong way: little of the delivered design is left in it
        seed = self.p.operator_ws(
            {"src/parts/bracket.txt": "\n".join(f"a wrong path {i}" for i in range(40))}
        )
        st = self.continue_with(
            "k2",
            {"seed": seed, "continuation": {"base": delivered}},
            {**GOOD, **DELIVERED, "src/parts/extra.txt": "a new part on the delivered design"},
        )
        self.assertEqual(st["result"]["label"], "delivered")
        self.assertEqual(self.desk.copied.get(A, 0), 0)

    def test_the_same_output_without_the_base_is_a_copy(self) -> None:
        self.deliver_first()
        seed = self.p.operator_ws(
            {"src/parts/bracket.txt": "\n".join(f"a wrong path {i}" for i in range(40))}
        )
        self.enqueue({"job": "k3", "brief": "A shelf bracket.", "prefer": A, "seed": seed})
        hk, _ = self.holder()
        self.assertEqual(hk, A)
        _, w = self.holder()
        self.desk.accept(A, "k3")
        self.p.work(
            w["workspace"],
            {**GOOD, **DELIVERED, "src/parts/extra.txt": "a new part on the delivered design"},
            deliverable=True,
        )
        self.desk.submit(A, "k3", w["workspace"], "d")
        wait_state(self.desk, "k3", ("queued", "done"))
        self.assertEqual(self.desk.jobs["k3"].attempts[0].kind, "copy")
        self.assertEqual(self.desk.copied.get(A, 0), 1)


if __name__ == "__main__":
    unittest.main()
