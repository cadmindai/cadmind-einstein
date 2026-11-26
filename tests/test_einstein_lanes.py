"""The lanes switch and the router (DESIGN.md "Open lanes"): with the lanes closed the router is
the one of before; open, client jobs go to the production lane by rating, one in five to the
longest wait, a priority job to the best rating; the entry test is retired; and opening the
lanes turns the launch rule off."""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import bittensor.http_auth as btauth
from bittensor.sp_core import Keypair

from cadmind_einstein import daemon, onboarding
from cadmind_einstein.desk import (
    FALLBACK_ALERT_S,
    NO_PRODUCTION_LOG_S,
    PRODUCTION_SEEN_S,
    Desk,
    DeskError,
    Job,
)
from cadmind_einstein.fake_miner import signed
from cadmind_einstein.scoring import HOUR_MS, THRESHOLD, JobOutcome, Ledger
from cadmind_einstein.service import serve
from cadmind_einstein.store import Store
from tests.test_einstein import VALIDATOR, Clock, FakePlatform

KEYS = {name: Keypair.create_from_uri(f"//{name}") for name in ("Alice", "Bob", "Charlie", "Dave")}
A, B, C, D = (k.ss58_address for k in KEYS.values())
TOKEN = "t-lanes"  # noqa: S105 -- a test's token


class Lanes(unittest.TestCase):
    """A desk with four keyed miners and a ledger to rate them in."""

    key_approval = "auto"

    def setUp(self) -> None:
        self.p = FakePlatform()
        self.clock = Clock()
        self.ledger = Ledger()
        self.reg = [A, B, C, D]
        self.said: list[str] = []
        self.desk = self.make()
        for hk in self.reg:
            self.key(hk)

    def make(self, store: Store | None = None) -> Desk:
        return Desk(
            self.p,
            self.ledger,
            registered=lambda: self.reg,
            clock=self.clock,
            rng=random.Random(7),  # noqa: S311 -- a test's seed
            log=self.said.append,
            store=store,
            key_approval=self.key_approval,
        )

    def key(self, hk: str) -> None:
        self.desk.agent_key(hk)

    def tearDown(self) -> None:
        self.desk.stop()

    def now(self) -> int:
        return self.desk.now_ms()

    def rate(self, hk: str, delivered: int, failed: int = 0) -> None:
        """``delivered`` silent deliveries (0.5 each) and ``failed`` attempts in the window."""
        n = len(self.ledger.outcomes)
        for i in range(delivered + failed):
            self.ledger.add(
                JobOutcome(
                    job=f"h-{n + i}",
                    hotkey=hk,
                    ended_ms=self.now() - HOUR_MS,
                    deliverable=i < delivered,
                    kind="pass" if i < delivered else "gate",
                )
            )

    def poll(self, *hks: str) -> dict[str, dict[str, Any]]:
        return {hk: self.desk.work(hk) for hk in hks or self.reg}

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def enqueue(self, job: str, **more: Any) -> None:
        self.p.queue.append({"job": job, "brief": "A wall hook for my coat.", **more})
        self.tick()

    def holder(self, job: str) -> str:
        got = [hk for hk, w in self.poll().items() if w["job"] == job]
        self.assertEqual(len(got), 1, f"{job}: {got}")
        return got[0]

    def cancelled(self, job: str) -> str:
        """The job is cancelled by its client: its miner is free again after the cool-off, its
        place in the line kept. Answers who held it."""
        hk = self.holder(job)
        self.desk.cancel(job)
        self.clock.t += 50
        return hk


