"""Judge 1.3c and the vote's finding:

- an assembly's bundle carries its vendored parts' own evidence (``parts.json``); 1.3c shows it to
  the judge as the fact sheet's ``subprojects``, with this validator's own vote on each part
  (``judged``), and 1.3b never does;
- a vote whose deciding sample rules a ``main`` line ``fails`` is sent to the platform as a finding
  (the line, its quote, the nudge on it), and sent again until the platform takes it."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_einstein_votes_stats import (
    BundlePlatform,
    ReviewRig,
    derive_reply,
    look_reply,
    make_bundle,
)

from cadmind_einstein import judge as jm
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.review import BundleError, read_bundle
from cadmind_einstein.votes import FINDING_TRIES, main_failure, part_votes

DIGEST = "sha256:" + "ab" * 32
PART = {
    "key": "arm",
    "count": 1,
    "purpose": "the arm that carries the load",
    "digest": DIGEST,
    "promises": ["carries"],
    "requirements": [
        {
            "id": "carries",
            "level": "must",
            "statement": "the arm carries the load",
            "verdict": "killed 'scale arm.web x0.5' by tests/test_arm.py::test_load",
            "proved": True,
            "promised": True,
        }
    ],
    "conforms": True,
    "problems": [],
    "decisions": [],
    "guard": "guard 9",
}


def with_parts(bundle: bytes, parts: list[dict[str, Any]], *, tamper: bool = False) -> bytes:
    """The bundle with a ``parts.json`` (named in its manifest)."""
    raw = json.dumps({"format": "cadmind-review-parts/1", "parts": parts}).encode()
    src = zipfile.ZipFile(io.BytesIO(bundle))
    manifest = json.loads(src.read("bundle.json"))
    manifest["parts_sha256"] = hashlib.sha256(raw).hexdigest()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            z.writestr(n, json.dumps(manifest) if n == "bundle.json" else src.read(n))
        z.writestr("parts.json", raw + (b" " if tamper else b""))
    return buf.getvalue()


def part_vote(verdict: str = "fails", score: int = 1) -> dict[str, Any]:
    """A recorded vote on a part, as the validator's store holds it."""
    look = json.loads(look_reply(score, verdicts=(verdict, "ok", "ok", "ok", "ok")))
    return {
        "job": "part1",
        "value": jm.V_MAP[score],
        "judge_version": "cadmind-judge/1.3c",
        "samples": [{"derived": json.loads(derive_reply()), **look}],
        "nudges": [{"id": "N1", "text": look["nudges"][0]["text"], "quote": "hold 5 kg"}],
    }


class TestTheBundlesParts(unittest.TestCase):
    def test_parts_json_is_read_and_checked(self) -> None:
        b = read_bundle(with_parts(make_bundle(), [PART]))
        self.assertEqual([p["key"] for p in b.parts], ["arm"])
        self.assertEqual(read_bundle(make_bundle()).parts, [])
        with self.assertRaises(BundleError):
            read_bundle(with_parts(make_bundle(), [PART], tamper=True))

    def test_1_3c_shows_the_parts_and_1_3b_withholds_them(self) -> None:
        inp = jm.inputs_from_bundle(read_bundle(with_parts(make_bundle(), [PART])))
        self.assertEqual(inp.facts["subprojects"][0]["key"], "arm")
        shown = jm.Judge("1.3c").look_turn("rate", inp)[0]["text"]
        hidden = jm.Judge("1.3b").look_turn("rate", inp)[0]["text"]
        self.assertIn('"subprojects"', shown)
        self.assertIn("test_arm.py::test_load", shown)
        self.assertNotIn('"subprojects"', hidden)

    def test_the_rule_is_in_1_3c_only_and_1_3b_is_kept(self) -> None:
        self.assertIn("Parts.", jm.Judge("1.3c").system_prompt())
        self.assertIn("counts as shown at the assembly", jm.Judge("1.3c").system_prompt())
        self.assertNotIn("Parts.", jm.Judge("1.3b").system_prompt())
        self.assertEqual(jm.VERSIONS["1.3b"].prompt, "judge_prompt_1.3b.md")
        self.assertEqual(jm.VERSIONS["1.3c"].prompt, "judge_prompt_1.3c.md")
        self.assertTrue(jm.VERSIONS["1.3c"].parts and not jm.VERSIONS["1.3b"].parts)
        self.assertEqual(jm.for_validator("1.3c").name, "1.3c")
        self.assertNotEqual(jm.judge_digest("1.3c"), jm.judge_digest("1.3b"))
        self.assertIn(
            '"judge_prompt_1.3c.md"',
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(),
        )


