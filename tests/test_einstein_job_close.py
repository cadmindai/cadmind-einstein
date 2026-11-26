"""A sweep closes only the job's grants: after a restart mid-grant, at a quota refusal and when a
job ends, the validator asks the platform to freeze one job's open grants to the miner (made by
this operator, its live attempts kept) -- never another job's, never another validator's. Only a
platform that does not take a job-scoped close gets the whole-hotkey sweep, and only while the
miner holds no live work here. A grant whose answer was lost comes back ``existing`` and is used;
a seed the platform says is gone (``seed_not_found``) is permanent."""

from __future__ import annotations

import tempfile
import unittest
from typing import Any

from cadmind_einstein.desk import GRANT_PERMANENT_TRIES, Desk
from cadmind_einstein.platform import Granted, OperatorClient, PlatformError
from tests.test_einstein import A, B, C, Clock
from tests.test_einstein_durable import Rig
from tests.test_einstein_grant_refusals import QUOTA_WORDS, Bench, QuotaPlatform, grants_to


class JobClosePlatform(QuotaPlatform):
    """The platform's grants as of the job-scoped close: each workspace knows its job and which
    operator made it (``us``, or ``them``: another validator); a grant of a job this operator
    already holds open for the miner answers that one (``existing``); ``close_job_grants``
    freezes one job's open grants of ours to the hotkey but ``keep``. ``old``: a platform from
    before it -- ``"shape"`` ignores ``job`` (it closes every open job workspace of the hotkey and
    answers without ``job``), ``"400"`` refuses the field."""

    def __init__(self, clock: Clock | None = None, *, old: str = "") -> None:
        super().__init__(clock)
        self.old = old
        self.job_closes: list[tuple[str, str, list[str]]] = []
        self.lose_answer = 0  # the next grants are made, but their answer never arrives

    def grant(self, hotkey: str, **kw: Any) -> str:
        job = str(kw.get("name") or "")
        if not self.old:
            for n, w in self.ws.items():
                held = w.get("hotkey") == hotkey and w.get("job") == job and w.get("by") == "us"
                if held and not w["frozen"]:
                    self._count("grant", hotkey)
                    return Granted(n, existing=True)
        ws = super().grant(hotkey, **kw)
        self.ws[ws].update(job=job, by="us")
        if self.lose_answer:
            self.lose_answer -= 1
            raise PlatformError("platform unreachable: timed out")
        return Granted(ws)

    def theirs(self, hotkey: str, job: str) -> str:
        """A grant another validator (another operator key) made to ``hotkey``."""
        return self._new(hotkey=hotkey, files={}, job=job, by="them")

    def close_job_grants(self, hotkey: str, job: str, keep: Any = ()) -> dict[str, Any]:
        self.job_closes.append((hotkey, job, list(keep)))
        if self.old == "400":
            raise PlatformError("unknown field: job", 400, "bad_request")
        if self.old == "shape":
            return self.close_grants(hotkey)
        frozen = [
            n
            for n, w in self.ws.items()
            if w.get("hotkey") == hotkey
            and w.get("job") == job
            and w.get("by") == "us"
            and not w["frozen"]
            and n not in keep
        ]
        for n in frozen:
            self.ws[n]["frozen"] = True
        return {"hotkey": hotkey, "job": job, "frozen": frozen}


class _Restart(unittest.TestCase):
    old = ""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.p = JobClosePlatform(old=self.old)
        self.r = Rig(self.tmp.name, platform=self.p)
        self.r.admit(A, B, C)
        self.r.poll(A, B, C)

    def tearDown(self) -> None:
        if self.r.desk is not None:
            self.r.desk.stop()
        self.tmp.cleanup()

    def cut_off(self, job: str) -> str:
        """The process dies inside the grant of ``job`` to A, after the platform made it."""
        desk = self.r.desk
        assert desk is not None
        with desk._mut():
            desk.create({"job": job, "brief": "A shelf."})
            desk.jobs[job].state, desk.jobs[job].granting = "assigning", A
            desk.miners[A].job = job
        return self.p.grant(A, brief="A shelf.", name=job)