class TestClosed(Lanes):
    def test_the_default_is_closed_and_the_router_is_the_one_of_before(self) -> None:
        self.assertFalse(self.desk.lanes_open)
        self.rate(A, 10)  # a production rating changes nothing while the lanes are closed
        self.desk.miners[B].admitted = True
        self.poll()
        self.enqueue("j1")
        self.assertEqual(self.holder("j1"), B)  # the admitted miner, whatever the ratings
        self.assertEqual(self.desk.trial_count, 0)
        self.assertEqual(self.desk.weighted(), [B])

    def test_work_names_the_lane_the_rating_and_the_threshold_all_the_same(self) -> None:
        self.rate(A, 9)
        got = self.poll(A, B)
        self.assertEqual(
            (got[A]["lane"], got[A]["rating"], got[A]["threshold"]),
            ("production", 0.45, THRESHOLD),
        )
        self.assertEqual(
            (got[B]["lane"], got[B]["rating"], got[B]["job"]), ("improvement", 0, None)
        )

    def test_the_entry_test_is_live(self) -> None:
        got = self.desk.admission(A)
        self.assertTrue(got["job"].startswith("admission-"))
        self.assertNotIn("retired", got)

    def test_a_claim_stores_its_priority(self) -> None:
        self.desk.miners[A].admitted = True
        self.desk.miners[B].admitted = True
        self.poll()
        self.enqueue("j1", priority=True)
        self.assertTrue(self.desk.jobs["j1"].priority)
        self.enqueue("j2")
        self.assertFalse(self.desk.jobs["j2"].priority)


