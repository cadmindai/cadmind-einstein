"""The miner's auto-update: release tags only, applied between jobs only, opt-out, rollback, a
dirty checkout left alone, follow-checkout mode, and the real miner loop through an update.

Every repository here is local (a bare repository plays the remote): no network."""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from cadmind_einstein import agent_miner
from cadmind_einstein import auto_update as au

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *GIT_ID, *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return p.stdout.strip()


class Origin:
    """A bare 'remote' with release tags of a toy ``cadmind_einstein``, and a clone of it."""

    def __init__(self, root: Path) -> None:
        self.dev, self.bare, self.clone = root / "dev", root / "origin.git", root / "clone"
        self.dev.mkdir()
        git(self.dev, "init", "-q", "-b", "main")
        self.release("0.6.0")
        subprocess.run(  # noqa: S603
            ["git", "init", "-q", "--bare", str(self.bare)],  # noqa: S607
            check=True,
            capture_output=True,
        )
        git(self.dev, "remote", "add", "origin", str(self.bare))
        git(self.dev, "push", "-q", "origin", "main", "--tags")
        subprocess.run(  # noqa: S603
            ["git", "clone", "-q", str(self.bare), str(self.clone)],  # noqa: S607
            check=True,
            capture_output=True,
        )
        git(self.clone, "-c", "advice.detachedHead=false", "checkout", "-q", "v0.6.0")

    def release(self, version: str, *, tag: str = "", says: str = "", broken: bool = False) -> None:
        pkg = self.dev / "cadmind_einstein"
        pkg.mkdir(exist_ok=True)
        (pkg / "__init__.py").write_text(f'__version__ = "{says or version}"\n')
        (pkg / "agent_miner.py").write_text("OK = (\n" if broken else "OK = True\n")
        git(self.dev, "add", "cadmind_einstein")
        git(self.dev, "commit", "-q", "-m", f"release {version}")
        git(self.dev, "tag", tag or f"v{version}")

    def publish(self, version: str, **kw: Any) -> None:
        self.release(version, **kw)
        git(self.dev, "push", "-q", "origin", "main", "--tags")

    def running(self) -> str:
        """What a fresh interpreter in the clone imports: the version the miner would run."""
        p = subprocess.run(
            [sys.executable, "-c", "import cadmind_einstein; print(cadmind_einstein.__version__)"],
            cwd=self.clone,
            capture_output=True,
            text=True,
            check=True,
        )
        return p.stdout.strip()


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def updater(o: Origin, mode: str = "release", **kw: Any) -> tuple[au.Updater, list[str], list[int]]:
    said: list[str] = []
    restarts: list[int] = []
    kw.setdefault("install", False)
    kw.setdefault("restart", lambda: restarts.append(1))
    up = au.Updater(mode, o.clone, log=said.append, current=o.running(), **kw)
    return up, said, restarts


class Repo(unittest.TestCase):
    def setUp(self) -> None:
        # the toy releases differ by one digit within one second: a cached .pyc would pass for both
        env = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.o = Origin(Path(self._tmp.name))


class TestVersions(unittest.TestCase):
    def test_release_tags_only_and_semver_order(self) -> None:
        self.assertEqual(au.release_of("v0.10.2"), (0, 10, 2))
        for not_a_release in ("v0.7.0-rc1", "0.7.0", "main", "v1.2", "release-1", "vX.Y.Z"):
            self.assertIsNone(au.release_of(not_a_release))
        tags = ["v0.6.0", "v0.9.0", "v0.10.0", "v0.10.0-rc1", "main", "v0.5.9"]
        self.assertEqual(au.newest_release(tags, "0.6.0"), "v0.10.0")  # not string order
        self.assertIsNone(au.newest_release(tags, "0.10.0"))
        self.assertIsNone(au.newest_release(["v0.5.0"], "0.6.0"))  # never a downgrade


class TestOptOut(unittest.TestCase):
    def test_the_flag_and_the_environment_turn_it_off(self) -> None:
        self.assertFalse(au.opted_out(False, {}))
        self.assertTrue(au.opted_out(True, {}))
        for off in ("0", "false", "no", "OFF", " 0 "):
            self.assertTrue(au.opted_out(False, {au.ENV: off}), off)
        self.assertFalse(au.opted_out(False, {au.ENV: "1"}))

    def test_from_args_says_off_and_makes_no_updater(self) -> None:
        ap = argparse.ArgumentParser()
        au.add_args(ap)
        for argv, env in (
            (["--no-auto-update"], {}),
            (["--auto-update", "off"], {}),
            ([], {au.ENV: "0"}),
        ):
            said: list[str] = []
            self.assertIsNone(au.from_args(ap.parse_args(argv), log=said.append, environ=env))
            self.assertEqual(said, ["auto-update: off"])

    def test_off_outside_a_git_checkout(self) -> None:
        ap = argparse.ArgumentParser()
        au.add_args(ap)
        said: list[str] = []
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "site-packages" / "cadmind_einstein" / "auto_update.py"
            got = au.from_args(
                ap.parse_args([]), log=said.append, environ={}, module_file=str(fake)
            )
        self.assertIsNone(got)
        self.assertIn("not running from a git checkout", said[0])

    def test_the_miner_takes_the_flags(self) -> None:
        base = ["--validator", "v", "--validator-hotkey", "h", "--platform", "p", "--state", "s"]
        a = agent_miner.parser().parse_args(base)
        self.assertEqual((a.auto_update, a.no_auto_update), ("release", False))
        a = agent_miner.parser().parse_args([*base, "--no-auto-update"])
        self.assertTrue(a.no_auto_update)


