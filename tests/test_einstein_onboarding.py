"""Manual miner onboarding and launch-equal weights (0.4.8).

- A registered hotkey's key request is recorded as pending until the operator approves it; only
  then is a key minted. A refusal or a revocation is answered with the operator's reason.
- Only an approved key holder takes the entry test; until it passes, no job and no weight.
- A revocation drops the key and the admission, frees the miner's job unscored, and removes the
  miner from the next vector under every rule; re-approved, it takes the entry test again.
- The entry test: a client-style brief at its own effort and deadline, no questions, through the
  same gate as a client job (the validator's guard, no unproven must, no open client decision),
  submitted on time.
- Launch: equal per admitted, reachable hotkey until a time, then the usual rules; recompute
  reproduces it.
"""

from __future__ import annotations

import base64
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

from cadmind_einstein import onboarding
from cadmind_einstein.desk import ADMISSION_BRIEF, DEADLINE_S, Desk, DeskError
from cadmind_einstein.fake_miner import ask_key
from cadmind_einstein.scoring import (
    HOUR_MS,
    JobOutcome,
    Ledger,
    recompute,
    report_order,
    score_all,
    standing,
    vector,
)
from cadmind_einstein.sealing import new_keypair, open_sealed
from cadmind_einstein.service import serve
from cadmind_einstein.store import Store
from tests.test_einstein import GOOD, OWNER, VALIDATOR, A, B, C, Clock, FakePlatform, wait_state

OPEN = """[decision.mount]
question = "Screwed to the wall, or stuck on?"
options = { screws = "two screws", tape = "foam tape" }
by = "client"
status = "open"
assumption = "screws"
because = "the load is light"
affects = ["R.holds"]
"""

COLD = {A: "cold-a", B: "cold-b", C: "cold-c"}


class _Case(unittest.TestCase):
    approval = "manual"

    def setUp(self) -> None:
        self.p = FakePlatform()
        self.clock = Clock()
        self.ledger = Ledger(hour_ms=HOUR_MS)
        self.reg = [A, B, C]
        self.cold = dict(COLD)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "s.sqlite3")
        self.desk = self.make()

    def make(self) -> Desk:
        return Desk(
            self.p,
            self.ledger,
            registered=lambda: self.reg,
            clock=self.clock,
            rng=random.Random(3),  # noqa: S311 -- a test's seed
            key_approval=self.approval,
            coldkeys=lambda: self.cold,
            store=self.store,
            excluded=[OWNER],
        )

    def tearDown(self) -> None:
        self.desk.stop()
        self.tmp.cleanup()

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def key(self, hk: str, **kw: Any) -> dict[str, Any]:
        return self.desk.agent_key(hk, new_keypair()[1], **kw)

    def onboard(self, hk: str) -> str:
        """Approved, keyed and through the entry test; answers the test's job id."""
        self.desk.approve(hk)
        self.assertIn("key_id", self.key(hk))
        return self.pass_test(hk)

    def pass_test(self, hk: str, files: dict[str, str] | None = None, **kw: Any) -> str:
        adm = self.desk.admission(hk)
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(hk)
        self.assertEqual(w["job"], adm["job"])
        self.desk.accept(hk, w["job"])
        self.p.work(w["workspace"], files or GOOD, deliverable=True, **kw)
        self.desk.submit(hk, w["job"], w["workspace"], "d")
        wait_state(self.desk, w["job"], ("done",))
        return str(w["job"])


