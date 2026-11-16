"""Refused grants never loop: a miner at its quota of open job workspaces is held off and the job
goes to another miner; a job whose grant can never succeed (its seed workspace gone) ends "didn't
finish" after a few tries spread over its back-off; a grant a restart cut off has its workspace
frozen before the miner works again; a job that ends has every workspace granted for it frozen;
grants are bounded per minute, so the judging always has the key's headroom."""

from __future__ import annotations

import random
import tempfile
import unittest
from typing import Any

from cadmind_einstein.desk import (
    GRANT_LOG_EVERY_S,
    GRANT_PERMANENT_TRIES,
    MINER_COOL_S,
    Attempt,
    Desk,
)
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.scoring import Ledger
from tests.test_einstein import GOOD, OWNER, A, B, C, Clock, D, FakePlatform, wait_state
from tests.test_einstein_durable import Rig

QUOTA = 2
QUOTA_WORDS = "this miner holds 2 open job workspace(s) already: freeze one first"


class QuotaPlatform(FakePlatform):
    """The platform's grant rules that matter here: a miner holds at most ``QUOTA`` open job
    workspaces (429 ``miner_job_quota``), a seed that no longer exists is a 404, and the
    validator's key has a per-minute rate (429 ``rate_limit``) over every call it makes."""

    def __init__(self, clock: Clock | None = None, *, rate: int = 0) -> None:
        super().__init__()
        self.clock, self.rate = clock, rate
        self.calls: list[tuple[float, str, str]] = []  # (time, call, hotkey or workspace)
        self.rate_refused: list[str] = []
        self.sticky = False  # close_grants frees nothing (another party's live work)
        self.grant_error: PlatformError | None = None
        self.freezes: list[str] = []

    def _count(self, call: str, what: str = "") -> None:
        t = self.clock() if self.clock is not None else 0.0
        if self.rate and sum(1 for x, _, _ in self.calls if t - 60 < x <= t) >= self.rate:
            self.rate_refused.append(call)
            raise PlatformError(
                "this API key is limited to 60 requests a minute", 429, "rate_limit"
            )
        self.calls.append((t, call, what))

    def open_of(self, hotkey: str) -> list[str]:
        return [n for n, w in self.ws.items() if w.get("hotkey") == hotkey and not w["frozen"]]

    def grant(self, hotkey: str, **kw: Any) -> str:
        self._count("grant", hotkey)
        if self.grant_error is not None:
            raise self.grant_error
        if len(self.open_of(hotkey)) >= QUOTA:
            raise PlatformError(QUOTA_WORDS, 429, "miner_job_quota")
        seed = str(kw.get("seed") or "")
        if seed and seed not in self.ws:
            raise PlatformError("no such workspace", 404)
        return super().grant(hotkey, **kw)

    def close_grants(self, hotkey: str) -> dict[str, Any]:
        if self.sticky:
            self.closed.append(hotkey)
            return {"hotkey": hotkey, "frozen": []}
        return super().close_grants(hotkey)

    def freeze(self, ws: str) -> dict[str, Any]:
        self._count("freeze", ws)
        self.freezes.append(ws)
        if ws not in self.ws:
            raise PlatformError("no such workspace", 404)
        return super().freeze(ws)

    def fork(self, ws: str) -> str:
        self._count("fork", ws)
        return super().fork(ws)

    def run(self, ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
        self._count("run", ws)
        return super().run(ws, command, args, **kw)

    def export(self, ws: str) -> bytes:
        self._count("export", ws)
        return super().export(ws)


def grants_to(p: QuotaPlatform, hotkey: str) -> int:
    return sum(1 for _, call, hk in p.calls if call == "grant" and hk == hotkey)


class Bench:
    """A desk on a fake clock whose miners keep polling while time runs (``run``)."""

    def __init__(self, p: QuotaPlatform, clock: Clock, miners: list[str], **kw: Any) -> None:
        self.p, self.clock, self.miners = p, clock, miners
        self.lines: list[str] = []
        self.desk = Desk(
            p,
            Ledger(),
            registered=lambda: miners,
            clock=clock,
            rng=random.Random(5),  # noqa: S311 -- a test's seed
            excluded=[OWNER],
            log=self.lines.append,
            **kw,
        )
        for hk in miners:
            self.desk.agent_key(hk)
            self.desk.miners[hk].admitted = True
        self.poll()

    def poll(self) -> dict[str, dict[str, Any]]:
        return {hk: self.desk.work(hk) for hk in self.miners}

    def tick(self) -> None:
        self.poll()
        self.desk.tick()
        assert self.desk.wait_idle()

    def run(self, seconds: float, step: float = 5.0) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.tick()
            self.clock.t += step

    def holder(self, job: str) -> str:
        for hk, w in self.poll().items():
            if w["job"] == job:
                return hk
        return ""

    def stop(self) -> None:
        self.desk.stop()


class TestMinerQuota(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.p = QuotaPlatform(self.clock)
        self.b = Bench(self.p, self.clock, [A, B])

    def tearDown(self) -> None:
        self.b.stop()

    def test_a_miner_at_its_quota_is_held_off_and_the_job_goes_to_another(self) -> None:
        # A holds two open job workspaces another party granted (a close frees none of them)
        self.p.sticky = True
        self.p.grant(A, brief="elsewhere", name="x1")
        self.p.grant(A, brief="elsewhere", name="x2")
        self.p.calls.clear()
        self.b.desk.create({"job": "q1", "brief": "A wall hook.", "prefer": A})
        self.b.tick()  # A is refused; the job is queued again
        self.assertEqual(self.b.desk.status("q1")["state"], "queued")
        self.b.tick()  # the next tick: another miner
        self.assertEqual(self.b.holder("q1"), B)
        self.b.desk.accept(B, "q1")
        self.assertEqual(grants_to(self.p, A), 1)
        # a second job wants A while B is busy: 30 minutes go by without a tight loop
        self.b.desk.create({"job": "q2", "brief": "A shelf.", "prefer": A})
        self.b.run(30 * 60)
        tries = grants_to(self.p, A)
        self.assertLessEqual(tries, 1 + 30 * 60 / MINER_COOL_S + 1)  # once per cool-down
        self.assertGreaterEqual(tries, 2)  # but it is asked again
        refused = [ln for ln in self.b.lines if "miner_job_quota" in ln]
        self.assertTrue(refused)
        self.assertLessEqual(len(refused), tries)  # one line per refusal at most, throttled
        self.assertIn(A[:10], refused[0])
        self.assertIn("q1", refused[0])

    def test_a_miner_at_its_quota_with_no_live_work_here_has_its_lost_grants_swept(self) -> None:
        # two workspaces granted to A for work this validator no longer tracks (answers lost)
        lost = [self.p.grant(A, brief="A wall hook.", name=f"old{i}") for i in range(2)]
        self.b.desk.create({"job": "q3", "brief": "A wall hook.", "prefer": A})
        self.b.tick()  # refused at its quota -> swept (A held none of this validator's work)
        self.assertEqual(self.p.closed, [A])
        self.assertTrue(all(self.p.ws[w]["frozen"] for w in lost))
        self.b.tick()  # the sweep freed its places: A is offered the job again at once
        self.assertEqual(self.b.holder("q3"), A)
        self.assertTrue(any("2 open job workspace(s) frozen" in ln for ln in self.b.lines))

    def test_a_miner_with_live_work_here_is_never_swept(self) -> None:
        self.b.desk.create({"job": "q4", "brief": "A wall hook.", "prefer": A})
        self.b.tick()
        self.assertEqual(self.b.holder("q4"), A)
        self.p.grant(A, brief="elsewhere", name="x")  # now at its quota
        # a second grant to A (a practice job beside its work) is refused at its quota
        self.b.desk.create({"job": "q5", "brief": "A shelf."})
        job = self.b.desk.jobs["q5"]
        with self.b.desk._mut():
            job.state, job.granting = "assigning", A
        self.b.desk._grant_refused(
            job, self.b.desk.miners[A], PlatformError(QUOTA_WORDS, 429, "miner_job_quota")
        )
        self.assertTrue(self.b.desk.wait_idle())
        self.assertNotIn(A, self.b.desk._sweep)  # its live work is not cut
        self.assertEqual(self.p.closed, [])
        self.assertEqual(self.b.desk.status("q5")["state"], "queued")
        self.assertEqual(self.b.holder("q4"), A)


class TestPermanentRefusal(unittest.TestCase):
    def test_a_revision_whose_seed_is_gone_ends_after_a_few_tries_unscored(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock)
        b = Bench(p, clock, [A, B, C])
        try:
            b.desk.create({"job": "s1", "brief": "", "seed": "ag-swept-long-ago"})
            b.run(60 * 60)
            st = b.desk.status("s1")
            self.assertEqual(st["state"], "done")
            self.assertEqual(st["result"]["label"], "didn't finish")
            self.assertFalse(st["result"]["charge"])
            self.assertIn("404", st["result"]["why"])
            self.assertEqual(grants_to(p, A) + grants_to(p, B) + grants_to(p, C), 3)
            self.assertEqual(GRANT_PERMANENT_TRIES, 3)
            self.assertEqual(p.completed["s1"]["label"], "didn't finish")  # the platform told
            self.assertEqual([o for o in b.desk.ledger.outcomes if o.job == "s1"], [])
            self.assertTrue(any(ln.startswith("ALERT: job s1") for ln in b.lines))
        finally:
            b.stop()

    def test_a_transient_refusal_backs_off_and_never_ends_the_job(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock)
        b = Bench(p, clock, [A, B])
        try:
            p.grant_error = PlatformError("bad gateway", 502)
            b.desk.create({"job": "t1", "brief": "A wall hook."})
            b.run(10 * 60)
            n = sum(1 for _, call, _ in p.calls if call == "grant")
            self.assertLessEqual(n, 6)  # 0, 30, 90, 210, 450 s: not one a tick
            self.assertGreaterEqual(n, 4)
            self.assertEqual(b.desk.status("t1")["state"], "queued")
            lines = [ln for ln in b.lines if "grant to" in ln]
            self.assertEqual(len(lines), 1)  # the first, then throttled
            p.grant_error = None
            b.run(10 * 60)
            self.assertTrue(b.desk.status("t1")["attempts"])  # granted once the platform is back
        finally:
            b.stop()

    def test_the_validators_own_rate_pauses_every_grant(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock)
        b = Bench(p, clock, [A, B, C])
        try:
            p.grant_error = PlatformError("limited to 60 requests a minute", 429, "rate_limit")
            b.desk.create({"job": "r1", "brief": "A wall hook."})
            b.tick()
            self.assertEqual(sum(1 for _, call, _ in p.calls if call == "grant"), 1)
            p.grant_error = None
            b.desk.create({"job": "r2", "brief": "A shelf."})  # a job with no back-off of its own
            clock.t += 10
            b.tick()
            self.assertEqual(sum(1 for _, call, _ in p.calls if call == "grant"), 1)  # paused
            clock.t += 55
            b.tick()
            self.assertEqual(sum(1 for _, call, _ in p.calls if call == "grant"), 3)
        finally:
            b.stop()


class TestLogThrottle(unittest.TestCase):
    def test_one_line_per_kind_then_at_most_every_few_minutes_with_the_count(self) -> None:
        clock = Clock()
        b = Bench(QuotaPlatform(clock), clock, [A])
        try:
            for _ in range(50):
                b.desk._say("job:x:404", "refused x")
                clock.t += 1
            b.desk._say("job:y:404", "refused y")  # another kind: its own line
            clock.t += GRANT_LOG_EVERY_S
            b.desk._say("job:x:404", "refused x")
            self.assertEqual(
                b.lines, ["refused x", "refused y", "refused x [49 more since the last line]"]
            )
        finally:
            b.stop()


class TestGrantRate(unittest.TestCase):
    def test_grants_are_bounded_per_minute_all_jobs_together(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock)
        miners = [A, B, C, D]
        b = Bench(p, clock, miners, grant_per_min=2)
        try:
            for i in range(4):
                b.desk.create({"job": f"g{i}", "brief": "A wall hook."})
            b.tick()
            self.assertEqual(sum(1 for _, call, _ in p.calls if call == "grant"), 2)
            clock.t += 30
            b.tick()
            self.assertEqual(sum(1 for _, call, _ in p.calls if call == "grant"), 3)
            clock.t += 30
            b.tick()
            self.assertEqual({b.holder(f"g{i}") for i in range(4)}, set(miners))
        finally:
            b.stop()

    def test_the_judge_goes_through_while_grants_are_refused(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock, rate=60)  # the validator's key: 60 calls a minute
        miners = [A] + [f"5Miner{i:02d}" for i in range(12)]
        b = Bench(p, clock, miners, max_inflight=12)
        try:
            b.desk.create({"job": "w1", "brief": "A wall hook.", "prefer": A})
            b.tick()
            w = b.poll()[A]
            self.assertEqual(w["job"], "w1")
            b.desk.accept(A, "w1")
            # thirty revisions whose seeds are gone, and miners all at their quota elsewhere
            p.sticky = True
            for hk in miners[1:]:
                p.grant(hk, brief="elsewhere", name="x1")
                p.grant(hk, brief="elsewhere", name="x2")
            for i in range(30):
                b.desk.create({"job": f"gone{i}", "brief": "", "seed": f"ag-gone-{i}"})
            p.calls.clear()
            b.run(3 * 60)
            p.work(w["workspace"], GOOD, deliverable=True)
            b.desk.submit(A, "w1", w["workspace"], "d")
            st = wait_state(b.desk, "w1", ("done",))
            self.assertEqual(st["result"]["label"], "delivered")
            self.assertEqual(p.rate_refused, [])  # nothing of the validator's was rate-refused
        finally:
            b.stop()


class TestRestartMidGrant(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Rig(self.tmp.name, platform=QuotaPlatform())
        self.r.admit(A, B, C)
        self.r.poll(A, B, C)

    def tearDown(self) -> None:
        if self.r.desk is not None:
            self.r.desk.stop()
        self.tmp.cleanup()

    def _cut_off(self, job: str, *, granting: bool) -> str:
        """The process dies inside the grant of ``job`` to A, after the platform made it."""
        desk = self.r.desk
        assert desk is not None
        with desk._mut():
            desk.create({"job": job, "brief": "A shelf."})
            desk.jobs[job].state = "assigning"
            desk.jobs[job].granting = A if granting else ""  # older rows: the miner's job says
            desk.miners[A].job = job
        return self.r.p.grant(A, brief="A shelf.", name=job)

    def test_the_lost_grant_is_frozen_before_the_miner_works_again(self) -> None:
        for granting in (True, False):
            with self.subTest(granting=granting):
                job = f"cut-{granting}"
                orphan = self._cut_off(job, granting=granting)
                desk = self.r.restart()
                self.assertIn(A, desk._sweep)
                self.r.poll(A, B, C)
                self.r.tick()  # A gets nothing while its sweep is due; the sweep runs
                self.assertTrue(self.r.p.ws[orphan]["frozen"])
                self.assertEqual(self.r.p.closed[-1], A)
                self.assertNotIn(A, desk._sweep)
                holder, w = self.r.holder(job)
                self.assertNotEqual(holder, A)
                self.assertFalse(self.r.p.ws[w["workspace"]]["frozen"])
                desk.cancel(job)
                self.r.tick()

    def test_a_job_that_ends_freezes_its_attempts_workspaces(self) -> None:
        desk = self.r.desk
        assert desk is not None
        desk.create({"job": "e2", "brief": "A wall hook."})
        self.r.tick()
        _, w = self.r.holder("e2")
        with desk._mut():
            # an earlier attempt of the job, its workspace still open on the platform
            stale = self.r.p.grant(C, brief="A wall hook.", name="e2")
            job = desk.jobs["e2"]
            job.attempts.insert(
                0, Attempt(hotkey=C, workspace=stale, assigned_ms=0, accept_by_ms=0, kind="lost")
            )
        self.r.p.freezes.clear()
        desk.cancel("e2")
        desk.wait_idle()
        self.assertTrue(self.r.p.ws[stale]["frozen"])
        self.assertTrue(self.r.p.ws[w["workspace"]]["frozen"])
        self.assertEqual(sorted(set(self.r.p.freezes)), sorted({stale, w["workspace"]}))


if __name__ == "__main__":
    unittest.main()