class TestOpen(Lanes):
    def setUp(self) -> None:
        super().setUp()
        self.desk.set_lanes_open(True)

    def test_a_client_job_goes_to_the_production_lane_only(self) -> None:
        self.rate(A, 8)  # 0.4: on the threshold, production
        self.rate(B, 7)  # 0.35: improvement
        self.desk.miners[B].admitted = True  # an old admission is no way in
        self.poll()
        self.enqueue("j1")
        self.assertEqual(self.holder("j1"), A)
        # the only production miner is busy: the next job stays in the platform's queue
        self.p.queue.append({"job": "j2", "brief": "A second hook."})
        self.tick()
        self.assertNotIn("j2", self.desk.jobs)
        self.assertEqual(self.desk.capacity()["active_miners"], 1)

    def test_a_job_held_here_waits_and_says_so_at_most_every_ten_minutes(self) -> None:
        self.rate(A, 10)
        self.poll()
        self.enqueue("j1")
        self.desk.accept(A, "j1")
        self.desk.jobs["j2"] = Job(job="j2", brief="b", created_ms=self.now(), queued_ms=self.now())
        for _ in range(3):
            self.poll()
            self.tick()
        self.assertEqual(self.desk.jobs["j2"].state, "queued")
        lines = [x for x in self.said if "no production miner free" in x]
        self.assertEqual(len(lines), 1)
        self.clock.t += NO_PRODUCTION_LOG_S + 1
        self.poll()
        self.tick()
        self.assertEqual(len([x for x in self.said if "no production miner free" in x]), 2)

    def test_the_best_rating_first_and_equal_ratings_to_the_longest_wait(self) -> None:
        self.rate(A, 10)  # 0.5
        self.rate(B, 12, 2)  # 6 / 14
        self.rate(C, 10)  # 0.5
        self.desk.miners[A].free_ms = 500
        self.desk.miners[C].free_ms = 100  # waited longer than A
        self.poll()
        self.enqueue("j1")
        self.assertEqual(self.holder("j1"), C)
        self.enqueue("j2")
        self.assertEqual(self.holder("j2"), A)
        self.enqueue("j3")
        self.assertEqual(self.holder("j3"), B)

    def test_one_job_in_five_goes_to_the_longest_wait_whatever_the_rating(self) -> None:
        self.rate(A, 10, 0)  # 0.5: the best
        self.rate(B, 8, 0)  # 0.4: just over the threshold
        self.desk.miners[A].free_ms = 100
        self.desk.miners[B].free_ms = 50  # never served: it has waited longest
        self.poll(A, B)
        got = []
        for i in range(5):
            self.enqueue(f"j{i}")
            got.append(self.cancelled(f"j{i}"))
            self.poll(A, B)
        self.assertEqual(got, [A, A, A, A, B])  # the fifth: the longest wait
        self.assertEqual(self.desk.trial_count, 5)

    def test_the_counter_is_kept_across_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite"
            self.desk.stop()
            self.ledger = Ledger(store=Store(path))
            self.desk = self.make(self.ledger.store)
            for hk in self.reg:
                self.key(hk)
            self.desk.set_lanes_open(True)
            self.rate(A, 10)
            self.poll()
            self.enqueue("j1")
            self.assertEqual(self.desk.trial_count, 1)
            self.desk.stop()
            again = Desk(
                self.p, Ledger(store=Store(path)), registered=lambda: self.reg, store=Store(path)
            )
            try:
                self.assertEqual(
                    (again.trial_count, again.lanes_open, again.lanes_saved), (1, True, True)
                )
            finally:
                again.stop()

    def test_a_priority_job_goes_to_the_best_rating_and_leaves_the_counter_alone(self) -> None:
        self.rate(A, 10)
        self.rate(B, 8)
        self.desk.miners[A].free_ms = 100
        self.desk.trial_count = 4  # the next counted job is the fifth: the longest wait (B)
        self.poll(A, B)
        self.enqueue("p1", priority=True)
        self.assertEqual(self.holder("p1"), A)  # the best rating, outside the one-in-five rule
        self.assertEqual(self.desk.trial_count, 4)  # neither read nor advanced
        self.enqueue("j1")
        self.assertEqual(self.holder("j1"), B)
        self.assertEqual(self.desk.trial_count, 5)

    def test_a_calibration_job_goes_to_the_longest_wait_and_is_not_counted(self) -> None:
        self.rate(A, 10)
        self.rate(B, 8)
        self.desk.miners[A].free_ms = 100
        self.poll(A, B)
        self.enqueue("c1", calibration={"set": "s", "id": "1", "generator": "g"})
        self.assertEqual(self.holder("c1"), B)
        self.assertEqual(self.desk.trial_count, 0)

    def test_prefer_is_honoured_only_for_a_production_miner(self) -> None:
        self.rate(A, 10)
        self.rate(B, 8)
        self.rate(C, 3)  # improvement lane
        self.desk.miners[A].free_ms = 100
        self.poll()
        self.enqueue("j1", prefer=C)
        self.assertEqual(self.holder("j1"), A)  # a client job only goes to the production lane
        self.enqueue("j2", prefer=B)
        self.assertEqual(self.holder("j2"), B)

    def test_no_production_miner_at_all_goes_to_the_highest_rated_free_approved_miner(
        self,
    ) -> None:
        self.rate(A, 3)  # 0.15
        self.rate(B, 6)  # 0.30: the highest, still under the threshold
        self.rate(C, 6)
        self.desk.miners[B].free_ms = 500
        self.desk.miners[C].free_ms = 100  # equal rating: the longer wait
        self.poll()
        self.enqueue("j1")
        self.assertEqual(self.holder("j1"), C)
        self.enqueue("j2")
        self.assertEqual(self.holder("j2"), B)
        alerts = [x for x in self.said if x.startswith("ALERT") and "production rating" in x]
        self.assertEqual(len(alerts), 1)  # at most hourly
        self.assertEqual(len([x for x in self.said if "no production miner at all" in x]), 2)
        self.clock.t += FALLBACK_ALERT_S + 1
        self.poll()
        self.enqueue("j3")
        self.assertEqual(self.holder("j3"), A)
        self.assertEqual(
            len([x for x in self.said if x.startswith("ALERT") and "production rating" in x]), 2
        )

    def test_a_production_miner_that_exists_but_is_busy_makes_the_job_wait(self) -> None:
        self.rate(A, 10)
        self.rate(B, 6)
        self.poll()
        self.enqueue("j1")
        self.assertEqual(self.holder("j1"), A)
        self.desk.jobs["j2"] = Job(job="j2", brief="b", created_ms=self.now(), queued_ms=self.now())
        self.poll()
        self.tick()
        self.assertEqual(self.desk.jobs["j2"].state, "queued")  # never to B while A exists
        self.assertFalse([x for x in self.said if "no production miner at all" in x])

    def test_an_offline_production_miner_is_not_waited_for(self) -> None:
        self.rate(A, 10)  # the only production miner
        self.rate(B, 6)
        self.rate(C, 3)
        # A has not polled for five minutes: no candidate, but seen within ten -- the job waits
        self.clock.t += 300
        self.poll(B, C)
        self.desk.jobs["j1"] = Job(job="j1", brief="b", created_ms=self.now(), queued_ms=self.now())
        self.tick()
        self.assertEqual(self.desk.jobs["j1"].state, "queued")
        self.assertEqual(self.desk.capacity()["active_miners"], 0)
        # past ten minutes without a poll it no longer counts: the fallback takes the job
        self.clock.t += PRODUCTION_SEEN_S - 300 + 1
        self.poll(B, C)
        self.tick()
        self.assertEqual(self.holder_of("j1"), B)  # the highest-rated free approved miner
        self.assertEqual(len([x for x in self.said if "no production miner at all" in x]), 1)
        self.assertTrue(any(x.startswith("ALERT") and "production rating" in x for x in self.said))
        # it polls again: it counts again, and the next job waits for it or goes to it
        self.poll(A)
        self.enqueue("j2")
        self.assertEqual(self.holder_of("j2"), A)

    def holder_of(self, job: str) -> str:
        return self.desk.jobs[job].attempts[-1].hotkey

    def test_an_online_but_busy_production_miner_makes_the_job_wait(self) -> None:
        self.rate(A, 10)
        self.rate(B, 6)
        self.poll()
        self.enqueue("j1")
        self.desk.accept(A, "j1")
        self.desk.jobs["j2"] = Job(job="j2", brief="b", created_ms=self.now(), queued_ms=self.now())
        for _ in range(4):  # A keeps polling while it works, for well over ten minutes
            self.clock.t += PRODUCTION_SEEN_S / 2 - 50
            self.poll()
            self.tick()
            self.assertEqual(self.desk.jobs["j2"].state, "queued")
        self.assertFalse([x for x in self.said if "no production miner at all" in x])

    def test_an_offline_or_blocked_miner_is_no_candidate(self) -> None:
        self.rate(A, 10)
        self.rate(B, 9)
        self.clock.t += 120
        self.poll(B)  # A has not polled for two minutes: not alive
        self.enqueue("j1")
        self.assertEqual(self.holder("j1"), B)

    def test_the_entry_test_is_retired(self) -> None:
        self.rate(A, 9)
        self.assertEqual(
            self.desk.admission(A),
            {"admitted": True, "retired": True, "lane": "production", "rating": 0.45},
        )
        self.assertEqual(
            self.desk.admission(B),
            {"admitted": True, "retired": True, "lane": "improvement", "rating": 0.0},
        )
        self.assertFalse([j for j in self.desk.jobs.values() if j.kind == "admission"])
        self.assertFalse(self.desk.miners[A].admitted)  # nothing is admitted: the rating decides

    def test_capacity_counts_the_production_lane(self) -> None:
        self.rate(A, 10)
        self.rate(B, 9)
        self.poll()
        self.assertEqual(
            (self.desk.capacity()["active_miners"], self.desk.capacity()["free_miners"]), (2, 2)
        )
        self.enqueue("j1")
        self.assertEqual(self.desk.capacity()["free_miners"], 1)

    def test_a_job_every_production_miner_tried_ends(self) -> None:
        self.rate(A, 10)
        self.rate(C, 2)
        self.poll()
        self.enqueue("j1")
        self.desk.reject(A, "j1", "not-capable", "cannot")
        self.poll()
        self.tick()
        # C is in the improvement lane: nobody that could take the job is left
        self.assertEqual(self.desk.jobs["j1"].result["label"], "declined")