class TestKeyApproval(_Case):
    def test_an_unapproved_hotkey_gets_no_key_and_its_ask_is_recorded(self) -> None:
        got = self.key(A, ip="203.0.113.7", note="  reach me\non the forum\x07 ")
        self.assertTrue(got["pending"])
        self.assertNotIn("sealed", got)
        self.assertIn("approval", got["message"])
        self.assertEqual(self.p.keys, {})  # nothing minted
        first = self.clock.t
        self.clock.t += 600
        self.key(A, ip="203.0.113.8")
        [row] = self.desk.key_requests()
        self.assertEqual(row["hotkey"], A)
        self.assertEqual(row["coldkey"], "cold-a")
        self.assertEqual((row["first_ms"], row["last_ms"]), (first * 1000, self.clock.t * 1000))
        self.assertEqual((row["asks"], row["ip"]), (2, "203.0.113.8"))
        self.assertEqual(row["note"], "reach me on the forum")  # one printable line, kept
        self.assertEqual(self.desk.miners[A].key_id, "")
        with self.assertRaises(DeskError) as e:
            self.desk.admission(A)  # no entry test before approval
        self.assertEqual(e.exception.status, 403)

    def test_once_approved_the_same_call_mints_the_sealed_key(self) -> None:
        self.key(A)
        self.desk.approve(A)
        priv, pub = new_keypair()
        got = self.desk.agent_key(A, pub)
        self.assertEqual(open_sealed(got["sealed"], priv, A), self.p.keys[A])
        self.assertEqual(self.desk.key_requests(), [])  # no longer pending
        [row] = self.desk.key_requests("approved")
        self.assertEqual(
            (row["approved_coldkey"], row["keyed"], row["admitted"]), ("cold-a", True, False)
        )

    def test_an_operator_may_approve_a_registered_hotkey_before_it_asks(self) -> None:
        self.desk.approve(B)
        self.assertIn("key_id", self.key(B))
        with self.assertRaises(DeskError) as e:
            self.desk.approve("5NotRegistered")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(DeskError):
            self.desk.approve(OWNER)

    def test_a_refusal_answers_its_reason_and_mints_nothing(self) -> None:
        self.key(A)
        with self.assertRaises(DeskError):
            self.desk.refuse(A, "   ")  # a refusal says why
        self.desk.refuse(A, "no contact given")
        with self.assertRaises(DeskError) as e:
            self.key(A)
        self.assertEqual(e.exception.status, 403)
        self.assertIn("no contact given", str(e.exception))
        self.assertEqual(self.p.keys, {})
        self.desk.approve(B)
        self.key(B)
        with self.assertRaises(DeskError) as e:
            self.desk.refuse(B, "x")  # holds a key: revoke it
        self.assertEqual(e.exception.status, 409)

    def test_an_approval_for_one_coldkey_does_not_hold_for_another(self) -> None:
        self.desk.approve(A)
        self.key(A)
        self.clock.t += 3600
        self.cold[A] = "cold-other"
        got = self.key(A)
        self.assertTrue(got["pending"])
        self.assertFalse(self.desk.approved(A))

    def test_approvals_survive_a_restart(self) -> None:
        self.key(A, note="hi")
        self.desk.approve(B)
        self.key(C)
        self.desk.refuse(C, "duplicate")
        self.desk.stop()
        self.desk = self.make()
        states = {r["hotkey"]: r["state"] for r in self.desk.key_requests("all")}
        self.assertEqual(states, {A: "pending", B: "approved", C: "refused"})
        self.assertEqual(self.desk.key_requests()[0]["note"], "hi")
        self.assertIn("key_id", self.key(B))

    def test_a_key_held_before_manual_approval_is_revoked_and_its_hotkey_pending(self) -> None:
        self.desk.stop()
        self.approval = "auto"
        self.desk = self.make()
        for hk in (A, B):
            self.key(hk)
        with self.desk._mut():  # persisted, as a passed test would be
            self.desk.miners[A].admitted = True
        self.desk.work(A)
        self.desk.work(B)
        self.p.queue.append({"job": "g1", "brief": "A wall hook."})
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())
        holder = next(h for h in (A, B) if self.desk.work(h)["job"] == "g1")
        self.assertEqual(holder, A)  # the only admitted miner
        self.desk.stop()
        self.approval = "manual"
        self.desk = self.make()  # the validator restarts under manual approval
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.admitted(), [])
        self.assertEqual(
            {r["hotkey"]: (r["state"], r["by"]) for r in self.desk.key_requests("all")},
            {A: ("pending", ""), B: ("pending", "")},
        )
        for hk in (A, B):
            self.assertEqual(self.desk.miners[hk].key_id, "")
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.p.keys, {})  # deleted on the platform
        st = self.desk.status("g1")
        self.assertEqual((st["state"], st["attempts"][-1]["kind"]), ("queued", "revoked"))
        self.assertEqual(self.ledger.outcomes, [])
        self.assertTrue(self.key(A)["pending"])  # asks like anyone else
        self.desk.approve(A)
        self.assertIn("key_id", self.key(A))
        self.assertEqual(self.desk.admitted(), [])  # and passes the entry test first
        self.pass_test(A)
        self.assertEqual(self.desk.admitted(), [A])

    def test_the_pending_message_is_the_operators(self) -> None:
        self.assertIn("approval", self.key(A)["message"])
        d = Desk(
            self.p,
            Ledger(),
            registered=lambda: [B],
            key_approval="manual",
            pending_message="Ask on the  official\nchannel.",
        )
        self.assertEqual(
            d.agent_key(B, new_keypair()[1])["message"], "Ask on the official channel."
        )
        d.stop()

    def test_a_pending_request_of_a_hotkey_that_left_is_dropped(self) -> None:
        self.key(A)
        self.key(B)
        self.desk.refuse(B, "no")
        self.reg = [C]
        self.desk.sync_registered(self.reg)
        self.assertEqual([r["hotkey"] for r in self.desk.key_requests("all")], [B])


