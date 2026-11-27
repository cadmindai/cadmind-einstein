"""The calibration archive: every vote's model conversations with
their raw answers, tokens, cost, latency, the judge's version, digest, mode and samples, the
instrument calls and their Watt compute; every job's row as it ended and as the store drops it;
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
from cadmind_einstein.judge import JUDGE_VERSION, Judge, inputs_from_bundle, judge_digest
from cadmind_einstein.models import Conversation
from cadmind_einstein.review import read_bundle
from cadmind_einstein.scoring import HOUR_MS, WINDOW_H
from cadmind_einstein.votes import cost_of
from tests.test_einstein_votes_stats import BRIEF, FakeModel, ReviewRig, make_bundle, sample


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
        self.model.replies = [("not json", "{}"), sample(), sample(), sample(1)]
        hk = self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        votes = [r for r in self.archive.records("j1") if r["kind"] == "vote"]
        self.assertEqual(len(votes), 1)
        v = votes[0]
        self.assertEqual(v["hotkey"], hk)
        self.assertEqual(v["einstein"], __version__)
        # the judge: its version, digest, mode and the samples asked
        self.assertEqual(v["judge"]["judge_version"], JUDGE_VERSION)
        self.assertEqual(v["judge"]["judge"], judge_digest())
        self.assertEqual(v["judge"]["judge_mode"], "rate")
        self.assertEqual(v["judge"]["samples"], 3)
        self.assertEqual(v["judge"]["model"], "fake:reviewer")
        self.assertEqual(v["bundle"]["sha256"], read_bundle(self.p.bundle).bundle_sha256)
        # four conversations, the first unavailable (its raw words kept all the same), then the
        # grouping call
        self.assertEqual([c["ix"] for c in v["calls"]], [0, 1, 2, 3, 4])
        self.assertEqual([c["kind"] for c in v["calls"]], ["sample"] * 4 + ["merge"])
        self.assertEqual(v["calls"][0]["raw"], ["not json", "{}"])
        self.assertIsNone(v["calls"][0]["parsed"])
        self.assertIn("not one JSON object", v["calls"][0]["unavailable"])
        self.assertIn("screw holes", v["calls"][1]["raw"][1])
        self.assertEqual(v["calls"][1]["parsed"]["score"], 2)
        self.assertEqual(v["calls"][1]["requests"], 2)
        self.assertEqual(v["calls"][1]["input_tokens"], 2000)
        self.assertEqual(v["calls"][1]["answered_by"], "fake:reviewer")
        self.assertAlmostEqual(v["calls"][1]["usd"], 2 * 1100 / 1_000_000)
        self.assertGreaterEqual(v["calls"][1]["latency_ms"], 0)
        self.assertEqual((v["calls"][1]["tool_calls"], v["calls"][1]["watt_compute_ms"]), ([], 0))
        self.assertEqual(v["cost"]["calls"], 5)
        self.assertAlmostEqual(v["cost"]["usd"], (4 * 2 + 1) * 1100 / 1_000_000)
        self.assertEqual((v["cost"]["tool_calls"], v["cost"]["watt_compute_ms"]), (0, 0))
        self.assertEqual([s["score"] for s in v["vote"]["samples"]], [2, 2, 1])
        self.assertEqual(v["vote"]["value"], 0.65)
        self.assertEqual(v["vote"]["judge_version"], JUDGE_VERSION)
        self.assertEqual(len(v["vote"]["nudges"]), 1)
        self.assertEqual(v["nudge_merge"]["filed"], 3)
        self.assertEqual(len(v["unavailable"]), 1)
        self.assertIn("not one JSON object", v["unavailable"][0])
        # the store's row says it is archived, and carries the vote's cost
        (row,) = self.reviewer.votes_of("j1")
        self.assertTrue(row["archived"])
        self.assertEqual(row["cost"]["calls"], 5)
        # the public report still carries no words
        self.assertNotIn("screw holes", json.dumps(self.reviewer.report(["j1"])))

    def test_calls_that_made_no_vote_are_archived_too(self) -> None:
        self.model.replies = [("nope", "nope")] * 5
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "pending")
        (att,) = [r for r in self.archive.records("j1") if r["kind"] == "vote-attempt"]
        self.assertEqual(len(att["calls"]), 5)
        self.assertEqual(att["outcome"], "0 valid sample(s)")

    def test_a_provider_error_keeps_the_calls_made_before_it(self) -> None:
        class Breaks(FakeModel):
            def converse(self, system: str, turns: Any, tools: Any = None) -> Conversation:
                if self.calls >= 1:
                    self.calls += 1
                    raise TimeoutError("the provider timed out")
                return super().converse(system, turns, tools)

        self.reviewer.model = Breaks()
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "pending")
        (att,) = [r for r in self.archive.records("j1") if r["kind"] == "vote-attempt"]
        self.assertEqual(len(att["calls"]), 2)
        self.assertIn("TimeoutError", att["calls"][1]["error"])
        self.assertIn("model: TimeoutError", att["outcome"])

    def test_tool_calls_and_watt_compute_are_summed(self) -> None:
        calls = [
            {
                "usd": 0.5,
                "tool_calls": [{"tool": "measure"}, {"tool": "inspect"}],
                "watt_compute_ms": 1200,
            },
            {"usd": None, "tool_calls": [], "watt_compute_ms": 0},
        ]
        got = cost_of(calls)
        self.assertEqual((got["calls"], got["tool_calls"], got["watt_compute_ms"]), (2, 2, 1200))
        self.assertEqual(got["usd"], 0.5)

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

    def test_the_judge_without_calls_is_unchanged(self) -> None:
        bundle = read_bundle(make_bundle())
        got = Judge().run("rate", inputs_from_bundle(bundle), FakeModel(), parallel=False)
        self.assertEqual((got["samples_valid"], got["unavailable"]), (3, []))


class TestJobsAreArchived(_Rig):
    def test_a_job_is_archived_as_it_ends_and_as_it_is_dropped_with_its_versions(self) -> None:
        hk = self.deliver()
        sent = self.p.completed["j1"]
        self.assertEqual(sent["versions"]["einstein"], __version__)
        self.assertEqual(sent["versions"]["deadline_s"], 240 * 60)  # the one deadline
        self.assertEqual(sent["versions"]["judge"]["judge"], judge_digest())
        self.assertEqual(sent["versions"]["judge"]["judge_version"], JUDGE_VERSION)
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


class TestBundleWithPhotos:
    def test_our_bundle_with_the_clients_photos_reads_as_before(self) -> None:
        import hashlib
        import io
        import zipfile

        plain = make_bundle()
        photo = b"\x89PNG\r\n\x1a\n the client's photo"
        listing = f"{hashlib.sha256(photo).hexdigest()}  photos/photo-1.png\n".encode()
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(plain)) as src, zipfile.ZipFile(out, "w") as z:
            for name in src.namelist():
                data = src.read(name)
                if name == "bundle.json":
                    manifest = json.loads(data)
                    manifest["photos"] = [
                        {"name": "photo-1.png", "sha256": hashlib.sha256(photo).hexdigest()}
                    ]
                    manifest["photos_sha256"] = hashlib.sha256(listing).hexdigest()
                    manifest["renderer"] = {"digest": "sha256:00", "python": "3.12"}
                    data = json.dumps(manifest).encode()
                z.writestr(name, data)
            z.writestr("photos.sha256", listing)
            z.writestr("photos/photo-1.png", photo)
        a, b = read_bundle(plain), read_bundle(out.getvalue())
        assert a.image_set == b.image_set
        assert a.images == b.images
        assert b.manifest["renderer"]["digest"] == "sha256:00"
        # the photos are read and checked, and go to the judge's blind turn 1 as client photos
        assert (a.photos, b.photos) == ({}, {"photo-1.png": photo})
        inp = inputs_from_bundle(b)
        assert [p.name for p in inp.photos] == ["client-photo-1.png"]
        turn1 = Judge().derive_turn(inp)
        assert [x["data"] for x in turn1 if x["type"] == "image"] == [photo]


def _with_models(manifest_extra: dict[str, object]) -> bytes:
    """`make_bundle`'s bundle as our validator's is when the client sent a 3D file of their part:
    ``models/<name>``, ``models.sha256`` and the manifest's ``models`` with ``models_sha256``."""
    import hashlib
    import io
    import zipfile

    model = b"solid bracket\nendsolid bracket\n"
    digest = hashlib.sha256(model).hexdigest()
    listing = f"{digest}  models/model-1.stl\n".encode()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(make_bundle())) as src, zipfile.ZipFile(out, "w") as z:
        for name in src.namelist():
            data = src.read(name)
            if name == "bundle.json":
                manifest = json.loads(data)
                manifest["models"] = [
                    {"name": "model-1.stl", "sha256": digest, "kind": "stl", "size": len(model)}
                ]
                manifest["models_sha256"] = hashlib.sha256(listing).hexdigest()
                manifest.update(manifest_extra)
                data = json.dumps(manifest).encode()
            z.writestr(name, data)
        z.writestr("models.sha256", listing)
        z.writestr("models/model-1.stl", model)
    return out.getvalue()


