"""The reference agent miner's command line: Claude Code's CLI restricted to the Watt MCP server by
default, or any agent CLI through an argv template."""

from __future__ import annotations

import argparse
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from cadmind_einstein import agent_miner
from cadmind_einstein.agent_miner import (
    PROMPT,
    RESUME,
    AgentRun,
    ClaudeSession,
    agent_argv,
    build,
    dropped,
    outcome,
    session_for,
)


def ns(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "agent_command": "",
        "agent_cli": "claude",
        "model": "",
        "agent_arg": [],
    }
    base.update(kw)
    return argparse.Namespace(**base)


class TestAgentArgv(unittest.TestCase):
    def call(self, a: argparse.Namespace) -> list[str]:
        return agent_argv(
            a,
            prompt="P",
            prompt_file=Path("/s/prompt.txt"),
            mcp_file=Path("/s/watt-mcp.json"),
            ws="ws-1",
            job="job-1",
        )

    def test_default_is_restricted_to_the_mcp_tools_and_names_no_model(self) -> None:
        argv = self.call(ns())
        self.assertEqual(argv[:3], ["claude", "-p", "P"])
        self.assertIn("--strict-mcp-config", argv)
        # the Watt tools, the local fetch tools (tests/test_fetch_mcp.py) and web search
        self.assertEqual(
            argv[argv.index("--allowedTools") + 1],
            "mcp__cadmind-watt,mcp__cadmind-fetch__fetch_to_workspace,mcp__cadmind-fetch__time_left,WebSearch",
        )
        self.assertNotIn("--model", argv)

    def test_model_and_extra_arguments_are_the_operators(self) -> None:
        argv = self.call(ns(agent_cli="/opt/bin/claude", model="m-1", agent_arg=["--verbose"]))
        self.assertEqual(argv[0], "/opt/bin/claude")
        self.assertEqual(argv[argv.index("--model") + 1], "m-1")
        self.assertEqual(argv[-1], "--verbose")

    def test_any_agent_cli_through_a_template(self) -> None:
        argv = self.call(
            ns(
                agent_command=(
                    "agent --mcp {mcp_config} --in {prompt_file} --ws {workspace} -m {model}"
                ),
                model="x",
            )
        )
        self.assertEqual(
            argv,
            [
                "agent",
                "--mcp",
                "/s/watt-mcp.json",
                "--in",
                "/s/prompt.txt",
                "--ws",
                "ws-1",
                "-m",
                "x",
            ],
        )

    def test_the_prompt_template_fills(self) -> None:
        text = PROMPT.format(
            ws="w",
            job="j",
            brief="A {braced} brief",
            minutes=30,
            deadline_utc="12:00",
            server="s",
            questions="",
            no_questions="no",
        )
        self.assertIn("A {braced} brief", text)
        self.assertIn("until 12:00 UTC", text)
        self.assertIn("time_left", text)
        self.assertIn("DONE <digest>", text)

    def test_a_change_round_is_delivered_only_when_the_whole_project_passes(self) -> None:
        said = " ".join(PROMPT.split())
        self.assertIn("is delivered only when the whole project passes `watt status`", said)
        self.assertIn("failures it inherits are part of the job", said)
        self.assertIn("your time is only what `time_left` says", said)

    def test_the_prompt_says_where_the_clients_photos_are_and_to_look_at_them(self) -> None:
        from cadmind_einstein.agent_miner import NO_QUESTIONS, QUESTIONS

        for text in (PROMPT, QUESTIONS, NO_QUESTIONS):
            self.assertIn("brief/photo-*", text)
            self.assertIn("view_image", text)

    def test_the_builder_is_told_to_read_the_parts_block_from_the_change_file(self) -> None:
        """The platform writes the parts the client selected in the 3D view after the client's
        words in the change file; `client_answers` gives only the words, so the builder must read
        the file itself."""
        from cadmind_einstein.agent_miner import questions_rule

        for work in ({}, {"no_questions": True}):
            said = " ".join(questions_rule(work, "ag-1").split())
            self.assertIn(
                "listing the parts the client selected in the 3D view, by their view key", said
            )
            self.assertIn("read the change file itself (`read_file` its `path`)", said)
            self.assertIn("`watt inspect` with `show: [<their keys>]` shows them", said)
        guide = " ".join(
            (Path(__file__).resolve().parents[1] / "docs" / "MINER.md").read_text().split()
        )
        self.assertIn("read the change file itself (`read_file` its `path`) for that block", guide)

    def test_a_continuation_says_its_base_and_an_unfinished_round_says_what_blocks(self) -> None:
        from cadmind_einstein.agent_miner import questions_rule

        plain = questions_rule({}, "ag-1")
        self.assertNotIn("CONTINUATION", plain)
        delivered = " ".join(
            questions_rule(
                {"continuation": True, "base": {"kind": "delivered", "workspace": "ag-ref"}},
                "ag-1",
            ).split()
        )
        self.assertIn("This is a CONTINUATION", delivered)
        self.assertIn("REVIEW/DESIGN-NOTES and `watt status`", delivered)
        self.assertIn("read-only workspace `ag-ref`", delivered)
        brief = " ".join(
            questions_rule({"continuation": True, "base": {"kind": "brief"}}, "ag-1").split()
        )
        self.assertIn("clear spec/, src/ and tests/ and build from brief/", brief)
        # no exit: the prompt asks for no UNFINISHED line, and no round that continues it
        self.assertNotIn("UNFINISHED", PROMPT)
        self.assertNotIn("continues it", PROMPT)