class TestAutoApproval(_Case):
    approval = "auto"

    def test_auto_keeps_the_self_serve_key(self) -> None:
        self.assertIn("key_id", self.key(A))
        self.assertEqual(self.desk.key_requests("all"), [])

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Desk(self.p, Ledger(), registered=list, key_approval="open")


class TestEntryTestGates(_Case):
    def test_approved_but_untested_gets_no_job_and_no_weight(self) -> None:
        for hk in (A, B):
            self.desk.approve(hk)
            self.key(hk)
        self.desk.work(A)
        self.desk.work(B)
        self.p.queue.append({"job": "c1", "brief": "A wall hook."})
        self.tick()
        self.assertEqual([j["job"] for j in self.p.queue], ["c1"])  # nobody admitted: unclaimed
        self.assertEqual(self.desk.capacity()["active_miners"], 0)
        self.assertEqual(self.desk.admitted(), [])
        uids = {OWNER: 0, A: 1, B: 2, C: 3}
        rep = self.ledger.report(
            self.desk.now_ms(),
            uids,
            owner_hotkey=OWNER,
            admitted=self.desk.admitted(),
            reachable=self.desk.reachable(),
            genesis="coldkey",
        )
        self.assertEqual(rep["vector"], [])  # keyed, registered, reachable: still no weight
        self.pass_test(A)
        self.assertEqual(self.desk.admitted(), [A])
        self.desk.work(A)
        self.tick()
        self.assertEqual(self.desk.work(A)["job"], "c1")

    def test_the_entry_test_is_a_high_effort_no_questions_job(self) -> None:
        self.desk.approve(A)
        self.key(A)
        adm = self.desk.admission(A)
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(adm["effort"], "high")
        w = self.desk.work(A)
        self.assertIs(w["no_questions"], True)  # the miner is told to decide every question
        self.assertEqual(w["brief"], ADMISSION_BRIEF)
        acc = self.desk.accept(A, w["job"])
        self.assertEqual(acc["deadline_ms"], self.desk.now_ms() + DEADLINE_S["high"] * 1000)

    def test_the_admission_effort_is_an_option_with_its_own_deadline(self) -> None:
        d = Desk(self.p, Ledger(), registered=lambda: [A], admission_effort="medium")
        d.agent_key(A, new_keypair()[1])
        self.assertEqual(d.admission(A)["effort"], "medium")
        d.stop()
        with self.assertRaises(ValueError):
            Desk(self.p, Ledger(), registered=lambda: [A], admission_effort="huge")

    def test_the_default_brief_is_a_small_device_not_the_worked_example(self) -> None:
        self.assertNotIn("watt example", ADMISSION_BRIEF)
        self.assertIn("decide every open question", ADMISSION_BRIEF)

    def test_an_unproven_must_fails_the_entry_test(self) -> None:
        self.desk.approve(A)
        self.key(A)
        job = self.pass_test(A, weak=True)  # only a lucky draw passed: the validator's guard
        st = self.desk.status(job)
        self.assertEqual(st["result"]["label"], "not admitted")
        self.assertIn("unproven R.fits", st["result"]["why"])
        self.assertEqual(self.desk.admitted(), [])
        self.assertEqual(self.desk.admission_failed[A], 1)

    def test_an_open_client_decision_fails_the_entry_test(self) -> None:
        self.desk.approve(A)
        self.key(A)
        job = self.pass_test(A, {**GOOD, "spec/decisions.toml": OPEN})
        st = self.desk.status(job)
        self.assertEqual(st["result"]["label"], "not admitted")
        self.assertIn("asked not to be asked", st["result"]["why"])
        self.assertEqual(self.desk.admitted(), [])

    def test_an_entry_test_past_its_deadline_does_not_admit(self) -> None:
        self.desk.approve(A)
        self.key(A)
        self.desk.admission(A)
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(A)
        self.desk.accept(A, w["job"])
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.clock.t += DEADLINE_S["high"] + 60
        self.tick()  # judged at its deadline, never submitted
        st = wait_state(self.desk, w["job"], ("done",))
        self.assertEqual(st["result"]["label"], "not admitted")
        self.assertEqual(self.desk.admitted(), [])

    def test_one_entry_test_a_day(self) -> None:
        self.desk.approve(A)
        self.key(A)
        first = self.desk.admission(A)
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(A)
        self.desk.accept(A, w["job"])
        self.desk.submit(A, w["job"], w["workspace"], "")  # nothing built: fails
        self.assertTrue(self.desk.wait_idle())
        again = self.desk.admission(A)  # within the day: the same test, no new one
        self.assertEqual((again["job"], again["result"]["label"]), (first["job"], "not admitted"))
        self.clock.t += 86_400
        self.assertNotEqual(self.desk.admission(A)["job"], first["job"])


