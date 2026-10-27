"""The reference miner's self-review round (cadmind_einstein.self_review) and its bundle
(cadmind_einstein.miner_bundle), with a fake agent and a fake judge: no model is called."""

from __future__ import annotations

import argparse
import io
import json
import struct
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from cadmind_einstein import agent_miner, miner_bundle
from cadmind_einstein import self_review as sr
from cadmind_einstein.agent_miner import Build, ClaudeSession, corrector, start_self_review
from cadmind_einstein.miner_bundle import MinerBundle, bundle_from_export

T0 = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def nudge(nid: str, text: str = "the clip misses the rail") -> dict[str, Any]:
    return {"id": nid, "text": text, "view": "view-iso.png", "part": "clip", "quote": "a rail"}


class FakeJudge:
    """Answers each mode from a script; records every call."""

    def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, int | None, list[str]]] = []

    def __call__(self, mode: str, inputs: Any, model: Any, **kw: Any) -> dict[str, Any]:
        self.calls.append((mode, kw.get("samples"), [n["id"] for n in inputs.nudges]))
        return self.script[mode].pop(0)


class FakeAgent:
    """The builder's correction: each call answers the next text and takes ``minutes``."""

    def __init__(self, clock: Clock, answers: list[str], minutes: float = 6) -> None:
        self.clock, self.answers, self.minutes = clock, list(answers), minutes
        self.messages: list[tuple[str, float]] = []

    def __call__(self, message: str, budget_s: float) -> tuple[str, bool]:
        self.messages.append((message, budget_s))
        self.clock.now += self.minutes * 60
        return self.answers.pop(0), False


def bundle() -> MinerBundle:
    return MinerBundle(
        manifest={"images": [{"name": "view-iso.png", "kind": "analytic", "view": "iso"}]},
        images={"view-iso.png": b"\x89PNG\r\n\x1a\nfake"},
        client={"format": "x", "brief": "a clip for a rail"},
        facts={"format": "x"},
    )


def round_of(
    clock: Clock, judge: FakeJudge, agent: FakeAgent, *, left_min: float = 30, loops: int = 2
) -> sr.SelfReview:
    return sr.SelfReview(
        job="j1",
        built_text="I built the clip.\nDONE d1\n",
        deadline_ms=int((clock.now + left_min * 60) * 1000),
        export=lambda: b"zip",
        status=lambda: None,
        correct=agent,
        log=lambda m: None,
        cancelled=lambda: False,
        model=sr.CostMeter(object()),
        judge_fn=judge,
        bundle_fn=lambda data: bundle(),
        clock=clock,
        loops=loops,
    )


def full(rnd: sr.SelfReview) -> None:
    """What the miner does: run, the final check when it fits, the loops."""
    rnd.run()
    if rnd.final_pending and rnd.final_fits():
        rnd.final(after_submit=False)
    rnd.loops()


