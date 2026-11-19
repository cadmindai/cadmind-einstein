"""Judge 1.3c and the vote's finding:

- an assembly's bundle carries its vendored parts' own evidence (``parts.json``); 1.3c shows it to
  the judge as the fact sheet's ``subprojects``, with this validator's own vote on each part
  (``judged``), and 1.3b never does;
- a vote whose deciding sample rules a ``main`` line ``fails`` is sent to the platform as a finding
  (the line, its quote, the nudge on it), and sent again until the platform takes it;
- each vote of ours is sent to the platform as its score (``{score, value, count, judge_version}``),
  the votes recorded before that included, and sent again until the platform takes it."""

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
from cadmind_einstein.scoring import HOUR_MS, WINDOW_H
from cadmind_einstein.votes import (
    FINDING_TRIES,
    VOTE_OUT_TRIES,
    main_failure,
    part_votes,
    vote_out,
)

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


class VotePlatform(FindingPlatform):
    def __init__(self) -> None:
        super().__init__()
        self.votes: list[tuple[str, dict[str, Any]]] = []
        self.refuse_vote: list[int] = []

    def vote(self, job: str, vote: dict[str, Any]) -> dict[str, Any]:
        if self.refuse_vote:
            raise PlatformError("refused", self.refuse_vote.pop(0), "x")
        self.votes.append((job, dict(vote)))
        return {"ok": True}


class _VoteRig(ReviewRig):
    def setUp(self) -> None:
        super().setUp()
        self.p = self.reviewer.platform = self.desk.platform = VotePlatform()  # type: ignore[assignment]

    def scoring(self, score: int) -> None:
        self.model.replies = [(derive_reply(), look_reply(score))] * self.reviewer.samples


