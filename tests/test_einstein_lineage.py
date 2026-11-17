"""A claim's ``lineage`` (the job ids of a project's earlier versions): the copy check never
compares a round's output with theirs. A round may rebuild what an earlier version had -- the
client asking to undo what the last round changed -- and only the seed's (the last version's)
features are subtracted, so the earlier version's builder would otherwise be its victim."""

from __future__ import annotations

import random
import unittest
from dataclasses import asdict
from typing import Any

from cadmind_einstein.desk import LINEAGE_MAX, Desk, DeskError, _job_from, _lineage_of
from cadmind_einstein.scoring import Ledger
from cadmind_einstein.similarity import CopyCheck
from tests.test_einstein import A, B, C, Clock, FakePlatform, wait_state
from tests.test_einstein_abuse import fp, project

# version 1 of a project, then version 2 changed most of its design; version 3 is asked to undo
# that change, so an honest builder hands version 1's design in again
V1 = project("plug", [67.0, 8.0, 6.0, 2.0, 32.8, 1.2, 9.9, 1.4, 0.8, 16.0, 3.5, 12.5])
V2 = project("plug", [61.0, 8.0, 5.5, 2.0, 30.1, 1.2, 9.0, 1.4, 0.7, 15.0, 3.5, 12.0])
OTHER = project("clip", [11.0, 4.5, 3.25, 7.75, 19.0, 2.2, 5.5, 0.9, 14.5, 6.25])


class TestClaimForm(unittest.TestCase):
    def test_only_short_job_ids_are_kept(self) -> None:
        self.assertEqual(_lineage_of(["v2", "v1"]), ["v2", "v1"])
        self.assertEqual(_lineage_of(["v2", "", 3, None, "x" * 81, "v2", "v1"]), ["v2", "v1"])
        for bad in (None, "v1", {"job": "v1"}, 7):
            self.assertEqual(_lineage_of(bad), [])
        self.assertEqual(len(_lineage_of([f"v{i}" for i in range(LINEAGE_MAX)])), LINEAGE_MAX)
        self.assertEqual(_lineage_of([f"v{i}" for i in range(LINEAGE_MAX + 1)]), [])

    def test_a_job_stored_before_the_field_still_loads(self) -> None:
        row = {"job": "j", "brief": "b"}
        self.assertEqual(_job_from(row).lineage, [])
        job = _job_from({**row, "lineage": ["v1"]})
        self.assertEqual(_job_from(asdict(job)).lineage, ["v1"])


class TestCopyCheck(unittest.TestCase):
    def rig(self) -> CopyCheck:
        """Version 1 built by A, version 2 by C (each from what came before), in the window."""
        cc = CopyCheck()
        self.assertIsNone(cc.check("v1", A, "d1", fp(V1), coldkey="owner-a"))
        self.assertIsNone(
            cc.check("v2", C, "d2", fp(V2), fp(V1), coldkey="owner-c", origins=["ws:v1"])
        )
        return cc

    def test_rebuilding_an_earlier_version_is_no_copy(self) -> None:
        cc = self.rig()
        seen: list[dict[str, Any]] = []
        found = cc.check(
            "v3",
            B,
            "d3",
            fp(V1),
            fp(V2),
            seen.append,
            coldkey="owner-b",
            origins=["ws:v2"],
            lineage=["v2", "v1"],
        )
        self.assertIsNone(found)
        # the pairs not compared are not in the hourly watch (as for the other skips)
        self.assertEqual([s["other_job"] for s in seen], [])

    def test_without_the_lineage_it_is_still_judged_a_copy(self) -> None:
        cc = self.rig()
        found = cc.check("v3", B, "d3", fp(V1), fp(V2), coldkey="owner-b", origins=["ws:v2"])
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found["job"], found["why"]), ("v1", "same normalised project"))

    def test_a_copy_of_another_project_is_still_caught_with_a_lineage(self) -> None:
        cc = self.rig()
        self.assertIsNone(cc.check("o1", C, "do", fp(OTHER), coldkey="owner-c"))
        seen: list[dict[str, Any]] = []
        found = cc.check(
            "v3", B, "d3", fp(OTHER), fp(V2), seen.append, coldkey="owner-b", lineage=["v2", "v1"]
        )
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["job"], "o1")
        self.assertEqual([s["other_job"] for s in seen], ["o1"])