class TestRestartOnTheNewPlatform(_Restart):
    def test_only_the_cut_off_jobs_grants_from_this_validator_are_frozen(self) -> None:
        orphan = self.cut_off("cut-1")
        theirs_same_job = self.p.theirs(A, "cut-1")  # made by another operator key
        theirs = self.p.theirs(A, "their-job-7")  # another validator's work for A
        mine_elsewhere = self.p.grant(B, brief="x", name="cut-1")  # the job's grant to B
        desk = self.r.restart()
        self.assertEqual(desk._sweep[A]["jobs"], ["cut-1"])
        self.r.poll(A, B, C)
        self.r.tick()
        self.assertEqual(self.p.job_closes[0], (A, "cut-1", []))
        self.assertTrue(self.p.ws[orphan]["frozen"])
        for ws in (theirs_same_job, theirs, mine_elsewhere):
            self.assertFalse(self.p.ws[ws]["frozen"], ws)
        self.assertEqual(self.p.closed, [])  # never the whole-hotkey close
        self.assertNotIn(A, desk._sweep)
        self.assertIs(desk._job_close, True)


class TestRestartOnAnOldPlatform(_Restart):
    old = "shape"

    def test_an_old_platform_gets_the_whole_hotkey_sweep_and_it_is_said_once(self) -> None:
        orphan = self.cut_off("cut-2")
        theirs = self.p.theirs(A, "their-job-8")
        desk = self.r.restart()
        lines: list[str] = []
        desk.log = lines.append  # type: ignore[method-assign]
        self.r.poll(A, B, C)
        self.r.tick()
        self.assertTrue(self.p.ws[orphan]["frozen"])
        self.assertTrue(self.p.ws[theirs]["frozen"])  # what an old platform's close does
        self.assertEqual(self.p.closed, [A])
        self.assertIs(desk._job_close, False)
        said = [ln for ln in lines if "does not take a job-scoped close" in ln]
        self.assertEqual(len(said), 1)
        # known now: the next sweep goes straight to the whole-hotkey close, said no more
        desk.cancel("cut-2")  # (B or C took it: live work there would hold the sweep off)
        self.assertTrue(desk.wait_idle())
        self.p.closed.clear()
        with desk._mut():
            desk._queue_sweep(B, "a grant cut off by a restart", "cut-3")
        desk._sweep_grants()
        self.assertEqual(self.p.closed, [B])
        self.assertEqual([j for _, j, _ in self.p.job_closes], ["cut-2"])
        self.assertEqual(len([ln for ln in lines if "job-scoped" in ln]), 1)


class TestRestartOnAPlatformThatRefusesTheField(_Restart):
    old = "400"

    def test_a_400_falls_back_to_the_whole_sweep_when_no_work_is_live(self) -> None:
        orphan = self.cut_off("cut-4")
        desk = self.r.restart()
        self.r.poll(A, B, C)
        self.r.tick()
        self.assertEqual([j for _, j, _ in self.p.job_closes], ["cut-4"])
        self.assertTrue(self.p.ws[orphan]["frozen"])
        self.assertEqual(self.p.closed, [A])
        self.assertIs(desk._job_close, False)

    def test_with_live_work_here_the_whole_sweep_waits(self) -> None:
        desk = self.r.desk
        assert desk is not None
        desk._job_close = False  # known: no job-scoped close here
        desk.create({"job": "live-1", "brief": "A wall hook.", "prefer": A})
        self.r.tick()
        holder, w = self.r.holder("live-1")
        with desk._mut():
            desk._queue_sweep(holder, "a grant cut off by a restart", "cut-5")
        desk._sweep_grants()
        self.assertEqual(self.p.closed, [])
        self.assertFalse(self.p.ws[w["workspace"]]["frozen"])
        self.assertIn(holder, desk._sweep)  # asked again once it holds no live work


class TestQuotaAndJobEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.p = JobClosePlatform(self.clock)
        self.b = Bench(self.p, self.clock, [A, B])
        self.desk = self.b.desk

    def tearDown(self) -> None:
        self.b.stop()

    def test_at_its_quota_with_live_work_only_the_jobs_it_holds_none_on_are_closed(self) -> None:
        self.desk._job_close = True  # the platform takes job-scoped closes (learned)
        self.desk.create({"job": "w1", "brief": "A wall hook.", "prefer": A})
        self.b.tick()
        live = self.b.poll()[A]
        self.assertEqual(live["job"], "w1")
        orphan = self.p.grant(A, brief="lost", name="lost-1")  # its answer never came
        theirs = self.p.theirs(A, "their-job-9")
        with self.desk._mut():
            self.desk._note_maybe(A, "lost-1")
        self.desk.create({"job": "q2", "brief": "A shelf."})
        job = self.desk.jobs["q2"]
        with self.desk._mut():
            job.state, job.granting = "assigning", A
        self.desk._grant_refused(
            job, self.desk.miners[A], PlatformError(QUOTA_WORDS, 429, "miner_job_quota")
        )
        self.assertTrue(self.desk.wait_idle())
        closed_jobs = [j for hk, j, _ in self.p.job_closes if hk == A]
        self.assertIn("lost-1", closed_jobs)
        self.assertNotIn("w1", closed_jobs)  # its live work here
        self.assertTrue(self.p.ws[orphan]["frozen"])
        self.assertFalse(self.p.ws[live["workspace"]]["frozen"])
        self.assertFalse(self.p.ws[theirs]["frozen"])
        self.assertEqual(self.p.closed, [])
        self.assertNotIn(A, self.desk._sweep)
        self.assertNotIn(A, self.desk._maybe)
        self.assertNotIn(A, self.desk._miner_cool)  # the close freed a place: offered again

    def test_a_job_that_ends_closes_its_grants_per_miner(self) -> None:
        self.desk.create({"job": "e1", "brief": "A wall hook.", "prefer": A})
        self.b.tick()
        w = self.b.poll()[A]
        lost = self.p.grant(B, brief="A wall hook.", name="e1")  # a grant to B, answer lost
        other = self.p.grant(A, brief="x", name="e9")  # another job's grant to A
        theirs = self.p.theirs(A, "e1")
        with self.desk._mut():
            self.desk._note_maybe(B, "e1")
        self.p.freezes.clear()
        self.desk.cancel("e1")
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(sorted(self.p.job_closes), sorted([(A, "e1", []), (B, "e1", [])]))
        self.assertTrue(self.p.ws[w["workspace"]]["frozen"])
        self.assertTrue(self.p.ws[lost]["frozen"])
        self.assertFalse(self.p.ws[other]["frozen"])
        self.assertFalse(self.p.ws[theirs]["frozen"])
        self.assertNotIn(B, self.desk._maybe)

    def test_a_grant_whose_answer_was_lost_comes_back_existing_and_is_used(self) -> None:
        clock = Clock()
        p = JobClosePlatform(clock)
        b = Bench(p, clock, [A])
        try:
            p.lose_answer = 1
            b.desk.create({"job": "x1", "brief": "A wall hook."})
            b.tick()  # made on the platform, its answer lost: queued again, backed off
            self.assertEqual(b.desk.status("x1")["state"], "queued")
            self.assertEqual(b.desk._maybe.get(A), ["x1"])
            made = p.open_of(A)
            self.assertEqual(len(made), 1)
            b.run(60)
            self.assertEqual(b.poll()[A]["job"], "x1")
            self.assertEqual(b.poll()[A]["workspace"], made[0])  # that workspace, no second one
            self.assertEqual(p.open_of(A), made)
            self.assertEqual(grants_to(p, A), 2)
            self.assertTrue(any("open workspace for it again" in ln for ln in b.lines))
            self.assertNotIn(A, b.desk._maybe)
        finally:
            b.stop()

    def test_an_earlier_attempts_workspace_answered_again_is_not_worked(self) -> None:
        self.desk.create({"job": "r1", "brief": "A wall hook.", "prefer": A})
        self.b.tick()
        first = self.b.poll()[A]["workspace"]
        job = self.desk.jobs["r1"]
        with self.desk._mut():  # its attempt ended (lost), its workspace's freeze never made
            job.attempts[-1].kind = "lost"
            job.state, job.granting = "assigning", A
        self.desk._assign(job, self.desk.miners[A])
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(len(job.attempts), 1)
        self.assertEqual(job.state, "queued")
        self.assertTrue(self.p.ws[first]["frozen"])


