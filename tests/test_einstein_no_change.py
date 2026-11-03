"""A later round that builds nothing (DESIGN.md section 4): answers that pick what the device was
built on end "no change", uncharged and unscored; anything else fails the gate; a real copy is still
a copy, and a job without a seed never takes this path."""

from __future__ import annotations

import hashlib
import json
import random
import unittest
from typing import Any

from bittensor.sp_core import Keypair

from cadmind_einstein.answers import answers_only, report_digest
from cadmind_einstein.desk import NO_CHANGE, Desk, Job
from cadmind_einstein.scoring import Ledger
from cadmind_einstein.sealing import new_keypair
from tests.test_einstein import Clock, FakePlatform, project_zip, wait_state

A, B, C, D = (
    Keypair.create_from_uri(u).ss58_address for u in ("//Alice", "//Bob", "//Charlie", "//Dave")
)
DESIGN = {"src/parts/arm.txt": "\n".join(f"bracket arm line {i}" for i in range(40))}
DECISION = (
    '[decision.rib_count]\nby = "client"\nstatus = "{status}"\nassumption = "two"\n'
    'choice = "two"\nanswer = {{ from = "client: two (cadmind.ai, today)" }}\n'
    "[decision.rib_count.options]\none = 'one rib'\ntwo = 'two ribs'\n"
)
ANSWER = "brief/answer-2026-01-01-rib_count-120000.md"
PICKED = "# The client's answer\n\nAnswer: two (two ribs)\n\n(received 2026-01-01 12:00 UTC)\n"
WORDS = PICKED + "\nIn the client's words:\n\ntwo, and make the arm longer\n"


def design_digest(files: dict[str, str]) -> str:
    """As Watt's: the design alone. An answer picking the assumption, and its record, leave it."""
    kept = {k: v for k, v in files.items() if k.startswith("src/")}
    return "sha256:" + hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest()