class TestIdleOnly(Repo):
    def test_a_check_never_applies_and_the_idle_point_does(self) -> None:
        clock = Clock()
        up, said, restarts = updater(self.o, clock=clock)
        self.o.publish("0.7.0")
        up.poll()  # what the background thread does while a job runs
        self.assertEqual(up.pending, "v0.7.0")
        self.assertEqual((self.o.running(), restarts), ("0.6.0", []))  # nothing applied mid-job
        self.assertTrue(
            up.between_jobs(), said
        )  # the job ended: applied at once, no wait for a check
        self.assertEqual((self.o.running(), restarts), ("0.7.0", [1]))
        self.assertIn("auto-update: restarting onto v0.7.0", said)

    def test_a_waiting_job_does_not_start_a_check_but_a_pending_update_goes_first(self) -> None:
        up, _, restarts = updater(self.o, clock=Clock())
        self.o.publish("0.7.0")
        self.assertFalse(up.between_jobs(job_waiting=True))  # no check while a job waits
        self.assertEqual(restarts, [])
        up.check()
        self.assertTrue(up.between_jobs(job_waiting=True))  # pending: restart before accepting
        self.assertEqual(restarts, [1])

    def test_checks_follow_the_interval(self) -> None:
        clock = Clock()
        up, _, restarts = updater(self.o, clock=clock, interval_s=1800)
        self.assertFalse(up.between_jobs())  # the first check: nothing newer
        self.o.publish("0.7.0")
        clock.t = 1799
        self.assertFalse(up.between_jobs())
        clock.t = 1800
        self.assertTrue(up.between_jobs())
        self.assertEqual(restarts, [1])

    def test_the_background_watch_checks_only_while_busy(self) -> None:
        up, _, _ = updater(self.o, clock=Clock(), interval_s=0.01)
        calls: list[int] = []
        up.poll = lambda: calls.append(1)  # type: ignore[method-assign]
        busy, stop = threading.Event(), threading.Event()
        t = up.watch(busy.is_set, stop)
        threading.Event().wait(0.2)
        self.assertEqual(calls, [])  # idle: the idle point checks, not the thread
        busy.set()
        for _ in range(200):
            if calls:
                break
            threading.Event().wait(0.01)
        stop.set()
        t.join(5)
        self.assertTrue(calls)
        self.assertFalse(t.is_alive())


