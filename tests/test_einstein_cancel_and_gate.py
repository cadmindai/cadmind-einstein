"""A cancel reaches the miner and the gate; the gate is bounded; the validator's own errors are
retried with a back-off and never blamed on a miner, and a judgement that cannot be made ends the
job uncharged; a delivery the platform did not charge withdraws its scores; key mints are checked
and budgeted before the platform is asked."""

from __future__ import annotations

import base64
import tempfile
import threading
import time
import unittest
from typing import Any

from cadmind_einstein.desk import DeskError
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.sealing import new_keypair, valid_public
from tests.test_einstein import GOOD, A, B, C, D, wait_state
from tests.test_einstein_operator_roles import _Case
from tests.test_einstein_security import ALICE, _DeskCase, _Svc


class _Cases(_DeskCase):
    def start(self, job: str, *, by: str = A) -> tuple[str, dict[str, Any]]:
        """Enqueue ``job``; ``by`` (the others held off) gets it and accepts it."""
        held = {hk: self.desk.miners[hk].blocked_until_ms for hk in (A, B, C) if hk != by}
        for hk in held:
            self.desk.miners[hk].blocked_until_ms = self.desk.now_ms() + 10**9
        self.p.queue.append({"job": job, "brief": "A wall hook."})
        for hk in (A, B, C):
            self.desk.work(hk)
        self.tick()
        for hk, was in held.items():
            self.desk.miners[hk].blocked_until_ms = was
        hk, w = self.holder(job)
        self.desk.accept(hk, job)
        return hk, w

    def submit(self, job: str, *, by: str = A) -> str:
        hk, w = self.start(job, by=by)
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, job, w["workspace"], "d")
        return w["workspace"]


class TestMinerToldOfACancel(_Cases):
    def test_work_says_cancelled_and_accept_submit_answer_410(self) -> None:
        hk, w = self.start("cancel-1")
        self.desk.cancel("cancel-1")
        said = self.desk.work(hk)
        self.assertEqual(said, {"job": None, "cancelled": "cancel-1"})
        with self.assertRaises(DeskError) as e:
            self.desk.submit(hk, "cancel-1", w["workspace"], "d")
        self.assertEqual(e.exception.status, 410)
        with self.assertRaises(DeskError) as e:
            self.desk.reject(hk, "cancel-1", "not-capable", "no")
        self.assertEqual(e.exception.status, 410)
        # another miner's call on that job is a plain 404
        with self.assertRaises(DeskError) as e:
            self.desk.accept(B, "cancel-1")
        self.assertEqual(e.exception.status, 404)

    def test_a_cooled_off_miner_gets_no_job_then_a_missed_first_job_scores_nothing(self) -> None:
        hk, _ = self.start("cancel-2", by=A)
        self.desk.cancel("cancel-2")
        for other in (B, C):  # only the cancelled miner is free
            self.desk.miners[other].blocked_until_ms = self.desk.now_ms() + 10**9
        self.p.queue.append({"job": "cancel-3", "brief": "A shelf."})
        self.desk.work(hk)
        self.tick()
        self.assertEqual(self.p.queue[0]["job"], "cancel-3")  # cooling off: nothing claimed
        self.clock.t += 50
        self.desk.work(hk)
        self.tick()
        w = self.desk.work(hk)
        self.assertEqual(
            (w["job"], w["cancelled"]), ("cancel-3", "cancel-2")
        )  # still told of the cancel
        self.clock.t += 121  # still stopping the cancelled job: not accepted in time
        self.desk.work(hk)
        self.tick()
        a = self.desk.jobs["cancel-3"].attempts[0]
        self.assertEqual(a.kind, "stale")
        self.assertEqual(self.desk.ledger.outcomes, [])  # scored nothing
        self.assertEqual(
            self.desk.status("cancel-3")["state"], "queued"
        )  # to another miner, uncounted
        self.assertFalse(self.desk.miners[hk].after_cancel)  # only the first one is forgiven

    def test_accepting_another_job_clears_the_notice(self) -> None:
        hk, _ = self.start("cancel-4", by=A)
        self.desk.cancel("cancel-4")
        self.clock.t += 50
        self.start("cancel-5", by=A)
        self.assertNotIn("cancelled", self.desk.work(hk))