class TestRound(unittest.TestCase):
    def test_a_round_runs_and_corrects(self) -> None:
        clock = Clock()
        judge = FakeJudge(
            {
                "nudge": [{"samples": [{"nudges": [nudge("N1"), nudge("N2", "too thin")]}]}],
                "check": [
                    {
                        "verdicts": {
                            "N1": {"samples": ["upheld"] * 3, "verdict": "upheld"},
                            "N2": {"samples": ["not_upheld"] * 3, "verdict": "not_upheld"},
                        },
                        "samples": [],
                    }
                ],
                "final": [
                    {
                        "verdicts": {"N1": {"samples": ["gone"] * 3, "verdict": "gone"}},
                        "score": 3,
                        "nudges": [],
                        "samples": [],
                    }
                ],
            }
        )
        agent = FakeAgent(clock, ["Moved the clip onto the rail.\nDONE d2\n"])
        rnd = round_of(clock, judge, agent)
        full(rnd)
        self.assertEqual([c[0] for c in judge.calls], ["nudge", "check", "final"])
        self.assertEqual(judge.calls[0][1], 1)  # the reviewer: one sample
        self.assertEqual(judge.calls[1], ("check", 3, ["N1", "N2"]))
        self.assertEqual(judge.calls[2], ("final", 3, ["N1"]))
        self.assertEqual(len(agent.messages), 1)
        msg, budget = agent.messages[0]
        self.assertIn("N1: the clip misses the rail", msg)
        self.assertNotIn("too thin", msg)
        self.assertLessEqual(budget, 30 * 60 - sr.SUBMIT_RESERVE_S)
        self.assertIn("DONE d2", rnd.text)
        s = rnd.summary()
        self.assertEqual(s["delivered"], "corrected")
        self.assertEqual((s["counts"]["upheld"], s["counts"]["gone"]), (1, 1))
        self.assertEqual(s["final_score"], 3)
        self.assertIn("1 upheld, 1 gone", rnd.line())

    def test_a_new_point_in_the_final_check_gets_a_loop(self) -> None:
        clock = Clock()
        judge = FakeJudge(
            {
                "nudge": [{"samples": [{"nudges": [nudge("N1")]}]}],
                "check": [{"verdicts": {"N1": {"verdict": "upheld"}}, "samples": []}],
                "final": [
                    {
                        "verdicts": {"N1": {"verdict": "gone"}},
                        "score": 1,
                        "nudges": [nudge("N1", "the lid cannot open")],
                        "samples": [],
                    },
                    {
                        "verdicts": {"N1": {"verdict": "gone"}, "F1N1": {"verdict": "gone"}},
                        "score": 3,
                        "nudges": [],
                        "samples": [],
                    },
                ],
            }
        )
        agent = FakeAgent(clock, ["Fixed.\nDONE d2\n", "Hinged the lid.\nDONE d3\n"], minutes=4)
        rnd = round_of(clock, judge, agent, left_min=60)
        full(rnd)
        self.assertEqual(len(agent.messages), 2)
        loop_msg = agent.messages[1][0]
        self.assertIn("STILL THERE from before or NEW", loop_msg)
        self.assertIn("F1N1: the lid cannot open", loop_msg)
        self.assertEqual(judge.calls[-1], ("final", 3, ["F1N1"]))
        self.assertIn("DONE d3", rnd.text)
        s = rnd.summary()
        self.assertEqual(s["counts"]["loops_run"], 1)
        self.assertEqual(s["final_score"], 3)

    def test_an_unchanged_device_is_still_there_and_loops(self) -> None:
        clock = Clock()
        judge = FakeJudge(
            {
                "nudge": [{"samples": [{"nudges": [nudge("N1")]}]}],
                "check": [{"verdicts": {"N1": {"verdict": "upheld"}}, "samples": []}],
                "final": [{"verdicts": {"N1": {"verdict": "gone"}}, "score": 3, "nudges": []}],
            }
        )
        # the first correction delivers the same digest: still there by the rule, a loop follows
        agent = FakeAgent(clock, ["I answered it in words.\nDONE d1\n", "Moved it.\nDONE d2\n"])
        rnd = round_of(clock, judge, agent, left_min=60)
        full(rnd)
        self.assertEqual(len(agent.messages), 2)
        self.assertEqual(rnd.summary()["counts"]["gone"], 1)

    def test_too_little_time_skips_and_never_calls_anyone(self) -> None:
        clock = Clock()
        judge = FakeJudge({})
        agent = FakeAgent(clock, [])
        rnd = round_of(clock, judge, agent, left_min=10)
        t = time.monotonic()
        full(rnd)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual((judge.calls, agent.messages, rnd.text), ([], [], ""))
        self.assertIn("min left", rnd.summary()["skipped"])

    def test_no_time_for_a_correction_after_the_check(self) -> None:
        clock = Clock()

        class SlowJudge(FakeJudge):
            def __call__(self, mode: str, inputs: Any, model: Any, **kw: Any) -> dict[str, Any]:
                clock.now += 9 * 60  # the reviewer ate the time
                return super().__call__(mode, inputs, model, **kw)

        judge = SlowJudge({"nudge": [{"samples": [{"nudges": [nudge("N1")]}]}]})
        agent = FakeAgent(clock, [])
        rnd = round_of(clock, judge, agent, left_min=18)
        full(rnd)
        self.assertEqual([c[0] for c in judge.calls], ["nudge"])
        self.assertEqual(agent.messages, [])
        self.assertIn("before the check", rnd.summary()["skipped"])

    def test_a_hung_judge_is_abandoned_before_the_submit(self) -> None:
        release = threading.Event()

        def hung(*a: Any, **k: Any) -> dict[str, Any]:
            release.wait(5)
            return {}

        with self.assertRaises(TimeoutError):
            sr._bounded(hung, 0.05)
        release.set()

    def test_reserve(self) -> None:
        now = T0
        self.assertEqual(sr.reserve_s(int((now + 120 * 60) * 1000), now), 18 * 60)
        self.assertAlmostEqual(sr.reserve_s(int((now + 20 * 60) * 1000), now), 8 * 60)
        self.assertEqual(sr.reserve_s(0, now), 0.0)


def ns(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "self_review": "on",
        "self_review_model": sr.DEFAULT_MODEL,
        "self_review_key_file": None,
        "self_review_effort": "high",
        "self_review_base_url": "",
        "self_review_samples": 3,
        "self_review_loops": 2,
        "platform": "https://platform.invalid",
        "agent_command": "",
        "agent_cli": "claude",
        "model": "",
        "agent_arg": [],
    }
    base.update(kw)
    return argparse.Namespace(**base)


