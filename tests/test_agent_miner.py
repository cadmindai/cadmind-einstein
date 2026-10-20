"""The reference agent miner's command line: Claude Code's CLI restricted to the Watt MCP server by
default, or any agent CLI through an argv template."""

from __future__ import annotations

import argparse
import os
import tempfile
import time
import unittest
from pathlib import Path

from cadmind_einstein.agent_miner import PROMPT, AgentRun, agent_argv, dropped


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
        # the Watt MCP server's tools and the one local fetch tool (tests/test_fetch_mcp.py)
        self.assertEqual(
            argv[argv.index("--allowedTools") + 1],
            "mcp__cadmind-watt,mcp__cadmind-fetch__fetch_to_workspace,mcp__cadmind-fetch__time_left",
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
        self.assertIn("UNFINISHED: <what still blocks delivery", PROMPT)


if __name__ == "__main__":
    unittest.main()


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