class TestDesk(unittest.TestCase):
    """End to end: the claim's lineage is kept on the job and reaches the copy check."""

    def setUp(self) -> None:
        self.p = FakePlatform()
        self.desk = Desk(
            self.p,
            Ledger(),
            registered=lambda: [A, B],
            clock=Clock(),
            rng=random.Random(3),  # noqa: S311 -- a test's seed
        )
        for hk in (A, B):
            self.desk.agent_key(hk)
            self.desk.miners[hk].admitted = True

    def tearDown(self) -> None:
        self.desk.stop()

    def build(self, body: dict[str, Any], files: dict[str, str], hotkey: str) -> dict[str, Any]:
        """``hotkey`` alone may take the job: it builds ``files``."""
        other = B if hotkey == A else A
        self.desk.miners[other].blocked_until_ms = self.desk.now_ms() + 10**9
        self.desk.miners[hotkey].blocked_until_ms = 0
        self.p.queue.append(body)
        self.desk.work(hotkey)
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(hotkey)
        self.assertEqual(w["job"], body["job"])
        self.desk.accept(hotkey, body["job"])
        self.p.work(w["workspace"], files, deliverable=True)
        self.desk.submit(hotkey, body["job"], w["workspace"], "")
        # a copy sends the job on to another miner (queued again); a delivery ends it
        return wait_state(self.desk, body["job"], ("done", "queued"))

    def rebuild(self, **claim: Any) -> dict[str, Any]:
        st = self.build({"job": "v1", "brief": "A plug."}, V1, A)
        self.assertEqual(st["result"]["label"], "delivered")
        seed = self.p._new(frozen=True, files=V2)  # version 2, delivered
        return self.build({"job": "v3", "brief": "Undo it.", "seed": seed, **claim}, V1, B)

    def test_a_round_rebuilding_an_earlier_version_delivers(self) -> None:
        st = self.rebuild(lineage=["v2", "v1"])
        self.assertEqual(self.desk.jobs["v3"].lineage, ["v2", "v1"])
        self.assertEqual(st["result"]["label"], "delivered")
        self.assertEqual(self.desk.copied.get(B, 0), 0)

    def test_from_a_platform_without_the_field_it_is_a_copy_as_before(self) -> None:
        st = self.rebuild()
        self.assertEqual(self.desk.jobs["v3"].lineage, [])
        self.assertEqual(st["attempts"][0]["kind"], "copy")
        self.assertEqual(self.desk.copied.get(B, 0), 1)

    def test_the_operator_voids_a_wrong_copy_verdict(self) -> None:
        self.rebuild()
        self.assertEqual(self.desk.copied.get(B, 0), 1)
        with self.assertRaises(DeskError):
            self.desk.void("v3", "x", "pass")  # only rejections or copy verdicts
        with self.assertRaises(DeskError) as e:
            self.desk.void("v1", "x", "copy")  # no copy verdict there
        self.assertEqual(e.exception.status, 404)
        out = self.desk.void("v3", "it rebuilt its own project's earlier version", "copy")
        self.assertEqual(([v["hotkey"] for v in out["voided"]], out["kind"]), ([B], "copy"))
        self.assertEqual(self.desk.copied.get(B, 0), 0)
        self.assertEqual(self.desk._abuse_counts(B)["copies"], 0)
        copies = [o for o in self.desk.ledger.outcomes if o.job == "v3" and o.kind == "copy"]
        self.assertTrue(copies and all(o.void.startswith("operator: ") for o in copies))
        self.assertEqual(self.desk.void("v3", "twice", "copy")["already_void"], 1)
        self.assertEqual(self.desk.copied.get(B, 0), 0)  # voiding twice: nothing


if __name__ == "__main__":
    unittest.main()