class TestSeedNotFound(unittest.TestCase):
    def test_the_code_is_permanent_on_any_status(self) -> None:
        gone = PlatformError("this grant's seed workspace is gone", 404, "seed_not_found")
        self.assertEqual(Desk._grant_refusal(gone), "permanent")
        bare = PlatformError("no such workspace", 404)  # an older platform
        self.assertEqual(Desk._grant_refusal(bare), "permanent")

    def test_a_revision_whose_seed_is_gone_ends_unscored(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock)
        p.grant_error = PlatformError("this grant's seed workspace is gone", 404, "seed_not_found")
        b = Bench(p, clock, [A, B, C])
        try:
            b.desk.create({"job": "s9", "brief": "", "seed": "ag-gone-9"})
            b.run(60 * 60)
            st = b.desk.status("s9")
            self.assertEqual((st["state"], st["result"]["label"]), ("done", "didn't finish"))
            self.assertIn("seed_not_found", st["result"]["why"])
            self.assertEqual(sum(1 for _, c, _ in p.calls if c == "grant"), GRANT_PERMANENT_TRIES)
        finally:
            b.stop()


class TestClient(unittest.TestCase):
    def client(self, answer: dict[str, Any]) -> tuple[OperatorClient, list[tuple[Any, ...]]]:
        c = OperatorClient("http://127.0.0.1:9", "op", keys_key="keys")
        seen: list[tuple[Any, ...]] = []

        def call(method: str, path: str, body: Any = None, **kw: Any) -> dict[str, Any]:
            seen.append((method, path, dict(body or {}), kw.get("role")))
            return answer

        c._call = call  # type: ignore[method-assign]
        return c, seen

    def test_the_job_scoped_close_names_the_job_and_what_it_keeps(self) -> None:
        c, seen = self.client({"hotkey": A, "job": "j1", "frozen": ["ws-1"]})
        out = c.close_job_grants(A, "j1", keep=("ws-2",))
        self.assertEqual(out["frozen"], ["ws-1"])
        self.assertEqual(
            seen,
            [
                (
                    "POST",
                    "/api/agent/subnet/keys/disable",
                    {"hotkey": A, "job": "j1", "keep": ["ws-2"]},
                    "keys",
                )
            ],
        )

    def test_a_grant_answered_existing_says_so(self) -> None:
        c, _ = self.client({"workspace": "ws-3", "existing": True, "reference": "ws-4"})
        ws = c.grant(A, brief="b", name="j2")
        self.assertEqual((ws, ws.existing), ("ws-3", True))
        ws, ref = c.grant_continuation(A, seed="ws-0", base="ws-5", name="j3")
        self.assertEqual((ws, ws.existing, ref), ("ws-3", True, "ws-4"))
        c, seen = self.client({"workspace": "ws-6", "reference": "ws-7", "improvement": True})
        ws, ref = c.grant_improvement(A, "imp-0123456789ab")
        self.assertEqual((ws, ws.existing, ref), ("ws-6", False, "ws-7"))
        self.assertEqual(
            seen[-1][:3],
            (
                "POST",
                "/api/agent/subnet/workspaces",
                {
                    "hotkey": A,
                    "name": "imp-0123456789ab",
                    "job": "imp-0123456789ab",
                    "improvement": True,
                },
            ),
        )

    def test_the_improvement_routes(self) -> None:
        c, seen = self.client({"try_id": "imp-0123456789ab", "ready": True})
        c.improvement_next(A)
        c.improvement_reject("imp-0123456789ab", A)
        c.improvement_bundle("imp-0123456789ab", "ws-1")
        c.improvement_result("imp-0123456789ab", {"hotkey": A, "outcome": "failed"})
        c.review_bundle("j1", "compare")
        base = "/api/engine/einstein/improvement"
        self.assertEqual(
            [(m, path, body, role) for m, path, body, role in seen],
            [
                ("POST", f"{base}/next", {"hotkey": A}, "queue"),
                ("POST", f"{base}/imp-0123456789ab/reject", {"hotkey": A}, "queue"),
                ("POST", f"{base}/imp-0123456789ab/bundle", {"workspace": "ws-1"}, "queue"),
                (
                    "POST",
                    f"{base}/imp-0123456789ab/result",
                    {"hotkey": A, "outcome": "failed"},
                    "queue",
                ),
                (
                    "POST",
                    "/api/engine/einstein/jobs/j1/review_bundle",
                    {"audience": "compare"},
                    "queue",
                ),
            ],
        )
        for gone in ("practice_brief", "grant_practice", "practice_bundle", "practice_result"):
            self.assertFalse(hasattr(c, gone), gone)


if __name__ == "__main__":
    unittest.main()