class TestRevoke(_Case):
    def setUp(self) -> None:
        super().setUp()
        for hk in (A, B):
            self.onboard(hk)

    def test_revoke_frees_the_job_unscored_and_another_miner_takes_it(self) -> None:
        self.desk.work(A)
        self.desk.work(B)
        self.p.queue.append({"job": "r1", "brief": "A wall hook."})
        self.tick()
        holder = next(h for h in (A, B) if self.desk.work(h)["job"] == "r1")
        other = B if holder == A else A
        self.desk.accept(holder, "r1")
        out = self.desk.revoke(holder, "left the team")
        self.assertEqual(out["freed_job"], "r1")
        self.assertTrue(self.desk.wait_idle())
        st = self.desk.status("r1")
        self.assertEqual(st["state"], "queued")
        self.assertEqual(st["attempts"][-1]["kind"], "revoked")
        self.assertEqual(self.ledger.outcomes, [])  # nobody scored
        self.assertNotIn(holder, self.p.keys)  # the key deleted on the platform
        m = self.desk.miners[holder]
        self.assertEqual((m.key_id, m.admitted, m.job), ("", False, ""))
        self.assertIsNone(self.desk.work(holder)["job"])
        self.desk.work(other)
        self.tick()
        self.assertEqual(self.desk.work(other)["job"], "r1")
        with self.assertRaises(DeskError) as e:
            self.key(holder)
        self.assertEqual(e.exception.status, 403)
        self.assertIn("left the team", str(e.exception))
        with self.assertRaises(DeskError):
            self.desk.admission(holder)

    def test_revoke_ends_an_entry_test_it_holds_without_counting_a_failure(self) -> None:
        self.desk.approve(C)
        self.key(C)
        adm = self.desk.admission(C)
        self.assertTrue(self.desk.wait_idle())
        self.desk.accept(C, adm["job"])
        self.desk.revoke(C, "spam")
        st = self.desk.status(adm["job"])
        self.assertEqual(st["result"]["label"], "not admitted")
        self.assertEqual(self.desk.admission_failed.get(C, 0), 0)

    def test_an_entry_test_judged_after_a_revoke_does_not_admit(self) -> None:
        self.desk.approve(C)
        self.key(C)
        adm = self.desk.admission(C)
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(C)
        self.desk.accept(C, w["job"])
        self.p.work(w["workspace"], GOOD, deliverable=True)
        gate = threading.Event()
        real = self.p.freeze

        def slow(ws: str) -> dict[str, Any]:
            gate.wait(5)
            return real(ws)

        self.p.freeze = slow  # type: ignore[method-assign]
        self.desk.submit(C, w["job"], w["workspace"], "d")
        self.desk.revoke(C, "spam")
        gate.set()
        st = wait_state(self.desk, adm["job"], ("done",))
        self.assertEqual(st["result"]["label"], "not admitted")
        self.assertNotIn(C, self.desk.admitted())

    def test_a_revoked_miner_is_out_of_the_next_vector_and_retested_when_reapproved(self) -> None:
        uids = {OWNER: 0, A: 1, B: 2}
        now = self.desk.now_ms()
        self.ledger.add(_passing("h1", A, now - HOUR_MS))
        self.ledger.add(_passing("h2", B, now - HOUR_MS))
        rep = self._report(uids)
        self.assertEqual({u for u, _ in rep["vector"]}, {1, 2})
        self.desk.revoke(A, "abuse")
        rep = self._report(uids)
        self.assertEqual([u for u, _ in rep["vector"]], [2])  # its history earns nothing now
        self.desk.approve(A)
        self.assertIn("key_id", self.key(A))
        self.assertNotIn(A, self.desk.admitted())  # approved again: still not admitted
        self.assertEqual([u for u, _ in self._report(uids)["vector"]], [2])
        self.pass_test(A)  # at once: the daily limit restarts on a revoke
        self.assertIn(A, self.desk.admitted())
        self.assertEqual({u for u, _ in self._report(uids)["vector"]}, {1, 2})

    def _report(self, uids: dict[str, int]) -> dict[str, Any]:
        self.desk.work(A)
        self.desk.work(B)
        return self.ledger.report(
            self.desk.now_ms(),
            uids,
            owner_hotkey=OWNER,
            admitted=self.desk.admitted(),
            reachable=self.desk.reachable(),
        )