class TestTheVoteIsSentToThePlatform(_VoteRig):
    def test_a_recorded_vote_is_sent_once_with_its_score(self) -> None:
        self.scoring(2)
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        ((job, v),) = self.p.votes
        self.assertEqual(job, "j1")
        self.assertEqual(
            v,
            {
                "score": 2,
                "value": jm.V_MAP[2],
                "count": 1,
                "judge_version": self.reviewer.judge_cfg.version,
            },
        )
        self.assertIs(type(v["score"]), int)
        self.assertIs(type(v["count"]), int)
        self.assertEqual(v["value"], self.reviewer.votes_of("j1")[0]["value"])
        self.assertTrue(any('"event":"vote-sent"' in line for line in self.said))
        self.reviewer.pass_once()
        self.assertEqual(len(self.p.votes), 1)

    def test_a_404_is_final(self) -> None:
        self.p.refuse_vote = [404]
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        row = self.reviewer.kv.get("voteout", "j1")
        self.assertEqual((row["sent"], row["tries"]), (False, VOTE_OUT_TRIES))
        self.reviewer.pass_once()
        self.assertEqual(self.p.votes, [])

    def test_a_500_is_sent_again_on_the_next_pass(self) -> None:
        self.p.refuse_vote = [500, 429]
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        self.assertEqual(self.p.votes, [])
        self.assertEqual(self.reviewer.kv.get("voteout", "j1")["tries"], 1)
        self.reviewer.pass_once()  # 429: not final either
        self.assertEqual(self.p.votes, [])
        self.reviewer.pass_once()
        self.assertEqual([j for j, _ in self.p.votes], ["j1"])
        self.assertTrue(self.reviewer.kv.get("voteout", "j1")["sent"])

    def test_a_vote_never_taken_is_given_up_after_its_tries(self) -> None:
        self.p.refuse_vote = [503] * (VOTE_OUT_TRIES + 2)
        self.deliver()
        self.reviewer.pass_once()
        for _ in range(VOTE_OUT_TRIES + 2):
            self.reviewer.pass_once()
        self.assertEqual(len(self.p.refuse_vote), 2)  # asked VOTE_OUT_TRIES times, no more
        self.assertEqual(self.p.votes, [])

    def test_an_abstaining_vote_or_one_with_no_value_sends_nothing(self) -> None:
        self.deliver()
        self.reviewer.pass_once()
        kept = self.reviewer.votes_of("j1")[0]
        self.assertIsNotNone(vote_out(kept))
        self.assertIsNone(vote_out(dict(kept, abstain=True)))
        self.assertIsNone(vote_out(dict(kept, value=None, samples=[])))
        self.assertIsNone(vote_out(dict(kept, value=0.4567, samples=[])))  # a v1 vote's mean
        # held in the store without a queue row: the backfill leaves them out too
        me = self.reviewer.me
        self.reviewer.kv.put("vote", f"a1|{me}", dict(kept, job="a1", abstain=True))
        self.reviewer.kv.put("vote", f"a2|{me}", dict(kept, job="a2", value=None, samples=[]))
        self.reviewer._votes_queued = False
        self.assertEqual(self.reviewer.send_votes(), 0)
        self.assertEqual([j for j, _ in self.p.votes], ["j1"])
        self.assertEqual(sorted(self.reviewer.kv.items("voteout")), ["j1"])

    def test_the_value_alone_gives_its_score_back(self) -> None:
        got = vote_out({"value": 0.65, "judge_version": "cadmind-judge/1.3f"}, 0)
        self.assertEqual(
            got, {"score": 2, "value": 0.65, "count": 1, "judge_version": "cadmind-judge/1.3f"}
        )
        low = vote_out({"value": 0.3, "samples": [{"score": 3}, {"score": 1}, {"score": 1}]}, 2)
        self.assertEqual(low, {"score": 1, "value": 0.3, "count": 2})

    def test_a_vote_recorded_before_is_queued_and_sent_once(self) -> None:
        self.scoring(3)
        self.deliver()
        self.reviewer.pass_once()
        kept = self.reviewer.votes_of("j1")[0]
        me = self.reviewer.me
        # as an earlier build left them: votes in the store, nothing queued for the platform
        self.reviewer.kv.delete("voteout", "j1")
        self.reviewer.kv.put("vote", f"old|{me}", dict(kept, job="old", review="old"))
        self.reviewer.kv.put("vote", "old|other", dict(kept, job="old", validator="other"))
        self.reviewer.kv.put("vote", "theirs|other", dict(kept, job="theirs", validator="other"))
        self.p.votes.clear()
        self.reviewer._votes_queued = False  # a new process
        self.assertEqual(self.reviewer.pass_once(), "idle")
        sent = dict(self.p.votes)
        self.assertEqual(sorted(sent), ["j1", "old"])  # ours only
        self.assertEqual((sent["old"]["score"], sent["old"]["value"]), (3, 1.0))
        self.assertEqual((sent["j1"]["count"], sent["old"]["count"]), (1, 2))
        self.reviewer.pass_once()
        self.assertEqual(len(self.p.votes), 2)
        # and the votes are read for it once: a later pass does not look again
        self.reviewer.kv.put("vote", f"late|{me}", dict(kept, job="late"))
        self.reviewer.pass_once()
        self.assertEqual(len(self.p.votes), 2)

    def test_a_vote_already_queued_is_not_queued_again(self) -> None:
        self.p.refuse_vote = [404]
        self.deliver()
        self.reviewer.pass_once()
        self.reviewer._votes_queued = False
        self.assertEqual(self.reviewer.queue_unsent_votes(), 0)
        self.reviewer.pass_once()
        self.assertEqual(self.p.votes, [])  # the refusal stays final

    def test_a_forgotten_vote_takes_its_queue_row_with_it(self) -> None:
        self.deliver()
        self.reviewer.pass_once()
        self.reviewer.kv.put("voteout", "kept", {"job": "kept", "vote": {}, "sent": True})
        self.assertIsNotNone(self.reviewer.kv.get("voteout", "j1"))
        self.reviewer.forget_before(self.desk.now_ms() + 3 * WINDOW_H * HOUR_MS)
        self.assertEqual(self.reviewer.votes_of("j1"), [])
        self.assertIsNone(self.reviewer.kv.get("voteout", "j1"))
        self.assertIsNotNone(self.reviewer.kv.get("voteout", "kept"))


class TestAPlatformWithoutTheVoteRoute(_Rig):
    def test_it_is_tolerated_and_nothing_is_queued_for_it(self) -> None:
        self.assertFalse(hasattr(self.p, "vote"))
        self.deliver()
        self.assertEqual(self.reviewer.pass_once(), "voted")
        self.assertEqual(self.reviewer.send_votes(), 0)
        self.assertEqual(self.reviewer.pass_once(), "idle")
        # the vote is still queued as it is recorded: a platform that learns the route gets it
        self.assertFalse(self.reviewer.kv.get("voteout", "j1")["sent"])
        self.assertEqual(self.reviewer.kv.get("voteout", "j1")["tries"], 0)


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
