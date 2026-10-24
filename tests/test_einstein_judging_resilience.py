"""The validator's judging under blips and restarts, its miners' time, its log and its overturn rule
(audit 2026-10-22):

- a failed poll of the validator's guard run is asked again before it counts; a judgement retried
  on our side, or re-judged after a restart, takes the guard run it already started up again
  instead of starting another (the fork cannot even be read while that run holds it);
- a miner is free for another job as soon as it submits, while ``max_inflight`` still bounds what
  the validator judges at once, and a job's end frees its miner of that job only;
- every job event is one structured log line, with no brief or client message in it, and the
  retry reason is kept on the attempt;
- a rejection delivered by the same owner's other hotkey is never an overturned rejection."""

from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
from typing import Any

from cadmind_einstein.desk import AbusePolicy, Desk, SecondOpinion, job_event
from cadmind_einstein.platform import OperatorClient, PlatformError
from cadmind_einstein.scoring import Ledger
from cadmind_einstein.sealing import new_keypair
from tests.test_einstein import GOOD, A, B, C, Clock, FakePlatform, wait_state
from tests.test_einstein_durable import Rig
from tests.test_einstein_votes_stats import ReviewRig

BRIEF = "A wall hook for a 12 mm towel rail."


class BlipPlatform(FakePlatform):
    """The validator's guard run (``report --mutate``) goes on on the platform while the validator
    loses it: its id is reported, then the poll fails (a network blip that outlasted the client's
    own retries). The fork answers 409 ``run_in_progress`` to any other command while the run holds
    it, as the platform does. ``attach`` waits for a run by its id; ``forget`` makes the platform
    no longer know it (404)."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[str] = []
        self.attached: list[tuple[str, float]] = []
        self.live: dict[str, tuple[str, str, Any]] = {}
        self.blip = True
        self.forget = False

    def run(self, ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
        if any(w == ws for w, _c, _a in self.live.values()):
            raise PlatformError("a command is still running", 409, "run_in_progress")
        if command == "report" and (args or {}).get("mutate"):
            run = f"g{len(self.started) + 1}"
            self.started.append(run)
            if kw.get("on_run") is not None:
                kw["on_run"](run)
            if self.blip:
                self.live[run] = (ws, command, args)
                raise PlatformError("platform unreachable: [Errno -3] name resolution", 0)
            return {**super().run(ws, command, args), "run": run}
        return super().run(ws, command, args)

    def attach(
        self, run: str, *, budget_s: float = 1800.0, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        self.attached.append((run, budget_s))
        if self.forget or run not in self.live:
            self.live.pop(run, None)
            raise PlatformError("no such run", 404, "not_found")
        ws, command, args = self.live.pop(run)
        return {**super().run(ws, command, args), "run": run}


def _desk(p: FakePlatform, clock: Clock, hks: tuple[str, ...], **kw: Any) -> Desk:
    desk = Desk(
        p,
        Ledger(),
        registered=lambda: list(hks),
        clock=clock,
        rng=random.Random(1),  # noqa: S311 -- a test's seed
        **kw,
    )
    for hk in hks:
        desk.agent_key(hk, new_keypair()[1])
        desk.miners[hk].admitted = True
    return desk


def _tick(desk: Desk) -> None:
    desk.tick()
    assert desk.wait_idle(30)


def _take(desk: Desk, p: FakePlatform, job: str, hk: str, **body: Any) -> dict[str, Any]:
    """``job`` queued and assigned to ``hk`` (the only miner polling), accepted."""
    p.queue.append({"job": job, "brief": BRIEF, **body})
    desk.work(hk)
    _tick(desk)
    w = desk.work(hk)
    assert w["job"] == job, w
    desk.accept(hk, job)
    return w


def _retry_now(desk: Desk) -> None:
    desk._retry_at.clear()
    _tick(desk)


# -- P1-1: the guard run is taken up again -------------------------------------------------------


class TestClientPolls(unittest.TestCase):
    """:class:`OperatorClient`'s run polls, with its HTTP call replaced."""

    def client(self, answers: list[Any]) -> tuple[OperatorClient, list[tuple[str, str]], list[str]]:
        calls: list[tuple[str, str]] = []
        logs: list[str] = []
        c = OperatorClient("http://platform.invalid", "op", log=logs.append, poll_backoff_s=0.0)

        def call(method: str, path: str, body: Any = None, **_: Any) -> Any:
            calls.append((method, path.split("?", maxsplit=1)[0]))
            got = answers.pop(0)
            if isinstance(got, Exception):
                raise got
            return got

        c._call = call  # type: ignore[method-assign]
        return c, calls, logs

    def blip(self) -> PlatformError:
        return PlatformError("platform unreachable: timed out", 0)

    def test_a_failed_poll_is_asked_again_and_the_run_is_kept(self) -> None:
        c, calls, logs = self.client(
            [
                {"state": "running", "run": "r1"},
                self.blip(),
                PlatformError("bad gateway", 502),
                {"state": "done", "ok": True, "run": "r1"},
            ]
        )
        seen: list[str] = []
        out = c.run("w", "report", {"mutate": True}, on_run=seen.append)
        self.assertEqual((out["state"], seen), ("done", ["r1"]))
        self.assertEqual([p for _m, p in calls].count("/api/agent/runs/r1"), 3)
        self.assertEqual(len(logs), 2)
        self.assertNotIn("op", " ".join(logs))  # no key in a line

    def test_three_retries_then_the_failure_counts(self) -> None:
        c, calls, _ = self.client([{"state": "running", "run": "r1"}] + [self.blip()] * 4)
        with self.assertRaises(PlatformError) as e:
            c.run("w", "report", {"mutate": True})
        self.assertEqual(e.exception.status, 0)
        self.assertEqual(len(calls), 1 + 4)  # the POST, the poll and its 3 retries

    def test_a_refusal_is_not_retried(self) -> None:
        c, calls, _ = self.client(
            [{"state": "running", "run": "r1"}, PlatformError("gone", 404, "not_found")]
        )
        with self.assertRaises(PlatformError):
            c.run("w", "report", {"mutate": True})
        self.assertEqual(len(calls), 2)

    def test_attach_polls_a_run_that_already_ended(self) -> None:
        c, calls, _ = self.client([{"state": "done", "ok": True, "run": "r7"}])
        out = c.attach("r7", budget_s=0)
        self.assertEqual(out["state"], "done")
        self.assertEqual(calls, [("GET", "/api/agent/runs/r7")])

    def test_a_stop_during_the_back_off_answers_running(self) -> None:
        c, _calls, _ = self.client([self.blip()])
        c.poll_backoff_s = 5.0
        stop = threading.Event()
        stop.set()
        self.assertEqual(c.attach("r1", stop=stop)["state"], "running")