class TestRollbackAndDirty(Repo):
    def test_a_release_that_does_not_import_is_rolled_back_and_not_tried_again(self) -> None:
        up, said, restarts = updater(self.o, clock=Clock())
        before = git(self.o.clone, "rev-parse", "HEAD")
        self.o.publish("0.7.0", broken=True)
        self.assertFalse(up.between_jobs())
        self.assertEqual((git(self.o.clone, "rev-parse", "HEAD"), restarts), (before, []))
        self.assertEqual(self.o.running(), "0.6.0")
        line = [s for s in said if "failed" in s]
        self.assertEqual(len(line), 1)
        self.assertIn("v0.7.0 failed (the new code does not import", line[0])
        self.assertIn("still running", line[0])
        up._last = float("-inf")
        self.assertFalse(up.between_jobs())  # the same tag again: skipped
        self.o.publish("0.7.1")
        up._last = float("-inf")
        self.assertTrue(up.between_jobs())  # a newer one is taken
        self.assertEqual(self.o.running(), "0.7.1")

    def test_a_tag_whose_code_says_another_version_is_rolled_back(self) -> None:
        up, said, restarts = updater(self.o, clock=Clock())
        self.o.publish("0.7.0", says="0.6.9")
        self.assertFalse(up.between_jobs())
        self.assertEqual((self.o.running(), restarts), ("0.6.0", []))
        self.assertTrue(any("says version '0.6.9', not '0.7.0'" in s for s in said))

    def test_a_failed_install_is_rolled_back_and_the_old_code_reinstalled(self) -> None:
        pips: list[str] = []

        def run(argv: list[str], timeout: float) -> tuple[int, str]:
            if "pip" in argv:
                pips.append(git(self.o.clone, "rev-parse", "--short", "HEAD"))
                return (1, "ERROR: no matching distribution") if len(pips) == 1 else (0, "")
            return au._run(argv, timeout, cwd=self.o.clone)

        up, said, restarts = updater(self.o, clock=Clock(), install=True, run=run)
        old = git(self.o.clone, "rev-parse", "--short", "HEAD")
        self.o.publish("0.7.0")
        self.assertFalse(up.between_jobs())
        self.assertEqual(restarts, [])
        self.assertEqual(pips[1], old)  # the previous commit reinstalled
        self.assertEqual(git(self.o.clone, "rev-parse", "--short", "HEAD"), old)
        self.assertTrue(any("reinstall failed: ERROR: no matching distribution" in s for s in said))

    def test_a_branch_checkout_goes_back_to_its_branch(self) -> None:
        git(self.o.clone, "checkout", "-q", "main")
        up, _, _ = updater(self.o, clock=Clock())
        self.o.publish("0.7.0", broken=True)
        self.assertFalse(up.between_jobs())
        self.assertEqual(git(self.o.clone, "symbolic-ref", "--short", "HEAD"), "main")

    def test_local_modifications_are_never_updated(self) -> None:
        init = self.o.clone / "cadmind_einstein" / "agent_miner.py"
        init.write_text("OK = 'mine'\n")
        up, said, restarts = updater(self.o, clock=Clock())
        self.o.publish("0.7.0")
        self.assertFalse(up.between_jobs())
        self.assertEqual((restarts, init.read_text()), ([], "OK = 'mine'\n"))
        self.assertEqual(git(self.o.clone, "describe", "--tags"), "v0.6.0")
        self.assertIn(
            "auto-update: local changes in the checkout: not updating 0.6.0 -> v0.7.0", said
        )

    def test_a_failed_fetch_is_one_line_and_nothing_changes(self) -> None:
        up, said, restarts = updater(self.o, clock=Clock(), remote="nowhere")
        self.assertFalse(up.between_jobs())
        self.assertEqual(restarts, [])
        self.assertTrue(said[0].startswith("auto-update: fetch from nowhere failed: "))


class TestFollowCheckout(Repo):
    def test_a_new_head_restarts_between_jobs_without_fetching(self) -> None:
        clock = Clock()
        up, said, restarts = updater(self.o, "follow-checkout", clock=clock)
        self.assertEqual(up.interval_s, au.CHECKOUT_INTERVAL_S)
        self.assertFalse(up.between_jobs())  # same HEAD
        self.o.publish("0.7.0")
        clock.t += au.CHECKOUT_INTERVAL_S
        self.assertFalse(up.between_jobs())  # nothing fetched: the remote's tag is not seen
        git(self.o.clone, "fetch", "-q", "--tags")
        git(self.o.clone, "-c", "advice.detachedHead=false", "checkout", "-q", "v0.7.0")
        clock.t += au.CHECKOUT_INTERVAL_S
        up.poll()  # found while busy ...
        self.assertEqual(restarts, [])
        self.assertTrue(up.between_jobs())  # ... and applied once idle
        self.assertEqual(restarts, [1])
        self.assertIn("restarting onto checkout HEAD", said[-1])

    def test_a_head_that_does_not_import_is_not_restarted_onto(self) -> None:
        clock = Clock()
        up, said, restarts = updater(self.o, "follow-checkout", clock=clock)
        self.o.publish("0.7.0", broken=True)
        git(self.o.clone, "fetch", "-q", "--tags")
        git(self.o.clone, "-c", "advice.detachedHead=false", "checkout", "-q", "v0.7.0")
        self.assertFalse(up.between_jobs())
        self.assertEqual(restarts, [])
        self.assertTrue(any("not restarting onto" in s and "does not import" in s for s in said))
        n = len(said)
        clock.t += au.CHECKOUT_INTERVAL_S
        self.assertFalse(up.between_jobs())  # the refused HEAD is not tried again
        self.assertEqual(len(said), n)

    def test_a_head_moved_back_restarts_nothing(self) -> None:
        up, _, restarts = updater(self.o, "follow-checkout", clock=Clock())
        start = git(self.o.clone, "rev-parse", "HEAD")
        git(self.o.clone, "checkout", "-q", "main")
        git(self.o.clone, *GIT_ID, "commit", "-q", "--allow-empty", "-m", "x")
        up.poll()
        git(self.o.clone, "-c", "advice.detachedHead=false", "checkout", "-q", start)
        self.assertFalse(up.between_jobs())
        self.assertEqual(restarts, [])