class TestTheSwitch(Lanes):
    key_approval = "manual"

    def key(self, hk: str) -> None:
        self.desk.agent_key(hk)
        self.desk.approve(hk)
        self.desk.agent_key(hk)

    def test_opening_the_lanes_turns_the_launch_rule_off_in_the_same_act(self) -> None:
        self.desk.set_launch_equal(True)
        self.assertEqual(
            self.desk.set_lanes_open(True),
            {"lanes_open": True, "was": False, "launch_equal": False},
        )
        self.assertTrue(any("launch-equal turned off" in x for x in self.said))
        with self.assertRaises(DeskError) as e:
            self.desk.set_launch_equal(True)
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.desk.set_launch_equal(False), {"launch_equal": False, "was": False})

    def test_the_switches_are_saved(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite"
            desk = Desk(self.p, Ledger(), registered=lambda: self.reg, store=Store(path))
            self.assertEqual((desk.lanes_open, desk.lanes_saved), (False, False))
            desk.set_launch_equal(True)
            desk.set_lanes_open(True, by="a test")
            desk.stop()
            desk = Desk(self.p, Ledger(), registered=lambda: self.reg, store=Store(path))
            self.assertEqual(
                (desk.lanes_open, desk.lanes_saved, desk.launch_equal), (True, True, False)
            )
            self.assertEqual(daemon.apply_lanes_flag(desk, None), "saved")
            self.assertEqual(daemon.apply_lanes_flag(desk, "off"), "flag")
            self.assertFalse(desk.lanes_open)
            desk.stop()
            desk = Desk(self.p, Ledger(), registered=lambda: self.reg, store=Store(path))
            self.assertFalse(desk.lanes_open)
            desk.stop()

    def test_the_daemon_takes_the_flag_and_refuses_both_on(self) -> None:
        a = daemon.parser().parse_args(self.argv("--lanes-open", "on"))
        self.assertEqual((a.lanes_open, a.launch_equal), ("on", None))
        self.assertIsNone(daemon.parser().parse_args(self.argv()).lanes_open)
        self.assertEqual(daemon.apply_lanes_flag(self.desk, None), "default")
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            daemon.main(self.argv("--lanes-open", "on", "--launch-equal", "on"))
        self.assertIn("--lanes-open on", str(e.exception))
        # a deprecated flag of the retired practice jobs is still taken, and ignored
        daemon.parser().parse_args(self.argv("--practice-per-day", "3"))

    @staticmethod
    def argv(*more: str) -> list[str]:
        need = {a.dest: a.option_strings[0] for a in daemon.parser()._actions if a.required}
        out: list[str] = []
        for flag in need.values():
            out += [flag, "x"]
        return [*out, *more]

    def test_a_job_of_before_the_lanes_is_stored_as_it_always_was(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "state.sqlite")
            desk = Desk(self.p, Ledger(), registered=lambda: self.reg, store=store)
            desk.create({"job": "j1", "brief": "A hook."})
            desk.create({"job": "j2", "brief": "A hook.", "priority": True})
            rows = store.items("job")
            self.assertFalse({"priority", "improvement", "practice"} & set(rows["j1"]))
            self.assertIs(rows["j2"]["priority"], True)
            desk.stop()
            # a row an older release stored, a practice job among them, loads
            old = {**rows["j1"], "job": "practice-0a1b2c", "kind": "practice"}
            store.put("job", "practice-0a1b2c", {**old, "practice": {"title": "t"}})
            again = Store(Path(d) / "state.sqlite")
            desk = Desk(self.p, Ledger(), registered=lambda: self.reg, store=again)
            self.assertEqual((desk.jobs["j1"].priority, desk.jobs["j2"].priority), (False, True))
            self.assertEqual(desk.jobs["practice-0a1b2c"].state, "done")
            desk.stop()

    def test_eligible_is_approved_keyed_and_not_blocked(self) -> None:
        self.desk.revoke(D, "a test")
        self.desk.miners[C].blocked_until_ms = self.now() + 1000
        self.assertEqual(sorted(self.desk.eligible()), sorted([A, B]))
        self.desk.set_lanes_open(True)
        self.assertEqual(sorted(self.desk.weighted()), sorted([A, B]))


class TestOverHttp(unittest.TestCase):
    def setUp(self) -> None:
        self.p = FakePlatform()
        self.desk = Desk(self.p, Ledger(), registered=lambda: [A, B])
        self.server = serve(
            self.desk,
            "127.0.0.1",
            0,
            token=TOKEN,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
            signer=VALIDATOR,
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tmp.name) / "token"
        self.token_file.write_text(TOKEN + "\n")
        os.chmod(self.token_file, 0o600)  # noqa: PTH101

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.desk.stop()
        self.tmp.cleanup()

    def call(self, method: str, path: str, body: Any = None, token: str = TOKEN) -> tuple[int, Any]:
        req = urllib.request.Request(  # noqa: S310 -- a local test server
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def op(self, *args: str) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = onboarding.main(
                ["--validator", self.base, "--token-file", str(self.token_file), *args]
            )
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_the_operator_route(self) -> None:
        self.assertEqual(
            self.call("GET", "/v1/operator/lanes_open"),
            (200, {"lanes_open": False, "launch_equal": False}),
        )
        self.assertEqual(self.call("POST", "/v1/operator/lanes_open", {"on": "yes"})[0], 400)
        self.assertEqual(
            self.call("POST", "/v1/operator/lanes_open", {"on": True}, "wrong")[0], 401
        )
        self.assertEqual(self.call("POST", "/v1/operator/launch_equal", {"on": True})[0], 200)
        self.assertEqual(
            self.call("POST", "/v1/operator/lanes_open", {"on": True}),
            (200, {"lanes_open": True, "was": False, "launch_equal": False}),
        )
        code, out = self.call("POST", "/v1/operator/launch_equal", {"on": True})
        self.assertEqual((code, out["code"]), (409, "lanes_open"))
        self.assertEqual(self.call("GET", "/v1/scores")[1]["lanes_open"], True)

    def test_the_operator_command(self) -> None:
        self.assertEqual(self.op("lanes-open", "status")[1]["lanes_open"], False)
        self.assertEqual(
            self.op("lanes-open", "on"),
            (0, {"lanes_open": True, "was": False, "launch_equal": False}),
        )
        self.assertEqual(self.op("launch-equal", "on")[0], 1)
        self.assertEqual(self.op("lanes-open", "off")[1]["lanes_open"], False)
        self.assertEqual(self.op("launch-equal", "on")[1], {"launch_equal": True, "was": False})

    def test_the_miner_routes(self) -> None:
        self.desk.agent_key(A)
        alice = KEYS["Alice"]
        code, work = signed(alice, self.base, "/v1/jobs/work", {}, VALIDATOR.ss58_address)
        self.assertEqual(
            (code, work),
            (200, {"job": None, "lane": "improvement", "rating": 0.0, "threshold": 0.4}),
        )
        # practice is retired, on both of its routes
        retired = {
            "error": "practice is retired: improvement jobs replace it",
            "code": "practice_retired",
        }
        self.assertEqual(
            signed(alice, self.base, "/v1/practice", {}, VALIDATOR.ss58_address), (410, retired)
        )
        self.assertEqual(
            signed(alice, self.base, "/v1/practice", {}, VALIDATOR.ss58_address, method="GET"),
            (410, retired),
        )
        # the entry test: live while the lanes are closed, a stub once they are open
        code, adm = signed(alice, self.base, "/v1/miners/admission", {}, VALIDATOR.ss58_address)
        self.assertEqual((code, adm["job"].startswith("admission-")), (200, True))
        self.desk.set_lanes_open(True)
        self.assertEqual(
            signed(alice, self.base, "/v1/miners/admission", {}, VALIDATOR.ss58_address),
            (200, {"admitted": True, "retired": True, "lane": "improvement", "rating": 0.0}),
        )


if __name__ == "__main__":
    unittest.main()