def built(text: str = "Built.\nDONE d1\n") -> Build:
    return Build(text, "", timed_out=False, cancelled=False)


class NoModel:
    """A judge model that must never be used."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the judge model was used ({name})")


class TestWiring(unittest.TestCase):
    def start(self, a: argparse.Namespace, got: Build, review_off: str, logs: list[str]) -> Any:
        return start_self_review(
            a,
            got,
            review_off=review_off,
            job="j1",
            ws="ws1",
            deadline_ms=int((time.time() + 3600) * 1000),
            key="k",
            questions_allowed=True,
            correct=lambda m, b: self.fail("no correction expected"),
            cancelled=lambda: False,
            log=logs.append,
            model=NoModel(),
        )

    def test_no_key_means_a_skip(self) -> None:
        why = sr.unavailable("on", sr.DEFAULT_MODEL, None)
        self.assertIn("no judge key", why)
        logs: list[str] = []
        got = built()
        self.assertIsNone(self.start(ns(), got, why, logs))
        self.assertEqual(got.text, "Built.\nDONE d1\n")
        self.assertTrue(any("self-review: skipped: no judge key" in m for m in logs))

    def test_an_empty_key_file_is_a_skip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "key"
            f.write_text("\n")
            self.assertIn("empty", sr.unavailable("on", sr.DEFAULT_MODEL, f))

    def test_off_never_runs(self) -> None:
        self.assertIn("off", sr.unavailable("off", sr.DEFAULT_MODEL, Path("/nonexistent")))
        logs: list[str] = []
        with mock.patch.object(sr, "SelfReview", side_effect=AssertionError("ran")):
            self.assertIsNone(self.start(ns(self_review="off"), built(), "", logs))
        self.assertEqual(logs, [])

    def test_no_done_means_no_round(self) -> None:
        logs: list[str] = []
        with mock.patch.object(sr, "SelfReview", side_effect=AssertionError("ran")):
            self.assertIsNone(self.start(ns(), built("I ran out of time."), "", logs))
        self.assertTrue(any("did not deliver" in m for m in logs))

    def test_the_round_gives_the_corrected_text_to_the_submit(self) -> None:
        got = built()
        fake = mock.Mock()
        fake.final_pending, fake.text = False, "Fixed.\nDONE d2\n"
        with mock.patch.object(sr, "SelfReview", return_value=fake):
            rnd = self.start(ns(), got, "", [])
        self.assertIs(rnd, fake)
        fake.run.assert_called_once()
        fake.loops.assert_called_once()
        self.assertEqual(got.text, "Fixed.\nDONE d2\n")


class FakeRun:
    made: ClassVar[list[tuple[list[str], str | None]]] = []

    def __init__(self, cmd: list[str], *, cwd: Path, stdin: str | None) -> None:
        FakeRun.made.append((cmd, stdin))
        self.stopped = threading.Event()
        self.proc = mock.Mock(returncode=0)

    def wait(self, timeout: float) -> tuple[str, str, bool]:
        return "Fixed.\nDONE d2\n", "", False


class TestCorrector(unittest.TestCase):
    def setUp(self) -> None:
        FakeRun.made = []

    def test_claude_code_resumes_its_session(self) -> None:
        got = built()
        got.session = ClaudeSession("sid-1")
        cmd = ["claude", "-p", "THE PROMPT", "--model", "m"]
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(agent_miner, "AgentRun", FakeRun),
        ):
            correct = corrector(
                ns(),
                got,
                cmd=cmd,
                prompt="THE PROMPT",
                prompt_file=Path(d) / "p",
                mcp_file=Path(d) / "m",
                work_dir=Path(d),
                ws="ws1",
                job="j1",
                watch=lambda r: None,
            )
            said, cancelled = correct("FIX N1", 600)
        self.assertEqual((said, cancelled), ("Fixed.\nDONE d2\n", False))
        self.assertEqual(
            FakeRun.made[0][0], ["claude", "--resume", "sid-1", "-p", "FIX N1", "--model", "m"]
        )
        self.assertIn("SELF-REVIEW", got.record)

    def test_an_agent_command_runs_fresh_told_of_its_work(self) -> None:
        got = built()
        a = ns(agent_command="my-agent --prompt {prompt_file}")
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(agent_miner, "AgentRun", FakeRun),
        ):
            pf = Path(d) / "p"
            correct = corrector(
                a,
                got,
                cmd=["my-agent"],
                prompt="THE PROMPT",
                prompt_file=pf,
                mcp_file=Path(d) / "m",
                work_dir=Path(d),
                ws="ws1",
                job="j1",
                watch=lambda r: None,
            )
            correct("FIX N1", 600)
            written = pf.read_text()
        cmd, stdin = FakeRun.made[0]
        self.assertEqual(cmd, ["my-agent", "--prompt", str(pf)])
        self.assertEqual(stdin, written)
        self.assertTrue(written.startswith("THE PROMPT"))
        self.assertIn("already holds your earlier work", written)
        self.assertIn("DONE d1", written)
        self.assertTrue(written.endswith("FIX N1"))


def stl(tris: list[tuple[tuple[float, float, float], ...]]) -> bytes:
    out = bytearray(80) + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0)
        for p in t:
            out += struct.pack("<3f", *p)
        out += b"\0\0"
    return bytes(out)


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> list[Any]:
    """A closed box, its faces wound outward."""
    v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    out = []
    for a, b, c, d in quads:
        out += [(v[a], v[b], v[c]), (v[a], v[c], v[d])]
    return out


class TestBundle(unittest.TestCase):
    def setUp(self) -> None:
        small = mock.patch.object(miner_bundle, "SIZE", 160)  # the same drawing, faster
        small.start()
        self.addCleanup(small.stop)

    def export(self) -> bytes:
        dev, world = box(0, 0, 0, 40, 20, 10), box(-50, -50, -5, 90, 70, 0)
        bodies = [
            {
                "key": "part:clip",
                "kind": "printed",
                "role": "part",
                "tri_start": 0,
                "tri_count": len(dev),
            },
            {
                "key": "world:desk",
                "kind": "component",
                "role": "world",
                "tri_start": len(dev),
                "tri_count": len(world),
            },
        ]
        report = {
            "deliverable": True,
            "needs": [
                {
                    "id": "fit",
                    "requirements": [
                        {"id": "fit", "level": "must", "statement": "it fits the rail"}
                    ],
                }
            ],
            "decisions": [
                {
                    "id": "w",
                    "by": "agent",
                    "status": "decided",
                    "question": "Width?",
                    "option": "20 mm",
                    "because": "not published",
                }
            ],
            "guard": {"ran": True, "mutants": 12},
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ws1/build/view/device.stl", stl(dev + world))
            z.writestr("ws1/build/view/device.json", json.dumps({"bodies": bodies}))
            z.writestr("ws1/build/report.json", json.dumps(report))
            z.writestr("ws1/spec/world.toml", '[world.desk]\nkind = "component"\n')
            z.writestr("ws1/brief/brief.md", "A clip for a rail.")
            z.writestr("ws1/brief/answer-1.md", "It is 20 mm wide.")
            z.writestr("ws1/brief/photo-1.jpg", b"\xff\xd8\xff photo")
        return buf.getvalue()

    def test_the_bundle_has_the_validators_picture_set_and_the_clients_words(self) -> None:
        b = bundle_from_export(self.export(), questions_allowed=False)
        names = sorted(b.images)
        self.assertEqual(len([n for n in names if n.startswith("view-")]), 8)
        self.assertEqual(len([n for n in names if n.startswith("world-")]), 8)
        self.assertEqual(
            [n for n in names if n.startswith("section-")],
            ["section-x.png", "section-y.png", "section-z.png"],
        )
        self.assertTrue(all(d.startswith(b"\x89PNG\r\n\x1a\n") for d in b.images.values()))
        kinds = {i["name"]: i["kind"] for i in b.manifest["images"]}
        self.assertEqual(kinds["world-iso.png"], "world")
        self.assertEqual(b.client["brief"], "A clip for a rail.")
        self.assertEqual(b.client["answers"][0]["text"], "It is 20 mm wide.")
        self.assertEqual(list(b.photos), ["photo-1.jpg"])
        self.assertEqual(b.facts["parts"]["size_mm"], [40.0, 20.0, 10.0])
        self.assertEqual(b.facts["requirements"][0]["statement"], "it fits the rail")
        self.assertFalse(b.facts["assumptions"]["questions"]["allowed"])
        self.assertEqual(b.facts["assumptions"]["items"][0]["built"], "20 mm")

        from cadmind_einstein.judge import inputs_from_bundle

        inp = inputs_from_bundle(b)
        self.assertEqual(len(inp.images), 19)
        self.assertEqual(inp.photos[0].name, "client-photo-1.jpg")

    def test_an_export_without_a_view_mesh_is_refused(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ws1/brief/brief.md", "x")
        with self.assertRaisesRegex(ValueError, "view mesh"):
            bundle_from_export(buf.getvalue())


if __name__ == "__main__":
    unittest.main()