class TestGateRunTakenUpAgain(unittest.TestCase):
    def setUp(self) -> None:
        self.p, self.clock = BlipPlatform(), Clock()
        self.logs: list[str] = []
        self.desk = _desk(self.p, self.clock, (A,), log=self.logs.append)
        self.addCleanup(self.desk.stop)

    def submit(self, job: str = "b1") -> None:
        w = _take(self.desk, self.p, job, A)
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(A, job, w["workspace"], "d")
        assert self.desk.wait_idle(30)

    def test_a_retry_after_a_lost_poll_attaches_to_the_same_run(self) -> None:
        self.submit()
        a = self.desk.jobs["b1"].attempts[-1]
        self.assertEqual((self.desk.status("b1")["state"], a.gate_run), ("judging", "g1"))
        self.assertTrue(a.gate_digest and a.gate_started_ms)
        self.clock.t += 600  # ten minutes of the run's budget spent
        _retry_now(self.desk)
        st = wait_state(self.desk, "b1", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")
        self.assertEqual(self.p.started, ["g1"])  # never a second guard run
        self.assertEqual([r for r, _ in self.p.attached], ["g1"])
        self.assertAlmostEqual(self.p.attached[0][1], self.desk.gate_run_s - 600, delta=1)
        a = self.desk.jobs["b1"].attempts[-1]
        self.assertEqual(a.outcome, "judged")
        self.assertIn("platform unreachable", a.last_retry)  # the reason survives the verdict
        self.assertEqual(self.p.forks, 1)

    def test_a_run_the_platform_forgot_is_started_again(self) -> None:
        self.submit("b2")
        self.p.forget, self.p.blip = True, False
        _retry_now(self.desk)
        st = wait_state(self.desk, "b2", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")
        self.assertEqual(self.p.started, ["g1", "g2"])
        self.assertEqual([r for r, _ in self.p.attached], ["g1"])

    def test_a_cancel_stops_the_run_it_would_have_attached_to(self) -> None:
        self.submit("b3")
        self.desk.cancel("b3")
        assert self.desk.wait_idle(30)
        self.assertIn("g1", self.p.cancelled_runs)
        _retry_now(self.desk)
        self.assertEqual(self.p.attached, [])


class TestGateRunAfterARestart(unittest.TestCase):
    def test_a_restart_mid_judgement_attaches_to_the_run_it_started(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = BlipPlatform()
            r = Rig(d, platform=p)
            r.admit(A, B, C)
            r.poll(A, B, C)
            p.queue.append({"job": "r1", "brief": BRIEF})
            r.tick()
            hk, w = r.holder("r1")
            assert r.desk is not None
            r.desk.accept(hk, "r1")
            p.work(w["workspace"], GOOD, deliverable=True)
            r.desk.submit(hk, "r1", w["workspace"], "d")
            assert r.desk.wait_idle(30)
            fork = r.desk.jobs["r1"].attempts[-1].fork
            assert r.store is not None
            row = r.store.get("job", "r1")["attempts"][-1]
            self.assertEqual((row["gate_run"], row["fork"]), ("g1", fork))  # persisted
            desk = r.restart()  # the validator dies while its guard run goes on
            try:
                st = wait_state(desk, "r1", ("done",), timeout=10)
                self.assertEqual(
                    (st["result"]["label"], st["result"]["workspace"]), ("delivered", fork)
                )
                self.assertEqual(p.started, ["g1"])
                self.assertEqual([x for x, _ in p.attached], ["g1"])
                self.assertEqual(p.forks, 1)
            finally:
                desk.stop()


# -- P1-2: the miner is free once it submits ------------------------------------------------------


class SlowGate(FakePlatform):
    """The validator's guard run held open until ``gate`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()

    def run(self, ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
        if command in ("test", "report") and (args or {}).get("mutate"):
            self.gate.wait(10)
        return super().run(ws, command, args, **kw)


class TestMinerFreeAtSubmit(unittest.TestCase):
    def setUp(self) -> None:
        self.p, self.clock = SlowGate(), Clock()
        self.addCleanup(self.p.gate.set)

    def desk(self, **kw: Any) -> Desk:
        desk = _desk(self.p, self.clock, (A,), **kw)
        self.addCleanup(desk.stop)
        return desk

    def submit_j1(self, desk: Desk) -> None:
        w = _take(desk, self.p, "j1", A)
        self.p.work(w["workspace"], {"src/a.py": "x"}, deliverable=True)
        desk.submit(A, "j1", w["workspace"], "sha256:x")

    def test_repro_r1_the_miner_takes_the_next_job_while_its_last_is_judged(self) -> None:
        desk = self.desk(max_inflight=2)
        self.submit_j1(desk)
        self.p.queue.append({"job": "j2", "brief": "A shelf bracket."})
        desk.work(A)
        desk.tick()
        st = wait_state(desk, "j2", ("assigned",))
        self.assertEqual(desk.status("j1")["state"], "judging")
        self.assertEqual(st["attempts"][0]["hotkey"], A)
        self.assertEqual(desk.work(A)["job"], "j2")
        desk.accept(A, "j2")
        self.p.gate.set()
        st = wait_state(desk, "j1", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")
        self.assertEqual(desk.miners[A].job, "j2")  # j1's end frees A of j1 only
        self.assertEqual(desk.work(A)["job"], "j2")

    def test_max_inflight_still_counts_the_judgement(self) -> None:
        desk = self.desk(max_inflight=1)
        self.submit_j1(desk)
        self.assertEqual(desk.miners[A].job, "")  # free...
        self.p.queue.append({"job": "j2", "brief": "A shelf bracket."})
        for _ in range(3):
            desk.work(A)
            desk.tick()
        self.assertNotIn("j2", desk.jobs)  # ...but the validator is at its bound: nothing claimed
        self.p.gate.set()
        wait_state(desk, "j1", ("done",))
        desk.work(A)
        _tick(desk)
        self.assertEqual(desk.work(A)["job"], "j2")

    def test_a_cancel_of_the_judged_job_leaves_the_miners_new_job_alone(self) -> None:
        desk = self.desk(max_inflight=2)
        self.submit_j1(desk)
        self.p.queue.append({"job": "j2", "brief": "A shelf bracket."})
        desk.work(A)
        desk.tick()
        wait_state(desk, "j2", ("assigned",))
        desk.cancel("j1")
        said = desk.work(A)
        self.assertEqual((said["job"], said.get("cancelled")), ("j2", None))
        self.assertFalse(desk.miners[A].after_cancel)
        self.assertEqual(desk.miners[A].cancelled_ms, 0)

    def test_a_late_attempt_still_holds_its_miner_until_the_verdict(self) -> None:
        desk = self.desk(max_inflight=2)
        _take(desk, self.p, "j1", A)
        self.clock.t += 2 * 3600
        desk.tick()
        self.assertEqual(desk.status("j1")["state"], "judging")
        self.assertEqual(desk.miners[A].job, "j1")


class TestRecoverFreesSubmittedMiners(unittest.TestCase):
    def test_a_restart_holds_no_miner_for_a_submitted_judgement(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            r = Rig(d)
            r.admit(A, B, C)
            r.poll(A, B, C)
            r.p.queue.append({"job": "s1", "brief": BRIEF})
            r.p.queue.append({"job": "s2", "brief": BRIEF})
            r.tick()
            assert r.desk is not None
            h1, _ = r.holder("s1")
            h2, _ = r.holder("s2")
            with r.desk._mut():
                for job, late in (("s1", False), ("s2", True)):
                    jb = r.desk.jobs[job]
                    jb.state = "judging"
                    jb.attempts[-1].accepted_ms = r.desk.now_ms()
                    jb.attempts[-1].submitted_ms = 0 if late else r.desk.now_ms()
            r.desk.wait_idle()
            r.desk.stop()
            assert r.store is not None
            r.store.close()
            desk = r.boot()  # not started: nothing is judged, only recovered
            try:
                self.assertEqual(desk.miners[h1].job, "")
                self.assertEqual(desk.miners[h2].job, "s2")
            finally:
                desk.stop()


# -- P2-3: one structured line per job event ------------------------------------------------------


def _events(logs: list[str]) -> list[dict[str, Any]]:
    return [json.loads(x.split(" ", 1)[1]) for x in logs if x.startswith("job-event ")]


class TestJobEvents(unittest.TestCase):
    def test_a_job_logs_each_event_without_the_brief_or_the_message(self) -> None:
        p, clock, logs = BlipPlatform(), Clock(), []
        desk = _desk(
            p,
            clock,
            (A, B),
            log=logs.append,
            second_opinion=SecondOpinion(coldkeys=1, wait_s=0.0),
        )
        self.addCleanup(desk.stop)
        desk.miners[B].blocked_until_ms = desk.now_ms() + 10**9
        _take(desk, p, "e1", A)
        private_words = "the client's private budget is 40 euros"
        desk.reject(A, "e1", "not-capable", private_words)
        assert desk.wait_idle(30)
        desk.miners[B].blocked_until_ms = 0
        w = _take(desk, p, "e1", B)
        p.work(w["workspace"], GOOD, deliverable=True)
        desk.submit(B, "e1", w["workspace"], "d", summary="all done")
        assert desk.wait_idle(30)
        _retry_now(desk)  # the blip: taken up again
        wait_state(desk, "e1", ("done",))
        ev = _events(logs)
        names = [e["event"] for e in ev]
        for want in (
            "assigned",
            "accepted",
            "rejected",
            "requeued",
            "submitted",
            "gate-start",
            "judge-retry",
            "gate-attach",
            "gate-run",
            "verdict",
            "ended",
        ):
            self.assertIn(want, names)
        self.assertTrue(all(e["job"] == "e1" for e in ev))
        rej = next(e for e in ev if e["event"] == "rejected")
        self.assertEqual((rej["hotkey"], rej["category"]), (A, "not-capable"))
        retry = next(e for e in ev if e["event"] == "judge-retry")
        self.assertIn("platform unreachable", retry["why"])
        self.assertEqual(retry["gate_run"], "g1")
        start = next(e for e in ev if e["event"] == "gate-start")
        self.assertEqual(start["run"], "g1")
        verdict = next(e for e in ev if e["event"] == "verdict")
        self.assertEqual((verdict["deliverable"], verdict["retries"]), (True, 1))
        self.assertIn("platform unreachable", verdict["last_retry"])
        self.assertEqual(ev[-1]["event"], "ended")
        self.assertEqual(ev[-1]["label"], "delivered")
        text = "\n".join(logs)
        for leak in (private_words, BRIEF, "all done", "cma_secret"):
            self.assertNotIn(leak, text)

    def test_the_line_is_json_and_drops_empty_fields(self) -> None:
        line = job_event("x", "j", a="", b=None, c=0, d=False)
        self.assertEqual(
            json.loads(line.split(" ", 1)[1]), {"event": "x", "job": "j", "c": 0, "d": False}
        )


class TestVoteEvent(ReviewRig):
    mode = "shadow"

    def test_a_recorded_vote_is_one_line(self) -> None:
        hk = self.deliver()
        while self.reviewer.pass_once() != "voted":
            self.clock.t += 3600
        ev = [e for e in _events(self.said) if e["event"] == "vote-recorded"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["job"], ev[0]["hotkey"], ev[0]["mode"]), ("j1", hk, "shadow"))
        self.assertIn("value", ev[0])


# -- P2-1: what an overturned rejection is -------------------------------------------------------


class TestOverturned(unittest.TestCase):
    def rig(self, cold: dict[str, str]) -> tuple[Desk, FakePlatform, Clock]:
        p, clock = FakePlatform(), Clock()
        desk = _desk(
            p,
            clock,
            (A, B, C),
            coldkeys=lambda: cold,
            second_opinion=SecondOpinion(coldkeys=1, wait_s=0.0),
            abuse=AbusePolicy(),
            max_inflight=2,
        )
        self.addCleanup(desk.stop)
        return desk, p, clock

    def overturn(
        self,
        desk: Desk,
        p: FakePlatform,
        clock: Clock,
        rejecter: str,
        deliverer: str,
        n: int,
        category: str,
    ) -> None:
        """``n`` jobs: ``rejecter`` rejects each (``category``), then ``deliverer`` delivers it."""
        others = [h for h in (A, B, C) if h not in (rejecter, deliverer)]
        for i in range(n):
            job = f"{category}-{rejecter[:4]}-{deliverer[:4]}-{i}"
            for h in [deliverer, *others]:
                desk.miners[h].blocked_until_ms = desk.now_ms() + 10**9
            _take(desk, p, job, rejecter, no_questions=True)
            desk.reject(
                rejecter, job, category, "Drop the LED? options: a: no LED; b: a bigger case"
            )
            assert desk.wait_idle(30)
            desk.miners[deliverer].blocked_until_ms = 0
            w = _take(desk, p, job, deliverer)
            p.work(w["workspace"], {"src/a.py": f"v{i}{job}"}, deliverable=True)
            desk.submit(deliverer, job, w["workspace"], "sha256:x")
            assert desk.wait_idle(30)
            self.assertEqual(desk.status(job)["result"]["label"], "delivered")
            for h in others:
                desk.miners[h].blocked_until_ms = 0
            clock.t += 3600

    def test_the_same_owners_delivery_overturns_nothing(self) -> None:
        desk, p, clock = self.rig({A: "owner", B: "owner", C: "other"})
        self.overturn(desk, p, clock, A, B, 3, "not-capable")
        self.assertEqual(desk.overturned.get(A, 0), 0)
        self.assertEqual(desk.enforce(), [])

    def test_another_owners_delivery_still_overturns_and_three_disable(self) -> None:
        desk, p, clock = self.rig({A: "a", B: "b", C: "c"})
        self.overturn(desk, p, clock, A, C, 3, "not-capable")
        self.assertEqual(desk.overturned.get(A, 0), 3)
        acted = desk.enforce()
        self.assertEqual([x["hotkey"] for x in acted], [A])
        self.assertIn("3 overturned", acted[0]["reason"])

    def test_a_withdrawn_delivery_takes_back_only_what_it_counted(self) -> None:
        desk, p, clock = self.rig({A: "a", B: "c", C: "c"})
        self.overturn(desk, p, clock, A, C, 1, "not-capable")
        self.overturn(desk, p, clock, B, C, 1, "not-capable")  # one owner's: not counted
        desk.overturned[B] = 5  # an earlier, counted overturn of B's must stay counted
        self.assertEqual(desk.overturned[A], 1)
        for job in [j for j in desk.jobs if desk.jobs[j].result.get("label") == "delivered"]:
            desk._uncharged(job, {"label": "cancelled"})
        self.assertEqual((desk.overturned[A], desk.overturned[B]), (0, 5))


if __name__ == "__main__":
    unittest.main()