class DigestPlatform(FakePlatform):
    def run(self, ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
        out = super().run(ws, command, args, **kw)
        digest = design_digest(self.ws[ws]["files"])
        for part in ("status", "guard"):
            if part in out["data"]:
                out["data"][part]["digest"] = digest
        return out

    def seed(self) -> str:
        files = {
            "brief/brief.md": "A bracket.",
            **DESIGN,
            "spec/decisions.toml": DECISION.format(status="open"),
        }
        files["build/report.json"] = json.dumps({"digest": design_digest(files)})
        return self._new(files=files, hotkey="validator", deliverable=True)

    def grant_continuation(
        self, hotkey: str, *, seed: str, base: str = "", name: str = "job"
    ) -> tuple[str, str]:
        ws = self._new(hotkey=hotkey, files=dict(self.ws[seed]["files"]))
        ref = self._new(hotkey=hotkey, files=dict(self.ws[base]["files"]), frozen=True)
        return ws, ref


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.p = DigestPlatform()
        self.ledger = Ledger(hour_ms=3_600_000)
        self.reg = [A, B, C, D]
        self.desk = Desk(
            self.p,
            self.ledger,
            registered=lambda: self.reg,
            clock=Clock(),
            rng=random.Random(7),  # noqa: S311 -- a test's seed
        )
        for hk in self.reg:
            self.desk.agent_key(hk, new_keypair()[1])
            self.desk.miners[hk].admitted = True

    def tearDown(self) -> None:
        self.desk.stop()

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def round(self, job: str, extra: dict[str, str], **body: Any) -> str:
        """The job claimed, its one builder handing back the seed plus ``extra``. Its hotkey."""
        for hk in self.reg:
            self.desk.work(hk)
        self.p.queue.append({"job": job, "brief": "Revision of an existing design.", **body})
        self.tick()
        hk, w = next((h, x) for h, x in ((h, self.desk.work(h)) for h in self.reg) if x["job"])
        self.desk.accept(hk, job)
        files = {**self.p.ws[w["workspace"]]["files"], **extra}
        self.p.work(w["workspace"], files, deliverable=True)
        self.desk.submit(hk, job, w["workspace"], "d", "The design already has two ribs.")
        wait_state(self.desk, job, ("queued", "done"))
        self.tick()
        return hk

    @property
    def recorded(self) -> dict[str, str]:
        return {"spec/decisions.toml": DECISION.format(status="answered")}


class TestNoChange(Case):
    def test_the_job_that_failed_four_times_as_a_copy(self) -> None:
        """Its shape: the seed was delivered by one miner, whose outputs the copy check holds; the
        client answered "two", the assumption; the builder recorded it, and the digest stayed."""
        seed = self.p.seed()
        digest = design_digest(self.p.ws[seed]["files"])
        self.desk.copies.check("earlier", D, digest, frozenset())
        hk = self.round("j", {ANSWER: PICKED, **self.recorded}, seed=seed)
        st = self.desk.status("j")
        res = st["result"]
        self.assertEqual((st["state"], res["label"], res["charge"]), ("done", NO_CHANGE, False))
        self.assertEqual(res["digest"], digest)
        self.assertEqual(res["workspace"], self.desk.jobs["j"].attempts[0].fork)
        self.assertEqual(res["summary"], "The design already has two ribs.")
        self.assertNotIn("copy", res)
        self.assertEqual(self.p.completed["j"]["label"], NO_CHANGE)
        # handed back at once: one attempt, no other miner, its fork kept
        self.assertEqual(len(self.p.grants), 1)
        self.assertEqual(self.desk.jobs["j"].attempts[0].kind, NO_CHANGE)
        self.assertNotIn(res["workspace"], self.p.deleted)
        # it can never be a way to earn anything, nor cost anything
        self.assertEqual(self.ledger.outcomes, [])
        self.assertEqual(self.desk.delivered.get(hk, 0), 0)
        self.assertEqual(self.desk.copied.get(hk, 0), 0)
        self.assertEqual(self.desk.failed.get(hk, 0), 0)
        self.assertIsNone(self.desk.work(hk)["job"])

    def test_a_changed_design_after_an_answer_is_a_delivery(self) -> None:
        seed = self.p.seed()
        moved = {"src/parts/arm.txt": "\n".join(f"new arm line {i}" for i in range(40))}
        self.round("j", {ANSWER: PICKED, **self.recorded, **moved}, seed=seed)
        res = self.desk.status("j")["result"]
        self.assertEqual((res["label"], res["charge"]), ("delivered", True))
        self.assertEqual(len(self.ledger.outcomes), 1)


class TestNothingBuilt(Case):
    def assert_fails_the_gate(self, job: str, hk: str) -> None:
        st = self.desk.status(job)
        res = st["result"]
        self.assertEqual((res["label"], res["charge"]), ("not finished", False))
        self.assertEqual(res["stage"], "unchanged")
        self.assertIn("was not built", res["error"])
        self.assertNotIn("copy", res)
        attempt = self.desk.jobs[job].attempts[0]
        self.assertEqual(attempt.kind, "gate")
        self.assertIn("was not built", attempt.outcome)
        self.assertEqual(self.desk.copied.get(hk, 0), 0)
        self.assertEqual(len(self.desk.jobs[job].attempts), 1)

    def test_a_change_request_a_builder_ignored(self) -> None:
        seed = self.p.seed()
        change = {"brief/change-2026-01-01-120000.md": "Make the arm longer."}
        self.assert_fails_the_gate("j", self.round("j", change, seed=seed))

    def test_a_change_request_sent_with_an_answer(self) -> None:
        seed = self.p.seed()
        extra = {
            ANSWER: PICKED,
            **self.recorded,
            "brief/change-2026-01-01-120001.md": "And make the arm longer.",
        }
        self.assert_fails_the_gate("j", self.round("j", extra, seed=seed))

    def test_an_answer_in_the_clients_own_words(self) -> None:
        seed = self.p.seed()
        extra = {ANSWER: WORDS, **self.recorded}
        self.assert_fails_the_gate("j", self.round("j", extra, seed=seed))

    def test_a_brief_saying_answered_with_no_answer_file(self) -> None:
        seed = self.p.seed()
        hk = self.round("j", {}, seed=seed, brief="What to change: Answered rib_count: two")
        self.assert_fails_the_gate("j", hk)

    def test_a_style_round_or_a_continuation_never_owes_nothing(self) -> None:
        seed = project_zip({"brief/brief.md": "x"})
        export = project_zip({"brief/brief.md": "x", ANSWER: PICKED})
        plain = Job(job="j", brief="b", seed="s")
        self.assertTrue(Desk._answers_changed_nothing(plain, seed, export))
        styled = Job(job="j", brief="b", seed="s", style={"baseline": {"digest": "c"}})
        self.assertFalse(Desk._answers_changed_nothing(styled, seed, export))
        continued = Job(job="j", brief="b", seed="s", continuation={"base": "w"})
        self.assertFalse(Desk._answers_changed_nothing(continued, seed, export))

    def test_the_builder_of_the_seed_gets_no_delivery_either(self) -> None:
        """Its own outputs never count as copies, so this one used to be delivered and charged."""
        seed = self.p.seed()
        digest = design_digest(self.p.ws[seed]["files"])
        for hk in self.reg:
            self.desk.copies.check("earlier", hk, digest, frozenset())
        change = {"brief/change-2026-01-01-120000.md": "Make the arm longer."}
        self.assert_fails_the_gate("j", self.round("j", change, seed=seed))

    def test_a_continuation_back_at_its_delivered_base(self) -> None:
        base = self.p.seed()
        unfinished = self.p.seed()
        self.p.ws[unfinished]["files"]["src/parts/arm.txt"] = "half an arm"
        delivered = {"src/parts/arm.txt": self.p.ws[base]["files"]["src/parts/arm.txt"]}
        hk = self.round("j", delivered, seed=unfinished, continuation={"base": base})
        self.assert_fails_the_gate("j", hk)


class TestOtherPathsStand(Case):
    def test_a_continuation_handing_in_its_unfinished_seed_is_a_first_delivery(self) -> None:
        base = self.p.seed()
        unfinished = self.p.seed()
        self.p.ws[unfinished]["files"].update(
            {"src/parts/arm.txt": "\n".join(f"unfinished arm line {i}" for i in range(40))}
        )
        files = self.p.ws[unfinished]["files"]
        files["build/report.json"] = json.dumps({"digest": design_digest(files)})
        self.round("j", {}, seed=unfinished, continuation={"base": base})
        self.assertEqual(self.desk.status("j")["result"]["label"], "delivered")

    def test_a_real_copy_of_another_project_is_still_a_copy(self) -> None:
        seed = self.p.seed()
        stolen = {"src/parts/arm.txt": "\n".join(f"another project line {i}" for i in range(40))}
        digest = design_digest({**self.p.ws[seed]["files"], **stolen})
        self.desk.copies.check("other", D, digest, frozenset())
        self.reg.remove(D)
        self.round("j", {ANSWER: PICKED, **self.recorded, **stolen}, seed=seed, exclude=[D])
        attempt = self.desk.jobs["j"].attempts[0]
        self.assertEqual(attempt.kind, "copy")

    def test_a_fresh_job_never_takes_this_path(self) -> None:
        files = {**DESIGN, "build/report.json": json.dumps({"digest": design_digest(DESIGN)})}
        self.round("j", {ANSWER: PICKED, **files})
        self.assertEqual(self.desk.status("j")["result"]["label"], "delivered")


class TestReading(unittest.TestCase):
    def test_answers_only(self) -> None:
        seed = project_zip({"brief/brief.md": "x", "brief/answer-2025-01-01-old-120000.md": WORDS})
        keep = {"brief/brief.md": "x", "brief/answer-2025-01-01-old-120000.md": WORDS}
        job = {"brief/job-2026-01-01-120000.toml": "no_questions = false\n"}
        self.assertFalse(answers_only(seed, project_zip(keep)))  # nothing new
        self.assertTrue(answers_only(seed, project_zip({**keep, ANSWER: PICKED})))
        self.assertTrue(answers_only(seed, project_zip({**keep, ANSWER: PICKED, **job})))
        self.assertFalse(answers_only(seed, project_zip({**keep, ANSWER: WORDS})))
        self.assertFalse(
            answers_only(seed, project_zip({**keep, ANSWER: PICKED, "brief/photo-1.jpg": "x"}))
        )
        self.assertFalse(answers_only(b"not a zip", project_zip(keep)))

    def test_report_digest(self) -> None:
        with_report = project_zip({"build/report.json": '{"digest": "sha256:a"}'})
        self.assertEqual(report_digest(with_report), "sha256:a")
        self.assertEqual(report_digest(project_zip({"build/report.json": "[1"})), "")
        self.assertEqual(report_digest(project_zip({"src/x": "y"})), "")
        self.assertEqual(report_digest(b"not a zip"), "")