class TestPartVotes(unittest.TestCase):
    def test_the_latest_vote_of_ours_on_the_parts_digest(self) -> None:
        jobs = {
            "old": {"created_ms": 1, "result": {"digest": DIGEST}},
            "part1": {"created_ms": 2, "result": {"digest": DIGEST}},
            "other": {"created_ms": 3, "result": {"digest": "sha256:" + "cd" * 32}},
        }
        votes = {"old|me": part_vote("ok", 3), "part1|me": part_vote(), "other|me": part_vote()}
        got = part_votes([DIGEST], jobs, votes, "me")
        job, judged = got[DIGEST]
        self.assertEqual((job, judged["value"]), ("part1", 0.3))
        self.assertEqual(judged["lines"][0]["verdict"], "fails")
        self.assertTrue(judged["lines"][0]["main"])
        self.assertEqual(len(judged["nudges"]), 1)
        self.assertEqual(part_votes([DIGEST], jobs, votes, "someone else"), {})


class TestMainFailure(unittest.TestCase):
    def test_a_failing_main_line_is_a_finding_with_its_nudge(self) -> None:
        got = main_failure(part_vote())
        assert got is not None
        self.assertEqual((got["id"], got["quote"]), ("U1", "hold 5 kg"))
        self.assertIn("thin where it meets", got["nudge"])
        self.assertEqual(got["value"], 0.3)

    def test_no_finding_when_no_main_line_fails(self) -> None:
        self.assertIsNone(main_failure(part_vote("ok", 3)))
        self.assertIsNone(main_failure(part_vote("cannot_tell", 2)))
        vote = part_vote()
        vote["samples"][0]["derived"]["useless_if"][0]["main"] = False
        vote["samples"][0]["derived"]["useless_if"][1]["main"] = True
        self.assertIsNone(main_failure(vote))
        self.assertIsNone(main_failure({"samples": []}))


class FindingPlatform(BundlePlatform):
    def __init__(self) -> None:
        super().__init__()
        self.findings: list[tuple[str, dict[str, Any]]] = []
        self.refuse: list[int] = []

    def finding(self, job: str, finding: dict[str, Any]) -> dict[str, Any]:
        if self.refuse:
            raise PlatformError("refused", self.refuse.pop(0), "x")
        self.findings.append((job, dict(finding)))
        return {"ok": True}


class _Rig(ReviewRig):
    def setUp(self) -> None:
        super().setUp()
        self.p = self.reviewer.platform = self.desk.platform = FindingPlatform()  # type: ignore[assignment]


class TestTheVoteSendsItsFinding(_Rig):
    def failing(self) -> None:
        self.model.replies = [
            (derive_reply(), look_reply(1, verdicts=("fails",) + ("ok",) * 4))
        ] * self.reviewer.samples

    def test_a_main_fail_is_sent_once(self) -> None:
        self.failing()
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        ((job, f),) = self.p.findings
        self.assertEqual((job, f["id"], f["quote"]), ("j1", "U1", "hold 5 kg"))
        self.reviewer.pass_once()
        self.assertEqual(len(self.p.findings), 1)

    def test_a_passing_vote_sends_none(self) -> None:
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        self.assertEqual(self.p.findings, [])

    def test_a_refused_finding_is_sent_again_until_taken(self) -> None:
        self.failing()
        self.p.refuse = [503, 0]
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        self.assertEqual(self.p.findings, [])
        self.reviewer.pass_once()
        self.assertEqual(self.p.findings, [])
        self.reviewer.pass_once()
        self.assertEqual(len(self.p.findings), 1)

    def test_a_platform_that_does_not_know_the_route_is_not_asked_again(self) -> None:
        self.failing()
        self.p.refuse = [404]
        self.deliver()
        self.reviewer.pass_once()
        row = self.reviewer.kv.get("finding", "j1")
        self.assertEqual((row["sent"], row["tries"]), (False, FINDING_TRIES))
        self.reviewer.pass_once()
        self.assertEqual(self.p.findings, [])


class TestTheAssemblysJudgeSeesItsParts(_Rig):
    def test_our_vote_on_the_part_is_in_the_judges_facts(self) -> None:
        self.p.bundle = with_parts(make_bundle(), [PART])
        self.reviewer.kv.put("job", "part1", {"created_ms": 1, "result": {"digest": DIGEST}})
        self.reviewer.kv.put("vote", f"part1|{self.reviewer.me}", part_vote())
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        text = "".join(b.get("text", "") for b in self.model.seen[0][1][1] if b["type"] == "text")
        self.assertIn('"judged"', text)
        self.assertIn('"verdict": "fails"', text)


if __name__ == "__main__":
    unittest.main()