class TestMinerLoop(Repo):
    """The real miner loop: an accepted job is built and submitted with an update pending, then the
    idle poll applies it and restarts (an injected exec), and the clone runs the new version."""

    def test_idle_update_reexec_new_version(self) -> None:
        o = self.o
        events: list[str] = []
        phase = {"job": True}
        job = {"job": "job-1", "workspace": "ws-1", "accepted": True, "deadline_ms": 0}

        def signed(_kp: object, _base: str, path: str, body: dict, _to: str) -> tuple[int, Any]:
            if path == "/v1/miners/agent_key":
                return 200, {"key_id": "k1"}
            if path == "/v1/jobs/work":
                if phase["job"]:
                    events.append("work: job")
                    return 200, dict(job)
                events.append("work: idle")
                return 200, {}
            return 200, {}

        class Got:
            timed_out = cancelled = False
            record = text = "DONE"
            sessions: tuple[str, ...] = ()

        def build(*_a: object, **_k: object) -> Got:
            events.append("build start")
            o.publish("0.7.0")  # a release lands while the job runs
            up = state["updater"]
            up.check()  # found during the job ...
            events.append(f"build end on {o.running()}")  # ... never applied during it
            return Got()

        def send(_call: object, path: str, _body: dict, **_k: object) -> tuple[int, dict]:
            events.append(f"send {path}")
            phase["job"] = False
            return 200, {}

        def restart() -> None:
            events.append(f"exec onto {o.running()}")

        state: dict[str, Any] = {}
        real_from_args = au.from_args

        def from_args(a: Any, **kw: Any) -> au.Updater | None:
            up = real_from_args(a, restart=restart, install=False, current="0.6.0", **kw)
            state["updater"] = up
            return up

        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.dict(os.environ),
            mock.patch.object(au, "checkout_of", lambda _f=None: o.clone),
            mock.patch.object(au, "from_args", from_args),
            mock.patch.object(agent_miner, "signed", signed),
            mock.patch.object(agent_miner, "own_key_id", lambda _p, _k: "k1"),
            mock.patch.object(agent_miner, "build", build),
            mock.patch.object(agent_miner, "corrector", lambda *a, **k: None),
            mock.patch.object(agent_miner, "start_self_review", lambda *a, **k: None),
            mock.patch.object(agent_miner, "answers_round", lambda *a, **k: False),
            mock.patch.object(agent_miner, "outcome", lambda *a, **k: ("submit", {"digest": "d"})),
            mock.patch.object(agent_miner, "send", send),
            # the agent CLI's own checks: no CLI here, and a test never calls a model
            mock.patch.object(agent_miner, "agent_launchable", lambda _argv: ""),
            mock.patch.object(agent_miner, "probe_agent", lambda *_a, **_k: ""),
            mock.patch("sys.stdout", io.StringIO()) as out,
        ):
            os.environ.pop(au.ENV, None)
            key = Path(d) / "agent.key"
            key.write_text("k\n")
            code = agent_miner.main(
                [
                    "--validator", "v", "--validator-hotkey", "h", "--platform", "p",
                    "--state", d, "--dev-uri", "//Alice", "--no-version-check",
                    "--agent-key-file", str(key), "--auto-update-interval", "1800",
                ]
            )  # fmt: skip
        self.assertEqual(code, 0)
        self.assertEqual(
            events,
            [
                "work: job",
                "build start",
                "build end on 0.6.0",
                "send /v1/jobs/submit",
                "work: idle",
                "exec onto 0.7.0",
            ],
        )
        self.assertEqual(git(o.clone, "describe", "--tags"), "v0.7.0")
        log = out.getvalue()
        self.assertIn("auto-update: release every 30 min, between jobs only", log)
        self.assertIn("auto-update: v0.7.0 found; applied between jobs", log)
        self.assertIn("auto-update: updating 0.6.0 -> v0.7.0", log)


class TestExecSelf(unittest.TestCase):
    def test_it_reexecutes_the_same_command_line(self) -> None:
        with (
            mock.patch.object(au.os, "execv") as execv,
            mock.patch.object(
                au.sys, "orig_argv", ["python", "-m", "cadmind_einstein.agent_miner", "--x"]
            ),
        ):
            au.exec_self()
        execv.assert_called_once_with(
            sys.executable, [sys.executable, "-m", "cadmind_einstein.agent_miner", "--x"]
        )

    def test_a_failed_restart_keeps_the_miner_running(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}),
        ):
            o = Origin(Path(d))

            def boom() -> None:
                raise OSError("exec format error")

            up, said, _ = updater(o, clock=Clock(), restart=boom)
            o.publish("0.7.0")
            self.assertFalse(up.between_jobs())
            self.assertIn(
                "the restart failed (OSError: exec format error); still running", said[-1]
            )
            n = len(said)
            self.assertFalse(up.between_jobs())  # not tried again on the next idle poll
            self.assertEqual(len(said), n)


if __name__ == "__main__":
    unittest.main()
