"""The calibration archive: every vote's model calls with
their raw replies, tokens, cost and latency; every job's row as it ended and as the store drops it;
the versions a result names; the operator's calibration tag, which never reaches the miner; and a
client's deletion, which removes the job's archive for good. No model is called: the model is a
fake."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from cadmind_einstein import __version__
from cadmind_einstein.archive import Archive
from cadmind_einstein.desk import DONE_KEEP_MS
from cadmind_einstein.models import Reply
from cadmind_einstein.review import RUBRIC_VERSION, ask_samples, read_bundle, rubric_digest
from cadmind_einstein.scoring import HOUR_MS, WINDOW_H
from tests.test_einstein_votes_stats import BRIEF, FakeModel, ReviewRig, make_bundle, reply


class _Rig(ReviewRig):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.archive = Archive(Path(tmp.name) / "archive", clock=self.clock)
        self.desk.archive = self.archive
        self.reviewer.archive = self.archive
        self.desk.judge = self.reviewer.judge


class TestVotesAreArchived(_Rig):
    def test_a_vote_keeps_every_call_raw_parsed_and_priced(self) -> None:
        self.model.replies = ["not json", reply(), reply(), reply()]
        hk = self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        votes = [r for r in self.archive.records("j1") if r["kind"] == "vote"]
        self.assertEqual(len(votes), 1)
        v = votes[0]
        self.assertEqual(v["hotkey"], hk)
        self.assertEqual(v["einstein"], __version__)
        self.assertEqual(v["judge"]["rubric_version"], RUBRIC_VERSION)
        self.assertEqual(v["judge"]["rubric"], rubric_digest())
        self.assertEqual(v["judge"]["model"], "fake:reviewer")
        self.assertEqual(v["bundle"]["sha256"], read_bundle(self.p.bundle).bundle_sha256)
        # four calls: the first unavailable, its raw words kept all the same
        self.assertEqual([c["ix"] for c in v["calls"]], [0, 1, 2, 3])
        self.assertEqual(v["calls"][0]["raw"], "not json")
        self.assertIsNone(v["calls"][0]["parsed"])
        self.assertIn("not one JSON object", v["calls"][0]["unavailable"])
        self.assertIn("screw holes", v["calls"][1]["raw"])
        self.assertEqual(v["calls"][1]["parsed"]["R2"]["score"], 3)
        self.assertEqual(v["calls"][1]["input_tokens"], 1000)
        self.assertAlmostEqual(v["calls"][1]["usd"], (1000 + 100) / 1_000_000)
        self.assertGreaterEqual(v["calls"][1]["latency_ms"], 0)
        self.assertEqual(v["cost"]["calls"], 4)
        self.assertAlmostEqual(v["cost"]["usd"], 4 * 1100 / 1_000_000)
        self.assertEqual(len(v["vote"]["samples"]), 3)
        self.assertEqual(v["unavailable"], ["the reply is not one JSON object alone"])
        # the store's row says it is archived, and carries the vote's cost
        (row,) = self.reviewer.votes_of("j1")
        self.assertTrue(row["archived"])
        self.assertEqual(row["cost"]["calls"], 4)
        # the public report still carries no words
        self.assertNotIn("screw holes", json.dumps(self.reviewer.report(["j1"])))

    def test_calls_that_made_no_vote_are_archived_too(self) -> None:
        self.model.replies = ["nope"] * 5
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "pending")
        (att,) = [r for r in self.archive.records("j1") if r["kind"] == "vote-attempt"]
        self.assertEqual(len(att["calls"]), 5)
        self.assertEqual(att["outcome"], "0 valid sample(s)")

    def test_a_provider_error_keeps_the_calls_made_before_it(self) -> None:
        class Breaks(FakeModel):
            def ask(self, system: str, text: str, images: Any) -> Reply:
                if self.calls >= 1:
                    self.calls += 1
                    raise TimeoutError("the provider timed out")
                return super().ask(system, text, images)

        self.reviewer.model = Breaks()
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "pending")
        (att,) = [r for r in self.archive.records("j1") if r["kind"] == "vote-attempt"]
        self.assertEqual(len(att["calls"]), 2)
        self.assertIn("TimeoutError", att["calls"][1]["error"])
        self.assertIn("model: TimeoutError", att["outcome"])

    def test_forget_before_drops_only_what_the_archive_holds(self) -> None:
        self.deliver()
        self.reviewer.pass_once()
        # a vote recorded before the archive existed: archived as it is dropped
        old = dict(self.reviewer.votes_of("j1")[0], job="old", archived=False)
        self.reviewer.kv.put("vote", "old|v", old)
        before = len(self.archive.records("j1"))
        later = self.desk.now_ms() + 3 * WINDOW_H * HOUR_MS
        self.reviewer.forget_before(later)
        self.assertEqual(self.reviewer.votes_of("j1"), [])
        self.assertEqual(len(self.archive.records("j1")), before)  # not archived twice
        (back,) = self.archive.records("old")
        self.assertTrue(back["backfill"])
        self.assertEqual(back["vote"]["value"], old["value"])

    def test_ask_samples_without_calls_is_unchanged(self) -> None:
        bundle = read_bundle(make_bundle())
        got, failed = ask_samples(FakeModel(), bundle, samples=3)
        self.assertEqual((len(got), failed), (3, []))


class TestJobsAreArchived(_Rig):
    def test_a_job_is_archived_as_it_ends_and_as_it_is_dropped_with_its_versions(self) -> None:
        hk = self.deliver()
        sent = self.p.completed["j1"]
        self.assertEqual(sent["versions"]["einstein"], __version__)
        self.assertEqual(sent["versions"]["deadline_s"], 60 * 60)  # medium
        self.assertEqual(sent["versions"]["judge"]["rubric"], rubric_digest())
        (ended,) = [r for r in self.archive.records("j1") if r["kind"] == "job"]
        self.assertEqual(ended["event"], "ended")
        self.assertEqual(ended["row"]["state"], "done")
        (a,) = ended["row"]["attempts"]
        self.assertEqual((a["hotkey"], a["kind"]), (hk, "pass"))
        self.assertTrue(a["assigned_ms"] and a["accepted_ms"] and a["submitted_ms"])
        self.assertEqual(ended["sent"]["label"], "delivered")
        # a week on, the store drops it: archived once more, as it went
        self.clock.t += DONE_KEEP_MS / 1000 + 60
        self.desk.tick()
        self.desk.wait_idle()
        self.assertNotIn("j1", self.desk.jobs)
        events = [r["event"] for r in self.archive.records("j1") if r["kind"] == "job"]
        self.assertEqual(events, ["ended", "dropped"])

    def test_the_calibration_tag_is_kept_and_never_reaches_the_miner(self) -> None:
        for hk in self.desk.miners:
            self.desk.work(hk)
        tag = {"set": "canary", "id": "K1", "generator": "briefs-1"}
        self.p.queue.append({"job": "c1", "brief": BRIEF, "calibration": tag})
        self.desk.tick()
        self.desk.wait_idle()
        self.assertEqual(self.desk.jobs["c1"].calibration, tag)
        works = [self.desk.work(hk) for hk in self.desk.miners]
        (w,) = [x for x in works if x.get("job") == "c1"]
        self.assertNotIn("calibration", w)
        self.assertNotIn("canary", json.dumps(w))
        self.assertNotIn("K1", json.dumps(w))

    def test_a_malformed_tag_is_dropped_not_refused(self) -> None:
        for hk in self.desk.miners:
            self.desk.work(hk)
        self.p.queue.append({"job": "c2", "brief": BRIEF, "calibration": {"x": "y" * 5000}})
        self.p.queue.append({"job": "c3", "brief": BRIEF, "calibration": "canary"})
        self.desk.tick()
        self.desk.wait_idle()
        self.assertEqual(self.desk.jobs["c2"].calibration, {"x": "y" * 200})
        self.assertEqual(self.desk.jobs["c3"].calibration, {})


class TestTheClientsDeletion(_Rig):
    def test_forget_removes_the_archive_and_nothing_is_archived_after(self) -> None:
        self.deliver()
        self.assertTrue(self.archive.records("j1"))
        out = self.desk.signal("j1", {"kind": "forget"})
        self.assertEqual(out, {"job": "j1", "forgotten": True})
        self.assertEqual(self.archive.records("j1"), [])
        # the vote made after the delete is not archived
        self.reviewer.pass_once()
        self.assertEqual(self.archive.records("j1"), [])
        # and a restart remembers it
        again = Archive(self.archive.root)
        self.assertFalse(again.append("j1", "vote", {}))
        self.assertTrue(again.append("j2", "vote", {}))

    def test_forget_of_an_unknown_job_is_answered(self) -> None:
        self.assertEqual(
            self.desk.signal("nope", {"kind": "forget"}), {"job": "nope", "forgotten": True}
        )


class TestArchiveFiles:
    def test_one_append_only_file_per_job_and_bad_ids_are_refused(self, tmp_path: Path) -> None:
        a = Archive(tmp_path / "archive")
        assert a.append("job-1", "job", {"n": 1})
        assert a.append("job-1", "job", {"n": 2})
        assert not a.append("../escape", "job", {})
        assert not a.append("", "job", {})
        assert [r["n"] for r in a.records("job-1")] == [1, 2]
        path = tmp_path / "archive" / "jobs" / "job-1.jsonl"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        with path.open("a") as f:
            f.write('{"torn"')
        assert [r["n"] for r in a.records("job-1")] == [1, 2]
        assert a.jobs() == ["job-1"]
        assert a.forget("job-1")
        assert not path.exists()