def _passing(job: str, hk: str, ended_ms: int) -> JobOutcome:
    return JobOutcome(
        job=job, hotkey=hk, ended_ms=ended_ms, deliverable=True, kind="pass", compute_s=1, wall_s=1
    )


UIDS: dict[str, int] = {OWNER: 0, A: 1, B: 2, C: 3}


def _weight(*hks: str) -> dict[str, float]:
    """The decayed weight of one passing job each, an hour old at hour 10."""
    now = 10 * HOUR_MS
    jobs = score_all([_passing(f"j{h}", h, now - HOUR_MS) for h in hks])
    return standing(jobs, now)[0]


class TestVectorOnlyAdmitted(unittest.TestCase):
    """Every rule and fallback of :func:`vector` weights admitted hotkeys only."""

    uids = UIDS

    def weight(self, *hks: str) -> dict[str, float]:
        return _weight(*hks)

    def test_history_counts_admitted_reachable_miners_only(self) -> None:
        vec, rule = vector(
            self.weight(A, B), self.uids, owner_hotkey=OWNER, admitted=[B], reachable=[A, B]
        )
        self.assertEqual((vec, rule), ([(2, 65_535)], "history"))

    def test_history_without_a_reachable_miner_counts_admitted_ones_only(self) -> None:
        vec, rule = vector(
            self.weight(A, B), self.uids, owner_hotkey=OWNER, admitted=[B], reachable=[]
        )
        self.assertEqual(vec, [(2, 65_535)])
        self.assertIn("no admitted miner reachable", rule)

    def test_the_kept_vector_drops_hotkeys_no_longer_admitted(self) -> None:
        last = {A: 65_535.0, B: 30_000.0}
        vec, rule = vector({}, self.uids, owner_hotkey=OWNER, admitted=[B], reachable=[], last=last)
        self.assertEqual(vec, [(2, 65_535)])
        self.assertTrue(rule.startswith("kept"))
        # nobody of the last vector admitted: never re-sent; with no admitted miner, empty
        vec, rule = vector({}, self.uids, owner_hotkey=OWNER, admitted=[], reachable=[], last=last)
        self.assertEqual(vec, [])

    def test_the_never_empty_fallback_is_live_admitted_miners_only(self) -> None:
        vec, rule = vector(
            {}, self.uids, owner_hotkey=OWNER, admitted=[C], reachable=[A, B, C], last={}
        )
        self.assertEqual(vec, [(3, 65_535)])
        self.assertIn("never empty", rule)

    def test_genesis_rules_weight_admitted_reachable_miners_only(self) -> None:
        for genesis in ("delivered", "coldkey", "hotkey"):
            vec, _ = vector(
                {},
                self.uids,
                owner_hotkey=OWNER,
                admitted=[B],
                reachable=[A, B, C],
                genesis=genesis,
                proven=[A, B, C],
            )
            self.assertEqual(vec, [(2, 65_535)], genesis)


