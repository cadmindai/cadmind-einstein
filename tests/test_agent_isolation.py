"""Claude Code's CLI runs isolated from the operator's own Claude Code configuration: no user
settings (hooks, plugins), no CLAUDE.md, no auto-memory, no skills or slash commands. The agent's
knowledge comes from Watt's ``watt_guide``. Every test here uses a temporary HOME; none reads or
writes the real one."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, ClassVar

from cadmind_einstein.agent_miner import (
    ISOLATION_ENV,
    ISOLATION_SETTINGS,
    AgentRun,
    ClaudeSession,
    agent_argv,
    agent_env,
    build,
    parser,
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


def argv_of(a: argparse.Namespace, mcp_file: Path = Path("/s/watt-mcp.json")) -> list[str]:
    return agent_argv(
        a, prompt="P", prompt_file=Path("/s/prompt.txt"), mcp_file=mcp_file, ws="ws-1", job="job-1"
    )


def value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class TestTheCommandLine(unittest.TestCase):
    def test_the_user_settings_stay_out(self) -> None:
        self.assertEqual(value(argv_of(ns()), "--setting-sources"), "project,local")

    def test_slash_commands_are_disabled_and_the_skill_tool_denied(self) -> None:
        argv = argv_of(ns())
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn("Skill", value(argv, "--disallowedTools").split(","))
        self.assertNotIn("Skill", value(argv, "--allowedTools").split(","))

    def test_the_sessions_settings_turn_off_synced_skills_plugins_and_memory(self) -> None:
        settings = json.loads(value(argv_of(ns()), "--settings"))
        self.assertEqual(settings, ISOLATION_SETTINGS)
        self.assertIs(settings["syncClaudeAiSkills"], False)
        self.assertIs(settings["syncClaudeAiPlugins"], False)
        self.assertIs(settings["autoMemoryEnabled"], False)
        self.assertEqual(settings["enabledPlugins"], {})

    def test_the_operator_lets_the_user_settings_back_in_only_by_asking(self) -> None:
        self.assertFalse(parser().parse_args(self.required()).agent_user_settings)
        a = parser().parse_args([*self.required(), "--agent-user-settings"])
        argv = argv_of(ns(agent_user_settings=a.agent_user_settings))
        self.assertEqual(value(argv, "--setting-sources"), "user,project,local")
        # everything else stays isolated
        self.assertIn("--disable-slash-commands", argv)
        self.assertEqual(json.loads(value(argv, "--settings")), ISOLATION_SETTINGS)
        self.assertIn("Skill", value(argv, "--disallowedTools").split(","))

    @staticmethod
    def required() -> list[str]:
        return [
            "--validator",
            "https://v",
            "--validator-hotkey",
            "5x",
            "--platform",
            "https://p",
            "--state",
            "/s",
        ]

    def test_the_operators_agent_args_still_come_last(self) -> None:
        argv = argv_of(ns(agent_arg=["--setting-sources", "user,project,local"]))
        self.assertEqual(argv[-2:], ["--setting-sources", "user,project,local"])
        self.assertLess(argv.index("--setting-sources"), len(argv) - 2)

    def test_a_resumed_session_keeps_every_flag(self) -> None:
        cmd = argv_of(ns())
        resumed = ClaudeSession("sid-1").resume(cmd, "go on")
        for flag in ("--setting-sources", "--settings", "--disable-slash-commands"):
            self.assertIn(flag, resumed)
        self.assertEqual(value(resumed, "--setting-sources"), "project,local")

    def test_another_agent_cli_gets_none_of_claude_codes_flags(self) -> None:
        argv = argv_of(ns(agent_command="my-agent --mcp {mcp_config}"))
        self.assertEqual(argv, ["my-agent", "--mcp", "/s/watt-mcp.json"])


class TestTheEnvironment(unittest.TestCase):
    def test_claude_md_and_auto_memory_are_off_for_claude_code(self) -> None:
        env = agent_env(ns(), base={"PATH": "/bin", "HOME": "/h"})
        self.assertEqual(env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual((env["PATH"], env["HOME"]), ("/bin", "/h"))  # the rest is the miner's
        self.assertEqual(
            set(ISOLATION_ENV),
            {"CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"},
        )

    def test_an_operator_value_cannot_turn_them_back_on(self) -> None:
        env = agent_env(ns(), base={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"})
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")

    def test_another_agent_cli_gets_the_miners_environment_unchanged(self) -> None:
        base = {"PATH": "/bin", "X": "y"}
        self.assertEqual(agent_env(ns(agent_command="my-agent"), base=base), base)


FAKE_CLAUDE = """#!/bin/sh
out="$FAKE_CLAUDE_OUT"
for arg in "$@"; do printf '%s\\n' "$arg" >> "$out.argv"; done
printf -- '---\\n' >> "$out.argv"
env > "$out.env"
echo "DONE d1"
"""


class TestAFakeClaudeBinary(unittest.TestCase):
    """The miner's own build loop, with a fake ``claude`` that records what it was given: the
    flags and the environment reach the process."""

    def test_the_child_gets_the_flags_and_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cli = root / "claude"
            cli.write_text(FAKE_CLAUDE)
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
            home = root / "home"
            home.mkdir()
            work = root / "empty"
            work.mkdir()
            out = root / "rec"
            a = ns(agent_cli=str(cli))
            base = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home),
                "FAKE_CLAUDE_OUT": str(out),
            }
            got = build(
                argv_of(a),
                session=ClaudeSession("sid-1"),
                cwd=work,
                stdin=None,
                timeout_s=30,
                deadline_ms=0,
                watch=lambda run: None,
                log=lambda msg: None,
                job="job-1",
                env=agent_env(a, base=base),
            )
            self.assertIn("DONE d1", got.text)
            argv = out.with_suffix(".argv").read_text().split("\n")
            env = dict(
                line.split("=", 1)
                for line in out.with_suffix(".env").read_text().splitlines()
                if "=" in line
            )
        self.assertEqual(value(argv, "--setting-sources"), "project,local")
        self.assertIn("--disable-slash-commands", argv)
        self.assertEqual(json.loads(value(argv, "--settings")), ISOLATION_SETTINGS)
        self.assertIn("Skill", value(argv, "--disallowedTools").split(","))
        for name, val in ISOLATION_ENV.items():
            self.assertEqual(env[name], val)
        self.assertEqual(env["HOME"], str(home))


class _Capture(http.server.BaseHTTPRequestHandler):
    """A model API that records every request body and answers each with an error: the CLI's
    request shows what it loaded (system prompt, tools, memory) and no model is ever called."""

    bodies: ClassVar[list[bytes]] = []

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length", 0))
        _Capture.bodies.append(self.rfile.read(n))
        body = json.dumps(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "capture only"}}
        ).encode()
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


MARKERS = ("STRAY-CLAUDE-MD-MARKER", "STRAY-SKILL-MARKER", "STRAY-MEMORY-MARKER")


@unittest.skipUnless(shutil.which("claude"), "Claude Code's CLI is not installed")
class TestTheRealCli(unittest.TestCase):
    """Claude Code's installed CLI, with a temporary HOME holding a user CLAUDE.md, a user skill, a
    user SessionStart hook and an auto-memory file for the work directory, and a local stand-in
    for the model API. A plain run loads all four (the control); the miner's run loads none. No
    credential is passed and no model is called."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.work, self.hooked = root / "home", root / "empty", root / "hook-ran"
        self.work.mkdir()
        user = self.home / ".claude"
        (user / "skills" / "strayskill").mkdir(parents=True)
        (user / "skills" / "strayskill" / "SKILL.md").write_text(
            "---\nname: strayskill\ndescription: STRAY-SKILL-MARKER does things\n---\nbody\n"
        )
        (user / "CLAUDE.md").write_text("STRAY-CLAUDE-MD-MARKER always say banana\n")
        hook = {"type": "command", "command": f"touch {self.hooked}"}
        (user / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}})
        )
        project = re.sub(r"[^A-Za-z0-9]", "-", str(self.work.resolve()))
        memory = user / "projects" / project / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text("- STRAY-MEMORY-MARKER remember bananas\n")
        self.mcp = root / "mcp.json"
        self.mcp.write_text(json.dumps({"mcpServers": {}}))
        _Capture.bodies = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def base_env(self) -> dict[str, str]:
        """Only what the CLI needs: the temporary HOME, the stand-in API and a placeholder key."""
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
            "ANTHROPIC_API_KEY": "placeholder-not-a-key",
            "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }

    def run_cli(self, argv: list[str], env: dict[str, str]) -> tuple[str, set[str]]:
        _Capture.bodies = []
        run = AgentRun(argv, cwd=self.work, stdin=None, env=env)
        out, err, timed_out = run.wait(120)
        self.assertFalse(timed_out, err)
        seen = b"\n".join(_Capture.bodies).decode(errors="replace")
        loaded = {m for m in MARKERS if m in seen}
        if self.hooked.exists():
            loaded.add("hook")
            self.hooked.unlink()
        return out + err, loaded

    def test_nothing_of_the_users_configuration_is_loaded(self) -> None:
        cli = shutil.which("claude")
        assert cli is not None
        _, control = self.run_cli([cli, "-p", "hi", "--output-format", "text"], self.base_env())
        if not _Capture.bodies and not control:
            self.skipTest("this CLI made no model request with a placeholder key")
        missing = {*MARKERS, "hook"} - control
        if missing:
            self.skipTest(f"this CLI version does not load {sorted(missing)} even unisolated")
        a = ns(agent_cli=cli)
        said, isolated = self.run_cli(argv_of(a, self.mcp), agent_env(a, base=self.base_env()))
        self.assertTrue(_Capture.bodies, said)  # the session did reach the model request
        self.assertEqual(isolated, set())
        self.assertNotIn("strayskill", b"\n".join(_Capture.bodies).decode(errors="replace"))