REBUILT = [
    {
        "source": "client_bracket",
        "file": "brief/model-1.stl",
        "name": "bracket v2.stl",
        "part": "bracket",
        "bound_mm": 0.1,
        "max_mm": 0.0312,
        "within": True,
        "changed": [
            {
                "region": "x 10..30 y 100..150 z 0..8",
                "asked": "add a 6 mm hole through the left ear",
                "share": 0.0213,
                "max_mm": 3.0,
            }
        ],
    }
]


class TestBundleWithModels:
    def test_a_bundle_with_the_clients_3d_files_reads_as_one_without(self) -> None:
        plain, ours = read_bundle(make_bundle()), read_bundle(_with_models({}))
        assert (ours.image_set, ours.images, ours.facts) == (
            plain.image_set,
            plain.images,
            plain.facts,
        )
        assert plain.models == []
        assert [(m["name"], m["kind"]) for m in ours.models] == [("model-1.stl", "stl")]
        # the listing alone states nothing of the design: the judge's facts are the fact sheet's
        inp = inputs_from_bundle(ours)
        assert inp.facts == inputs_from_bundle(plain).facts
        assert inp.photos == []  # a model is never a picture

    def test_manifest_keys_this_reader_does_not_know_are_no_failure(self) -> None:
        got = read_bundle(
            _with_models({"models_rebuilt": {}, "models_later": [1, 2], "models_measured": None})
        )
        assert "client_files" not in inputs_from_bundle(got).facts
        # a listing that is not a list of files is read as none, never refused
        assert read_bundle(_with_models({"models": "model-1.stl"})).models == []

    def test_what_the_manifest_states_of_the_files_is_lines_of_the_fact_sheet(self) -> None:
        from cadmind_einstein import judge as jm

        bundle = read_bundle(
            _with_models({"models_measured": ["model-1.stl"], "models_rebuilt": REBUILT})
        )
        inp = inputs_from_bundle(bundle)
        assert inp.facts["client_files"] == [
            "client file model-1.stl (stl): measured by the designer",
            (
                'part bracket is client file model-1.stl (the client\'s "bracket v2.stl") built'
                " again: at most 0.03 mm from the file outside the changed places (bound 0.10 mm);"
                " 1 changed place stated"
            ),
            (
                "part bracket, changed place 1: the box x 10..30 y 100..150 z 0..8 (mm, the file's"
                " coordinates), 2% of the file's surface, differs from the file by up to 3.00 mm;"
                ' the client\'s words: "add a 6 mm hole through the left ear"'
            ),
        ]
        # the judge reads them in turn 2 with the other facts; no model byte is in any turn
        judge = jm.Judge()
        look = judge.look_turn("rate", inp)
        assert "add a 6 mm hole through the left ear" in look[0]["text"]
        for block in [*judge.derive_turn(inp), *look]:
            if block["type"] == "text":
                assert "endsolid" not in block["text"]
            else:
                assert b"endsolid" not in block["data"]

    def test_the_lines_say_what_is_missing_and_guess_nothing(self) -> None:
        from cadmind_einstein.judge import client_file_lines

        models = [{"name": "model-1.stl", "kind": "stl"}, {"name": "model-2.step", "kind": "step"}]
        # a file the designer did not measure; an unchanged rebuild over the bound; one not compared
        lines = client_file_lines(
            {
                "models": models,
                "models_measured": {"model-1.stl": {"readings": 3}, "model-2.step": False},
                "models_rebuilt": {
                    "rebuilt": [
                        {
                            "file": "brief/model-1.stl",
                            "part": "knob",
                            "bound_mm": 0.1,
                            "max_mm": 0.42,
                            "within": False,
                            "changed": [],
                        },
                        {"file": "brief/model-2.step", "part": "cap", "changed": []},
                        {"part": "nameless"},
                    ]
                },
            }
        )
        assert lines == [
            "client file model-1.stl (stl): measured by the designer (3 readings cited)",
            "client file model-2.step (step): not measured by the designer",
            (
                "part knob is client file model-1.stl built again: at most 0.42 mm from the file"
                " outside the changed places (bound 0.10 mm; over the bound);"
                " no changed place stated"
            ),
            (
                "part cap is client file model-2.step built again: not compared with the file"
                " in this build; no changed place stated"
            ),
        ]
        # a rebuild alone says nothing of what was measured
        assert client_file_lines({"models": models, "models_rebuilt": REBUILT})[0].startswith(
            "part bracket is client file model-1.stl"
        )
        assert client_file_lines({"models": models}) == []
        assert client_file_lines({}) == []
