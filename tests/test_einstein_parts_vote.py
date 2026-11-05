"""A delivery that waits for its parts (a main project delivered at its interface stage, its
sub-projects still placeholders): the vote waits until every part is built and is made on the
bundle with them in view; 14 days after the delivery it is made on the device as it stands.
Meanwhile the job scores as a job with no vote, never a 0. A platform that says nothing of parts
gets the vote at once, as before."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_einstein_votes_stats import BundlePlatform, ReviewRig

from cadmind_einstein.votes import PARTS_POLL_S, PARTS_WAIT_S


class PartsPlatform(BundlePlatform):
    def __init__(self) -> None:
        super().__init__()
        self.parts: dict[str, Any] | None = None
        self.remade = 0
        """how many asks answer the bundle not made again yet once the parts are built"""

    def review_bundle(self, job: str, audience: str = "ours") -> dict[str, Any]:
        out = super().review_bundle(job, audience)
        if self.parts is None:
            return out
        parts = dict(self.parts)
        if not parts["awaiting"] and self.remade > 0:
            self.remade -= 1
            return {
                "ready": False,
                "state": "queued",
                "retry_s": 30,
                "parts": {**parts, "assembled": True},
            }
        return {**out, "parts": parts}


class _Parts(ReviewRig):
    def setUp(self) -> None:
        super().setUp()
        self.p = self.reviewer.platform = self.desk.platform = PartsPlatform()  # type: ignore[assignment]

    def waiting(self, built: int = 1, total: int = 4) -> None:
        self.p.parts = {
            "awaiting": True,
            "built": built,
            "total": total,
            "delivered_ms": int(self.clock.t * 1000),
            "assembled": False,
        }

    def built(self) -> None:
        assert self.p.parts is not None
        self.p.parts = {
            **self.p.parts,
            "awaiting": False,
            "built": self.p.parts["total"],
            "assembled": True,
        }


class TestPartsShadow(_Parts):
    def test_without_parts_the_vote_is_made_at_once_as_before(self) -> None:
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        self.assertEqual(self.reviewer.votes_of("j1")[0]["parts"], "")

    def test_the_vote_waits_for_the_parts_then_judges_the_assembled_device(self) -> None:
        self.waiting()
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "pending")
        (row,) = self.reviewer.queue()
        self.assertEqual((row["deferred"], row["tries"]), ("parts", 0))
        self.assertIn("1 of 4 built", row["why"])
        self.assertEqual(self.model.samples, 0, "no model call while it waits")
        self.assertEqual(self.reviewer.pass_once(), "idle", "asked again only after the poll")
        self.assertTrue(any('"vote-deferred"' in s for s in self.said))
        # still waiting a week later: never expired, never counted as a try
        for _ in range(4):
            self.clock.t += 2 * 24 * 3600
            self.assertEqual(self.reviewer.pass_once(), "pending")
        self.assertEqual(self.reviewer.queue()[0]["tries"], 0)
        self.built()
        self.p.remade = 1
        self.clock.t += PARTS_POLL_S
        self.assertEqual(
            self.reviewer.pass_once(), "pending", "its bundle is made again with the parts"
        )
        self.clock.t += 60
        self.assertEqual(self.reviewer.pass_once(), "voted")
        (vote,) = self.reviewer.votes_of("j1")
        self.assertEqual(vote["parts"], "built")

    def test_a_ready_bundle_not_yet_made_with_the_parts_is_not_judged(self) -> None:
        self.waiting()
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "pending")
        assert self.p.parts is not None
        self.p.parts = {**self.p.parts, "awaiting": False, "built": 4, "assembled": False}
        self.clock.t += PARTS_POLL_S
        self.assertEqual(self.reviewer.pass_once(), "pending")
        self.assertEqual(self.model.samples, 0)
        self.built()
        self.clock.t += 60
        self.assertEqual(self.reviewer.pass_once(), "voted")

    def test_after_14_days_the_device_is_judged_as_it_stands(self) -> None:
        self.waiting()
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "pending")
        self.clock.t += PARTS_WAIT_S - 3600
        self.assertEqual(self.reviewer.pass_once(), "pending")
        self.clock.t += 2 * 3600
        self.assertEqual(self.reviewer.pass_once(), "voted")
        self.assertEqual(self.reviewer.votes_of("j1")[0]["parts"], "as it stands")

    def test_the_14_days_run_from_the_platforms_delivery_time(self) -> None:
        self.waiting()
        assert self.p.parts is not None
        self.p.parts["delivered_ms"] = int((self.clock.t - PARTS_WAIT_S) * 1000) - 1
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")


class TestPartsCounted(_Parts):
    mode = "counted"

    def test_the_score_while_deferred_is_a_job_with_no_vote(self) -> None:
        self.waiting()
        self.deliver()
        before = self.ledger.outcomes[-1].s
        self.assertEqual(self.reviewer.pass_once(), "pending")
        o = self.ledger.outcomes[-1]
        self.assertEqual((o.parts["V"], o.parts["u"]), (-1.0, 0.0), "no vote, not a 0")
        self.assertEqual(o.s, before)
        self.assertAlmostEqual(o.parts["B"], o.parts["I"])
        self.built()
        self.clock.t += PARTS_POLL_S
        self.assertEqual(self.reviewer.pass_once(), "voted")
        o = self.ledger.outcomes[-1]
        self.assertEqual(o.parts["u"], 0.4)
        self.assertAlmostEqual(o.s, 0.4 + 0.6 * (0.4 * 0.65 + 0.6 * 1.0))


if __name__ == "__main__":
    import unittest

    unittest.main()