class TestGateStoppedOnCancel(_Cases):
    def test_a_cancel_while_judging_stops_the_guard_run_and_deletes_the_fork(self) -> None:
        real, entered = self.p.run, threading.Event()

        def slow(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "report":
                kw["on_run"]("run-9")
                entered.set()
                kw["stop"].wait(5)
                return {"state": "running", "run": "run-9"}
            return real(ws, command, args)

        self.p.run = slow  # type: ignore[method-assign]
        hk = A
        self.submit("g1", by=hk)
        self.assertTrue(entered.wait(5))
        a = self.desk.jobs["g1"].attempts[0]
        self.assertEqual(a.gate_run, "run-9")
        fork = a.fork
        self.desk.cancel("g1")
        self.assertTrue(self.desk.wait_idle())
        self.assertIn("run-9", self.p.cancelled_runs)
        self.assertIn(fork, self.p.deleted)
        self.assertEqual(self.desk.status("g1")["result"]["label"], "cancelled")
        self.assertEqual(self.desk.ledger.outcomes, [])

    def test_a_run_past_its_budget_is_stopped_once_then_fails_the_attempt(self) -> None:
        real, budgets = self.p.run, []

        def over(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "report":
                budgets.append(kw.get("budget_s"))
                return {"state": "running", "run": f"run-{len(budgets)}"}
            return real(ws, command, args)

        self.p.run = over  # type: ignore[method-assign]
        self.desk.gate_run_s = 900.0
        self.submit("g2")
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status("g2")["state"], "judging")
        self.assertEqual(self.p.cancelled_runs, ["run-1"])  # the lane is freed at once
        self.desk._retry_at.clear()
        self.tick()
        st = wait_state(self.desk, "g2", ("queued", "done"))
        self.assertEqual(st["state"], "queued")
        self.assertEqual(budgets, [900.0, 900.0])
        self.assertEqual(self.desk.jobs["g2"].attempts[0].kind, "gate")


class TestOurErrors(_Cases):
    def _failing(self, error: PlatformError) -> None:
        real = self.p.run

        def busy(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "report":
                raise error
            return real(ws, command, args)

        self.p.run = busy  # type: ignore[method-assign]

    def test_rate_busy_quota_and_timeouts_are_ours(self) -> None:
        cases = [
            PlatformError("slow down", 429, "rate_limited"),
            PlatformError("watt busy", 409, "busy"),
            PlatformError("quota", 400, "account_agent_quota"),
            PlatformError("quota", 413, "storage_limit"),
            PlatformError("timeout", 408),
            PlatformError("down", 502),
        ]
        for n, err in enumerate(cases):
            with self.subTest(code=err.code or err.status):
                self.p.run = type(self.p).run.__get__(self.p)  # type: ignore[method-assign]
                self._failing(err)
                job = f"o{n}"
                self.submit(job, by=(A, B, C)[n % 3])
                self.assertTrue(self.desk.wait_idle())
                a = self.desk.jobs[job].attempts[0]
                self.assertEqual(self.desk.status(job)["state"], "judging")
                self.assertEqual((a.kind, a.retries), ("", 1))
                self.assertGreater(self.desk._retry_at[job], time.monotonic() + 5)
                self.desk.cancel(job)
                self.clock.t += 50  # past the cancelled miner's cool-off
        self.assertEqual(self.desk.ledger.outcomes, [])

    def test_a_judgement_that_keeps_failing_on_our_side_ends_uncharged(self) -> None:
        self.desk.judge_max_retries = 2
        self._failing(PlatformError("busy", 409, "run_in_progress"))
        self.submit("o9")
        for _ in range(3):
            self.assertTrue(self.desk.wait_idle())
            self.desk._retry_at.clear()
            self.tick()
        st = wait_state(self.desk, "o9", ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("didn't finish", False))
        a = self.desk.jobs["o9"].attempts[0]
        self.assertEqual((a.kind, a.retries), ("ours", 3))
        self.assertEqual(self.desk.ledger.outcomes, [])  # nobody scored
        self.assertEqual(self.desk.miners[A].job, "")  # the miner is free
        self.assertIn(a.fork, self.p.deleted)
        self.tick()
        self.assertEqual(self.p.completed["o9"]["label"], "didn't finish")

    def test_give_up_after_its_time_too(self) -> None:
        self._failing(PlatformError("down", 503))
        self.submit("o10")
        self.assertTrue(self.desk.wait_idle())
        self.clock.t += self.desk.judge_give_up_s + 1
        self.desk._retry_at.clear()
        self.tick()
        self.assertEqual(self.desk.status("o10")["result"]["label"], "didn't finish")

    def test_backoff_doubles(self) -> None:
        self._failing(PlatformError("down", 503))
        self.submit("o11")
        delays = []
        for _ in range(3):
            self.assertTrue(self.desk.wait_idle())
            delays.append(self.desk._retry_at["o11"] - time.monotonic())
            self.desk._retry_at.clear()
            self.tick()
        self.assertGreater(delays[1], delays[0] * 1.4)
        self.assertGreater(delays[2], delays[1] * 1.4)

    def test_an_unexpected_error_is_retried_not_a_hot_loop(self) -> None:
        real = self.p.run

        def boom(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "report":
                raise RecursionError("deep")
            return real(ws, command, args)

        self.p.run = boom  # type: ignore[method-assign]
        self.submit("o12")
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status("o12")["state"], "judging")
        self.assertGreater(self.desk._retry_at["o12"], time.monotonic() + 5)

    def test_an_unreadable_export_fails_the_gate(self) -> None:
        self.p.export = lambda ws: b"not a zip at all"  # type: ignore[method-assign]
        self.submit("o13")
        st = wait_state(self.desk, "o13", ("queued", "done"))
        self.assertEqual(st["state"], "queued")
        self.assertEqual(self.desk.jobs["o13"].attempts[0].kind, "gate")


class TestUncharged(_Cases):
    def test_a_delivery_the_platform_did_not_charge_withdraws_its_scores(self) -> None:
        self.p.complete = lambda job, result: {  # type: ignore[method-assign]
            "accepted": True,
            "charged": False,
            "label": "cancelled",
        }
        self.submit("u1")
        wait_state(self.desk, "u1", ("done",))
        self.tick()  # the result is sent; the platform answers it charged nothing
        self.assertEqual(self.desk.ledger.outcomes, [])
        self.assertEqual(self.desk.delivered.get(A), 0)
        r = self.desk.status("u1")["result"]
        self.assertEqual((r["label"], r["charge"], r["uncharged"]), ("cancelled", False, True))
        self.assertNotIn("u1", self.desk._outbox)

    def test_an_old_platform_answer_changes_nothing(self) -> None:
        self.submit("u2")
        wait_state(self.desk, "u2", ("done",))
        self.tick()
        self.assertEqual(self.desk.status("u2")["result"]["label"], "delivered")
        self.assertEqual(len(self.desk.ledger.outcomes), 1)


class TestMints(_Cases):
    def test_a_bad_public_key_mints_nothing(self) -> None:
        minted = dict(self.p.keys)
        for bad in (b"", b"x" * 31, bytes(32)):
            with self.assertRaises(DeskError) as e:
                self.desk.agent_key(B, bad)
            self.assertEqual(e.exception.status, 400)
        self.assertEqual(self.p.keys, minted)
        self.assertFalse(valid_public(bytes(32)))
        self.assertTrue(valid_public(new_keypair()[1]))

    def test_a_fresh_key_is_not_minted_again(self) -> None:
        with self.assertRaises(DeskError) as e:
            self.desk.agent_key(A, new_keypair()[1])
        self.assertEqual(e.exception.status, 429)
        self.clock.t += 601
        self.desk.agent_key(A, new_keypair()[1])

    def test_the_validators_budgets_stop_before_the_platform(self) -> None:
        self.desk.mint_fresh_s = 0
        self.desk.mint_per_coldkey_hour = 0
        self.desk.mint_per_hour = len(self.desk._mints) + 1
        self.desk.agent_key(A, new_keypair()[1])
        calls = []
        real = self.p.mint_key
        self.p.mint_key = lambda hk: calls.append(hk) or real(hk)  # type: ignore[method-assign]
        with self.assertRaises(DeskError) as e:
            self.desk.agent_key(B, new_keypair()[1])
        self.assertEqual((e.exception.status, calls), (429, []))
        self.desk.disable(B)  # a revocation is never counted, never refused
        self.clock.t += 3601
        self.desk.mint_per_coldkey_hour = 1
        self.desk.mint_per_hour = 100
        self.desk.coldkeys = lambda: {A: "same", C: "same"}
        self.desk.agent_key(A, new_keypair()[1])
        with self.assertRaises(DeskError) as e:
            self.desk.agent_key(C, new_keypair()[1])
        self.assertEqual(e.exception.status, 429)


class TestServiceChecksTheKeyFirst(_DeskCase):
    def test_a_malformed_key_is_400_and_takes_no_place_in_the_rate(self) -> None:
        svc = _Svc(agent_key_per_hotkey=(1, 3600.0))
        try:
            for bad in ("", "not base64!", base64.b64encode(b"x" * 31).decode()):
                code, _ = svc.signed(ALICE, "/v1/miners/agent_key", {"x25519_public": bad})
                self.assertEqual(code, 400)
            good = base64.b64encode(new_keypair()[1]).decode()
            code, _ = svc.signed(ALICE, "/v1/miners/agent_key", {"x25519_public": good})
            self.assertEqual(code, 200)
        finally:
            svc.close()


class TestClientRuns(_Case):
    def test_run_reports_its_id_and_stops_when_told(self) -> None:
        self.platform.rules[("POST", "/api/agent/workspaces/")] = (
            200,
            {"state": "running", "run": "r1"},
            {},
        )
        stop, seen = threading.Event(), []
        stop.set()
        out = self.client().run("w", "report", {"mutate": True}, on_run=seen.append, stop=stop)
        self.assertEqual((out["state"], seen), ("running", ["r1"]))
        self.assertFalse(any(p.startswith("/api/agent/runs/") for _m, p, _k in self.platform.seen))

    def test_cancel_run_and_a_gone_run(self) -> None:
        c = self.client()
        c.cancel_run("r1")
        self.assertIn(("POST", "/api/agent/runs/r1/cancel", "op"), self.platform.seen)
        self.platform.rules[("POST", "/api/agent/runs/")] = (404, {"error": "gone"}, {})
        c.cancel_run("r2")  # already over: nothing to stop

    def test_complete_answers_what_the_platform_said(self) -> None:
        self.queue.rules[("POST", "/api/engine/einstein/jobs/")] = (
            200,
            {"accepted": True, "charged": False, "label": "cancelled"},
            {},
        )
        self.assertEqual(self.client().complete("j", {"label": "delivered"})["charged"], False)


class TestDurable(unittest.TestCase):
    def test_the_cancel_notice_survives_a_restart_and_old_rows_load(self) -> None:
        from tests.test_einstein_durable import Rig

        with tempfile.TemporaryDirectory() as d:
            r = Rig(d)
            r.admit(A, B, C)
            r.poll(A, B, C)
            assert r.desk is not None and r.store is not None
            r.p.queue.append({"job": "d1", "brief": "A wall hook."})
            r.tick()
            hk, _ = r.holder("d1")
            r.desk.cancel("d1")
            # a miner row written before these fields existed
            r.store.put("miner", D, {"hotkey": D, "key_id": "k", "last_seen_ms": 0})
            desk = r.restart()
            self.assertEqual(desk.work(hk).get("cancelled"), "d1")
            self.assertTrue(desk.miners[hk].after_cancel)
            self.assertEqual(desk.miners[D].cancelled_job, "")
            desk.stop()
