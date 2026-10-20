"""A style round's gate (cadmind_einstein.style): the delivery keeps the settled contract it began
from -- the same contract digest, the same items, each within 0.001 mm and 0.001 degree."""

from __future__ import annotations

import json
import unittest
from typing import Any

from cadmind_einstein.agent_miner import questions_rule
from cadmind_einstein.style import check, compare, relative_angle_deg
from tests.test_einstein import GOOD, TestDesk, project_zip, wait_state
from tests.test_einstein_answers import OPEN

BASELINE = {
    "version": "watt-contract 1",
    "contract": "sha256:" + "c" * 64,
    "layout": {"hardware:tact[0]": [1.0, 2.0, 3.0, 0.0, 0.0, 90.0]},
}


def report(digest: str = BASELINE["contract"], layout: dict | None = None) -> str:
    return json.dumps(
        {
            "digest": "sha256:d",
            "contract": {"digest": digest, "layout": layout or BASELINE["layout"]},
        }
    )


class TestCompare(unittest.TestCase):
    def test_kept_within_tolerance_and_each_change_named(self) -> None:
        self.assertEqual(compare(BASELINE, json.loads(report())["contract"]), [])
        noisy = {"hardware:tact[0]": [1.0005, 2.0, 3.0, 0.0, 0.0, 90.0005]}
        self.assertEqual(compare(BASELINE, {"digest": BASELINE["contract"], "layout": noisy}), [])
        moved = {"hardware:tact[0]": [1.01, 2.0, 3.0, 0.0, 0.0, 90.0]}
        self.assertIn(
            "0.010 mm", compare(BASELINE, {"digest": BASELINE["contract"], "layout": moved})[0]
        )
        self.assertIn(
            "changed", compare(BASELINE, {"digest": "sha256:e", "layout": BASELINE["layout"]})[0]
        )
        self.assertEqual(
            compare(BASELINE, {"digest": BASELINE["contract"], "layout": {}}),
            ["hardware:tact[0] is gone"],
        )
        self.assertTrue(compare(BASELINE, None))
        self.assertAlmostEqual(relative_angle_deg([0, 0, 0], [0, 0, 360]), 0.0, places=6)

    def test_the_export_is_read_under_its_workspace_directory(self) -> None:
        kept = project_zip({"build/report.json": report()})
        self.assertEqual(check(kept, BASELINE), "")
        changed = project_zip({"build/report.json": report(digest="sha256:e")})
        self.assertIn("changed the settled contract", check(changed, BASELINE))
        self.assertIn("no contract", check(project_zip({"brief/brief.md": "x"}), BASELINE))
        self.assertEqual(check(changed, None), "", "not a style round")


class TestDeskStyle(unittest.TestCase):
    """The desk on the in-memory platform: the style gate is part of the gate, on the validator's
    own export of its fork."""

    tearDown = TestDesk.tearDown
    tick = TestDesk.tick
    enqueue = TestDesk.enqueue
    poll = TestDesk.poll
    assigned = TestDesk.assigned

    def setUp(self) -> None:
        TestDesk.setUp(self)  # type: ignore[arg-type]

    def run_job(self, job: str, files: dict[str, str], **body: Any) -> dict[str, Any]:
        self.poll(*self.reg)
        self.enqueue({"job": job, "brief": "Reshape it.", **body})
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], {**GOOD, **files}, deliverable=True)
        self.desk.submit(hk, job, w["workspace"], "d")
        st = wait_state(self.desk, job, ("queued", "done"))
        st["hotkey"], st["work"] = hk, w
        return st

    def test_a_kept_contract_is_delivered_and_the_miner_is_told_it_is_a_style_round(self) -> None:
        st = self.run_job(
            "s1", {"build/report.json": report()}, style={"baseline": BASELINE}, no_questions=True
        )
        self.assertIs(st["work"]["style"], True)
        self.assertEqual(st["result"]["label"], "delivered")

    def test_a_changed_contract_fails_the_gate_and_the_miner_reads_why(self) -> None:
        st = self.run_job(
            "s2", {"build/report.json": report(digest="sha256:e")}, style={"baseline": BASELINE}
        )
        self.assertEqual(st["state"], "queued")
        why = self.desk.failures(st["hotkey"])[-1]["why"]
        self.assertIn("changed the settled contract", why)

    def test_a_job_without_a_baseline_is_not_a_style_round(self) -> None:
        st = self.run_job(
            "s3", {"build/report.json": report(digest="sha256:e")}, style={"words": "x"}
        )
        self.assertNotIn("style", st["work"])
        self.assertEqual(st["result"]["label"], "delivered")


class TestSettledOpenDecisions(unittest.TestCase):
    """A client decision open when the version was delivered stays open through its style round
    when the contract settles it (deciding it would change the contract): the no-questions gate
    exempts exactly the baseline's ``settled_decisions``, and nothing without them."""

    tearDown = TestDesk.tearDown
    tick = TestDesk.tick
    enqueue = TestDesk.enqueue
    poll = TestDesk.poll
    assigned = TestDesk.assigned

    def setUp(self) -> None:
        TestDesk.setUp(self)  # type: ignore[arg-type]

    def run_style(self, job: str, baseline: dict[str, Any], decisions: str) -> dict[str, Any]:
        seed = self.p._new(files={**GOOD, "spec/decisions.toml": OPEN})
        self.poll(*self.reg)
        self.enqueue(
            {
                "job": job,
                "brief": "Reshape it.",
                "seed": seed,
                "no_questions": True,
                "style": {"baseline": baseline},
            }
        )
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(
            w["workspace"],
            {"spec/decisions.toml": decisions, "build/report.json": report()},
            deliverable=True,
        )
        self.desk.submit(hk, job, w["workspace"], "d")
        st = wait_state(self.desk, job, ("queued", "done"))
        st["hotkey"] = hk
        return st

    def test_an_open_decision_the_contract_settles_stays_open(self) -> None:
        st = self.run_style("o1", {**BASELINE, "settled_decisions": ["mount"]}, OPEN)
        self.assertEqual(st["result"]["label"], "delivered", st)

    def test_a_baseline_without_settled_decisions_exempts_nothing(self) -> None:
        st = self.run_style("o3", BASELINE, OPEN)
        self.assertEqual(st["state"], "queued")
        self.assertIn("asked not to be asked", self.desk.failures(st["hotkey"])[-1]["why"])

    def test_named_settled_decisions_are_the_only_ones_exempt(self) -> None:
        st = self.run_style("o2", {**BASELINE, "settled_decisions": []}, OPEN)
        self.assertEqual(st["state"], "queued")
        self.assertIn("asked not to be asked", self.desk.failures(st["hotkey"])[-1]["why"])


class TestStylePrompt(unittest.TestCase):
    def test_a_style_round_says_what_it_may_change(self) -> None:
        said = questions_rule({"no_questions": True, "style": True}, "ag-1")
        self.assertIn("STYLE ROUND", said)
        self.assertIn("W0220", said)
        self.assertNotIn("STYLE ROUND", questions_rule({"no_questions": True}, "ag-1"))