class TestCancelledJob(unittest.TestCase):
    def test_the_work_answer_says_when_a_job_is_gone(self) -> None:
        self.assertTrue(dropped({"job": None, "cancelled": "job-1"}, "job-1"))
        self.assertTrue(dropped({"job": "job-2"}, "job-1"))
        self.assertTrue(dropped({"job": None}, "job-1"))
        self.assertFalse(dropped({"job": "job-1"}, "job-1"))
        self.assertFalse(dropped({"error": "validator unreachable"}, "job-1"))
        self.assertFalse(dropped(None, "job-1"))

    def test_a_stopped_agent_ends_with_everything_it_started(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "child.pid"
            run = AgentRun(
                ["sh", "-c", f"sleep 30 & echo $! > {marker}; echo started; wait"],
                cwd=Path(d),
                stdin=None,
                grace_s=2.0,
            )
            for _ in range(100):
                if marker.exists() and marker.read_text().strip():
                    break
                time.sleep(0.05)
            t0 = time.monotonic()
            run.stop()
            out, _err, timed_out = run.wait(20)
            self.assertLess(time.monotonic() - t0, 10)
            self.assertFalse(timed_out)
            self.assertIn("started", out)
            child = int(marker.read_text())
            time.sleep(0.2)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)  # the agent's own children went with it

    def test_a_timeout_stops_it_too(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            run = AgentRun(["sleep", "30"], cwd=Path(d), stdin=None, grace_s=1.0)
            _out, _err, timed_out = run.wait(0.3)
            self.assertTrue(timed_out)
            self.assertTrue(run.stopped.is_set())

    def test_the_builtin_tools_are_denied(self) -> None:
        argv = agent_argv(
            ns(),
            prompt="P",
            prompt_file=Path("/s/p"),
            mcp_file=Path("/s/m"),
            ws="w",
            job="j",
        )
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Read", "Write", "Edit", "WebFetch", "Grep", "Glob"):
            self.assertIn(tool, denied)

    def test_web_search_is_allowed_and_web_fetch_is_not(self) -> None:
        """Web search runs on the model provider's side; downloads go through the fetch tool, so
        every source is stored in the project and cited."""
        argv = agent_argv(
            ns(),
            prompt="P",
            prompt_file=Path("/s/p"),
            mcp_file=Path("/s/m"),
            ws="w",
            job="j",
        )
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        self.assertIn("WebSearch", allowed)
        self.assertNotIn("WebSearch", denied)
        self.assertIn("WebFetch", denied)
        self.assertNotIn("WebFetch", allowed)
        self.assertFalse(set(allowed) & set(denied))

    def test_the_prompt_says_to_search_the_web_for_the_fitted_products(self) -> None:
        said = " ".join(PROMPT.split())
        self.assertNotIn("ONLY", said)
        self.assertIn(
            "with the {server} MCP tools, the local `fetch_to_workspace` tool and web search. Use "
            "web search to find drawings and dimensions of every product the device must fit (its "
            "outline AND the positions and sizes of its buttons, ports, grilles, camera and other "
            "protrusions); store each page you use with `fetch_to_workspace` (or with the hosted "
            "`fetch` when the local tool is not there) and cite it. A spec sheet's overall size is "
            "not enough for a part that fits around a product.",
            said,
        )


class TestNoHandOver(unittest.TestCase):
    """An unfinished project earns nothing: the prompt says so, an agent that stops early with time
    left is resumed once in its session, and what it said last goes to the client as ``summary``."""

    def test_the_prompt_has_no_hand_over_and_names_the_only_endings(self) -> None:
        said = " ".join(PROMPT.split())
        self.assertNotIn("hand over early", said)
        self.assertIn("There is no hand-over: an unfinished project earns nothing.", said)
        self.assertIn(
            "When the remaining work looks too large, start its most valuable piece (one control, "
            "one hold), build it, test it, then take the next.",
            said,
        )
        self.assertIn(
            "The only endings are `DONE`, `REJECT`, or `time_left` under 5 minutes.", said
        )
        msg = " ".join(RESUME.format(minutes=42).split())
        self.assertEqual(
            msg,
            "You stopped with 42 minutes left. There is no hand-over: an unfinished project "
            "earns nothing. Continue with the next most valuable piece, build it, test it, and "
            "keep going until `DONE`, `REJECT`, or `time_left` under 5 minutes.",
        )

    def test_claude_sessions_name_then_resume_the_same_session(self) -> None:
        s = ClaudeSession("sid-1")
        cmd = ["claude", "-p", "PROMPT", "--mcp-config", "m.json", "--model", "x"]
        self.assertEqual(
            s.first(cmd),
            [
                "claude",
                "--session-id",
                "sid-1",
                "-p",
                "PROMPT",
                "--mcp-config",
                "m.json",
                "--model",
                "x",
            ],
        )
        self.assertEqual(
            s.resume(cmd, "go on"),
            [
                "claude",
                "--resume",
                "sid-1",
                "-p",
                "go on",
                "--mcp-config",
                "m.json",
                "--model",
                "x",
            ],
        )
        with self.assertRaises(ValueError):
            s.resume(["agent", "--in", "f"], "go on")
        self.assertIsInstance(session_for(ns()), ClaudeSession)
        self.assertIsInstance(session_for(ns(agent_cli="/opt/bin/claude")), ClaudeSession)
        self.assertIsNone(session_for(ns(agent_command="agent --in {prompt_file}")))
        self.assertNotEqual(session_for(ns()).id, session_for(ns()).id)  # type: ignore[union-attr]

    NOW = 1_000_000.0
    CMD: ClassVar[list[str]] = ["claude", "-p", "PROMPT", "--strict-mcp-config"]

    def run_build(
        self,
        outputs: list[tuple[str, bool]],
        *,
        left_s: float,
        session: ClaudeSession | str | None = "default",
        cancelled: bool = False,
    ) -> tuple[agent_miner.Build, list[mock.Mock], list[str]]:
        """``outputs``: each run's (stdout, timed_out), in order; time is frozen at NOW and the
        deadline is ``left_s`` after it."""
        if session == "default":
            session = ClaudeSession("sid-1")
        assert not isinstance(session, str)
        runs: list[mock.Mock] = []

        def fake_run(argv: list[str], *, cwd: Path, stdin: str | None) -> mock.Mock:
            out, timed_out = outputs[len(runs)]
            run = mock.Mock(argv=argv, stdin=stdin)
            run.wait.return_value = (out, "", timed_out)
            run.stopped.is_set.return_value = timed_out or cancelled
            runs.append(run)
            return run

        logs: list[str] = []
        watched: list[object] = []
        with (
            mock.patch.object(agent_miner, "AgentRun", side_effect=fake_run),
            mock.patch.object(agent_miner.time, "time", return_value=self.NOW),
        ):
            got = build(
                list(self.CMD),
                session=session,
                cwd=Path("/w"),
                stdin=None,
                timeout_s=3000,
                deadline_ms=int((self.NOW + left_s) * 1000),
                watch=watched.append,
                log=logs.append,
                job="job-1",
            )
        self.assertEqual(watched[-1], None)  # the heartbeat is told when nothing runs
        return got, runs, logs

    def test_an_agent_that_stops_early_is_resumed_once_in_its_session(self) -> None:
        got, runs, logs = self.run_build(
            [("Hand-over note: the rest is too large.", False), ("built more\nDONE abc123", False)],
            left_s=45 * 60,
        )
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].argv[:3], ["claude", "--session-id", "sid-1"])
        argv = runs[1].argv
        self.assertEqual(argv[:4], ["claude", "--resume", "sid-1", "-p"])
        self.assertTrue(argv[4].startswith("You stopped with 45 minutes left."))
        self.assertEqual(argv[5:], ["--strict-mcp-config"])
        # its timeout: the time left minus the margin
        self.assertEqual(runs[1].wait.call_args.args[0], 45 * 60 - agent_miner.RESUME_MARGIN_S)
        self.assertTrue(got.resumed)
        self.assertEqual(got.text, "built more\nDONE abc123")
        self.assertIn("--- RESUMED with 45 min left ---", got.record)
        self.assertIn("Hand-over note", got.record)
        self.assertTrue(any("resuming it once" in m for m in logs))
        self.assertEqual(outcome("job-1", "ws", got.text)[1]["digest"], "abc123")

    def test_the_resume_happens_once_only(self) -> None:
        got, runs, _ = self.run_build(
            [("stopped", False), ("stopped again", False)], left_s=50 * 60
        )
        self.assertEqual(len(runs), 2)
        self.assertEqual(got.text, "stopped again")

    def test_no_resume_after_done_or_reject_or_with_little_time_left(self) -> None:
        for text in ("work\nDONE d1", "REJECT not-capable: we cannot build engines"):
            got, runs, _ = self.run_build([(text, False)], left_s=60 * 60)
            self.assertEqual(len(runs), 1, text)
            self.assertFalse(got.resumed)
        got, runs, logs = self.run_build([("stopped", False)], left_s=9 * 60)
        self.assertEqual(len(runs), 1)
        self.assertEqual(logs, [])

    def test_no_resume_after_a_timeout_or_a_cancelled_job(self) -> None:
        got, runs, _ = self.run_build([("partial", True)], left_s=30 * 60)
        self.assertEqual(len(runs), 1)
        self.assertTrue(got.timed_out)
        got, runs, _ = self.run_build([("partial", False)], left_s=30 * 60, cancelled=True)
        self.assertEqual(len(runs), 1)
        self.assertTrue(got.cancelled)

    def test_an_agent_command_cli_is_not_resumed_and_says_so(self) -> None:
        got, runs, logs = self.run_build([("stopped", False)], left_s=40 * 60, session=None)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].argv, self.CMD)  # no session flags for another CLI
        self.assertFalse(got.resumed)
        self.assertEqual(len(logs), 1)
        self.assertIn("cannot resume", logs[0])

    def test_without_done_the_final_text_is_the_summary(self) -> None:
        kind, body = outcome("job-1", "ws-1", "  Built the base.\n\n  The hinge   still fails.\n")
        self.assertEqual(kind, "submit")
        self.assertEqual(
            body,
            {
                "job": "job-1",
                "workspace": "ws-1",
                "digest": "",
                "summary": "Built the base. The hinge still fails.",
            },
        )
        long = "word " * 1000
        self.assertEqual(len(outcome("j", "w", long + "END")[1]["summary"]), 2000)
        self.assertTrue(outcome("j", "w", long + "END")[1]["summary"].endswith("END"))
        # an UNFINISHED line an agent writes anyway (the prompt no longer asks) still wins
        said = outcome("j", "w", "lots of log\nUNFINISHED: the clip is untested\n")[1]["summary"]
        self.assertEqual(said, "the clip is untested")
        self.assertNotIn("summary", outcome("j", "w", "work\nDONE d1\n")[1])
        self.assertNotIn("summary", outcome("j", "w", "  \n")[1])
        kind, body = outcome("j", "w", "REJECT not-allowed: a weapon")
        self.assertEqual(
            (kind, body["category"], body["message"]), ("reject", "not-allowed", "a weapon")
        )
        self.assertEqual(outcome("admission-1", "w", "REJECT not-allowed: x")[0], "submit")


if __name__ == "__main__":
    unittest.main()