class TestLaunchEqual(unittest.TestCase):
    uids = UIDS

    def launch(self, on: bool, **kw: Any) -> tuple[list[tuple[int, int]], str]:
        args: dict[str, Any] = {
            "owner_hotkey": OWNER,
            "admitted": [A, B, OWNER],
            "reachable": [A, B, OWNER],
            "coldkeys": {A: "same", B: "same"},
            **kw,
        }
        return vector(_weight(A), self.uids, launch_equal=on, **args)

    def test_on_equal_per_hotkey_whatever_the_history(self) -> None:
        vec, rule = self.launch(True)
        self.assertEqual(vec, [(1, 65_535), (2, 65_535)])  # per hotkey, never the owner
        self.assertEqual(rule, "launch: equal among admitted miners")

    def test_off_the_usual_rules(self) -> None:
        self.assertEqual(self.launch(False), ([(1, 65_535)], "history"))

    def test_never_empty_and_admitted_only(self) -> None:
        vec, rule = self.launch(True, reachable=[], last={C: 1.0})
        self.assertEqual(vec, [(1, 65_535), (2, 65_535)])
        self.assertIn("never empty", rule)
        vec, rule = self.launch(True, admitted=[], reachable=[A], last={A: 1.0})
        self.assertEqual(vec, [])
        vec, _ = self.launch(True, admitted=[B], reachable=[A, B, C])
        self.assertEqual(vec, [(2, 65_535)])  # a revoked (not admitted) miner gets nothing

    def test_the_report_records_the_switch_and_recompute_follows_it(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(report_dir=Path(d))
            ledger.add(_passing("1", A, 8 * HOUR_MS))
            for on, rule in ((True, "launch: equal among admitted miners"), (False, "history")):
                rep = ledger.report(
                    9 * HOUR_MS,
                    self.uids,
                    owner_hotkey=OWNER,
                    admitted=[A, B],
                    reachable=[A, B],
                    launch_equal=on,
                )
                self.assertEqual((rep["rule"], rep["launch_equal"]), (rule, on))
            files = sorted(Path(d).glob("hour-*.json"), key=report_order)
            self.assertEqual(len(files), 2)
            for f in files:
                published = json.loads(f.read_text())
                again = recompute(published)
                self.assertTrue(again["matches_published"], f.name)
                self.assertEqual(again["rule"], published["rule"])
            # a report from before the switch existed (no field) is recomputed with it off
            old = json.loads(files[-1].read_text())
            del old["launch_equal"]
            self.assertTrue(recompute(old)["matches_published"])


class TestLaunchSwitch(_Case):
    def test_the_switch_is_persisted_and_changed_at_runtime(self) -> None:
        self.assertFalse(self.desk.launch_equal)
        self.assertEqual(self.desk.set_launch_equal(True), {"launch_equal": True, "was": False})
        self.desk.stop()
        self.desk = self.make()
        self.assertTrue(self.desk.launch_equal)  # persisted
        self.desk.set_launch_equal(False)
        self.desk.stop()
        self.desk = self.make()
        self.assertFalse(self.desk.launch_equal)


class TestAskKey(unittest.TestCase):
    def test_the_miner_waits_while_pending_and_takes_the_key_once_approved(self) -> None:
        answers = [
            (202, {"pending": True, "message": "wait", "retry_after_s": 900}),
            (202, {"pending": True, "message": "wait", "retry_after_s": 900}),
            (200, {"key_id": "k", "sealed": {}}),
        ]
        bodies: list[dict[str, Any]] = []
        slept: list[float] = []

        def call(path: str, body: dict[str, Any]) -> tuple[int, Any]:
            self.assertEqual(path, "/v1/miners/agent_key")
            bodies.append(body)
            return answers.pop(0)

        code, got = ask_key(
            call, b"\x01" * 32, contact="me", log=lambda _m: None, sleep=slept.append
        )
        self.assertEqual((code, got["key_id"]), (200, "k"))
        self.assertEqual(slept, [900.0, 900.0])
        self.assertEqual(bodies[0]["contact"], "me")

    def test_a_bounded_wait_gives_up_pending(self) -> None:
        def call(path: str, body: dict[str, Any]) -> tuple[int, Any]:
            return 202, {"pending": True, "retry_after_s": 900}

        code, _ = ask_key(
            call, b"\x01" * 32, log=lambda _m: None, sleep=lambda _s: None, max_wait_s=60
        )
        self.assertEqual(code, 202)


TOKEN = "t" * 32


class TestHttpAndCli(unittest.TestCase):
    def setUp(self) -> None:
        self.p = FakePlatform()
        self.desk = Desk(self.p, Ledger(), registered=lambda: [A, B], key_approval="manual")
        self.server = serve(
            self.desk,
            "127.0.0.1",
            0,
            token=TOKEN,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
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

    def ask(self, contact: str = "") -> tuple[int, dict[str, Any], Any]:
        alice = Keypair.create_from_uri("//Alice")
        priv, pub = new_keypair()
        body = json.dumps(
            {"x25519_public": base64.b64encode(pub).decode(), "contact": contact}
        ).encode()
        headers = {
            "Content-Type": "application/json",
            **btauth.sign(
                alice,
                method="POST",
                path="/v1/miners/agent_key",
                body=body,
                receiver_ss58=VALIDATOR.ss58_address,
            ),
        }
        req = urllib.request.Request(  # noqa: S310 -- loopback
            self.base + "/v1/miners/agent_key", data=body, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 -- loopback
                return r.status, json.loads(r.read()), priv
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), priv

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = onboarding.main(
                ["--validator", self.base, "--token-file", str(self.token_file), *args]
            )
        return code, out.getvalue(), err.getvalue()

    def test_pending_then_approved_over_http_and_the_cli(self) -> None:
        code, got, _ = self.ask("forum: alice")
        self.assertEqual(code, 202)
        self.assertTrue(got["pending"])
        rc, out, _ = self.cli("pending")
        self.assertEqual(rc, 0)
        listed = json.loads(out)
        self.assertEqual(listed["key_approval"], "manual")
        self.assertEqual([r["hotkey"] for r in listed["requests"]], [A])
        self.assertEqual(listed["requests"][0]["note"], "forum: alice")
        self.assertEqual(listed["requests"][0]["ip"], "127.0.0.1")
        self.assertNotIn(TOKEN, out)
        self.assertEqual(self.cli("approve", A)[0], 0)
        code, got, priv = self.ask()
        self.assertEqual(code, 200)
        self.assertEqual(open_sealed(got["sealed"], priv, A), self.p.keys[A])
        rc, out, err = self.cli("revoke", A, "--reason", "testing")
        self.assertEqual(rc, 0)
        self.assertNotIn(TOKEN, out + err)
        self.assertNotIn(A, self.p.keys)
        code, got, _ = self.ask()
        self.assertEqual(code, 403)
        self.assertIn("testing", got["error"])
        rc, _, err = self.cli("refuse", B, "--reason", "unknown")
        self.assertEqual(rc, 0)
        rc, out, _ = self.cli("pending", "--state", "all")
        self.assertEqual(
            {r["hotkey"]: r["state"] for r in json.loads(out)["requests"]},
            {A: "revoked", B: "refused"},
        )
        rc, _, err = self.cli("approve", "5Nobody")
        self.assertEqual(rc, 1)
        self.assertIn("not registered", err)

    def test_the_launch_switch_over_the_cli(self) -> None:
        rc, out, _ = self.cli("launch-equal", "status")
        self.assertEqual((rc, json.loads(out)), (0, {"launch_equal": False}))
        rc, out, _ = self.cli("launch-equal", "on")
        self.assertEqual((rc, json.loads(out)["launch_equal"]), (0, True))
        self.assertTrue(self.desk.launch_equal)
        self.cli("launch-equal", "off")
        self.assertFalse(self.desk.launch_equal)
        req = urllib.request.Request(  # noqa: S310 -- loopback
            self.base + "/v1/operator/launch_equal",
            data=json.dumps({"on": "yes"}).encode(),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)  # noqa: S310
        self.assertEqual(e.exception.code, 400)

    def test_the_operator_routes_need_the_token(self) -> None:
        for path, data in (
            ("/v1/operator/key_requests", None),
            ("/v1/operator/approve", json.dumps({"hotkey": A}).encode()),
            ("/v1/operator/revoke", json.dumps({"hotkey": A, "reason": "x"}).encode()),
            ("/v1/operator/launch_equal", json.dumps({"on": True}).encode()),
        ):
            req = urllib.request.Request(self.base + path, data=data)  # noqa: S310 -- loopback
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=10)  # noqa: S310
            self.assertEqual(e.exception.code, 401, path)
        self.assertFalse(self.desk.approved(A) or self.desk.launch_equal)

    def test_the_cli_refuses_a_token_file_others_can_read(self) -> None:
        os.chmod(self.token_file, 0o644)  # noqa: PTH101
        with self.assertRaises(SystemExit):
            self.cli("pending")


if __name__ == "__main__":
    unittest.main()
