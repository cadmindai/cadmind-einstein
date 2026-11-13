"""Manual miner onboarding and launch-equal weights (0.4.8).

- A registered hotkey's key request is recorded as pending until the operator approves it; only
  then is its key looked up. A refusal or a revocation is answered with the operator's reason.
- Only an approved key holder takes the entry test; until it passes, no job and no weight.
- A revocation drops the key and the admission, frees the miner's job unscored, and removes the
  miner from the next vector under every rule; re-approved, it takes the entry test again.
- The entry test: a client-style brief with the one job deadline, no questions, through the
  same gate as a client job (the validator's guard, no unproven must, no open client decision),
  submitted on time.
- Launch: equal per admitted, reachable hotkey until a time, then the usual rules; recompute
  reproduces it.
"""

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

from cadmind_einstein import onboarding
from cadmind_einstein.desk import ADMISSION_BRIEF, JOB_DEADLINE_S, Desk, DeskError
from cadmind_einstein.fake_miner import _not_registered_yet, ask_key
from cadmind_einstein.scoring import (
    HOLD_RULE,
    HOUR_MS,
    JobOutcome,
    Ledger,
    recompute,
    report_order,
    score_all,
    standing,
    vector,
)
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
        return self.desk.agent_key(hk, **kw)

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
        self.assertNotIn("key_id", got)
        self.assertIn("approval", got["message"])
        self.assertEqual(self.p.looked_up, [])  # the platform is not asked
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

    def test_once_approved_the_same_call_looks_the_key_up(self) -> None:
        self.key(A)
        self.desk.approve(A)
        self.assertEqual(self.desk.agent_key(A), {"key_id": f"k-{A[:6]}"})
        self.assertEqual(self.p.looked_up, [A])
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

    def test_a_refusal_answers_its_reason_and_looks_nothing_up(self) -> None:
        self.key(A)
        with self.assertRaises(DeskError):
            self.desk.refuse(A, "   ")  # a refusal says why
        self.desk.refuse(A, "no contact given")
        with self.assertRaises(DeskError) as e:
            self.key(A)
        self.assertEqual(e.exception.status, 403)
        self.assertIn("no contact given", str(e.exception))
        self.assertEqual(self.p.looked_up, [])
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
        self.assertEqual(sorted(self.p.closed), sorted([A, B]))  # closed on the platform
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
        self.assertEqual(d.agent_key(B)["message"], "Ask on the official channel.")
        d.stop()

    def test_a_pending_request_of_a_hotkey_that_left_is_dropped(self) -> None:
        self.key(A)
        self.key(B)
        self.desk.refuse(B, "no")
        self.reg = [C]
        self.desk.sync_registered(self.reg)
        self.assertEqual([r["hotkey"] for r in self.desk.key_requests("all")], [B])


class TestWaitlist(_Case):
    """The waitlist (0.6.10, owner 2026-11-13): first come, first served, one place per coldkey; a
    duplicate holds no place and everyone behind it moves up."""

    def setUp(self) -> None:
        super().setUp()
        self.more = [Keypair.create_from_uri(f"//Wait{i}").ss58_address for i in range(4)]
        self.reg += self.more
        self.cold.update({h: f"cold-w{i}" for i, h in enumerate(self.more)})

    def ask(self, hk: str, **kw: Any) -> dict[str, Any]:
        self.clock.t += 60
        return self.key(hk, **kw)

    def places(self) -> dict[str, Any]:
        return {r["hotkey"]: r["waitlist"] for r in self.desk.key_requests()}

    def test_first_come_first_served_and_the_place_is_in_the_pending_answer(self) -> None:
        self.assertEqual(self.ask(A)["waitlist"], {"position": 1, "of": 1, "coldkey_known": True})
        self.assertEqual(self.ask(B)["waitlist"]["position"], 2)
        self.assertEqual(self.ask(C)["waitlist"], {"position": 3, "of": 3, "coldkey_known": True})
        self.assertEqual(self.ask(A)["waitlist"], {"position": 1, "of": 3, "coldkey_known": True})
        self.assertEqual([p["position"] for p in self.places().values()], [1, 2, 3])

    def test_a_duplicate_coldkey_holds_no_place_and_nobody_moves_back(self) -> None:
        w0, w1 = self.more[:2]
        self.cold[B] = "cold-a"  # B is A's owner's second hotkey
        self.ask(A)
        self.ask(B)
        got = self.ask(w0)
        self.assertEqual(got["waitlist"], {"position": 2, "of": 2, "coldkey_known": True})
        dup = self.places()[B]
        self.assertEqual(
            dup, {"position": None, "of": 2, "duplicate_of": A, "duplicate_kind": "waiting"}
        )
        # a later read that finds the duplicate moves everyone behind it up
        self.ask(w1)
        self.assertEqual(self.places()[w1]["position"], 3)
        self.cold[w0] = "cold-a"
        self.assertEqual(self.places()[w1], {"position": 2, "of": 2, "coldkey_known": True})
        self.assertEqual(self.places()[w0]["duplicate_of"], A)

    def test_the_public_network_lists_only_uids_and_places(self) -> None:
        from cadmind_einstein.stats import MinerStats

        w0 = self.more[0]
        self.cold[B] = "cold-a"  # a duplicate: no place, not listed
        for hk in (A, B, w0):
            self.ask(hk)
        uids = {A: 7, B: 8, w0: 9}
        stats = MinerStats(self.desk)
        stats.uids = lambda: uids  # type: ignore[method-assign]
        self.assertEqual(stats.network()["waitlist"], [{"uid": 7, "position": 1}, {"uid": 9, "position": 2}])

    def test_an_approval_or_refusal_closes_the_gap(self) -> None:
        self.cold[B] = "cold-a"
        for hk in (A, B, C, *self.more[:2]):
            self.ask(hk)
        self.desk.approve(A)
        line = self.places()
        self.assertEqual(
            line[B], {"position": None, "of": 3, "duplicate_of": A, "duplicate_kind": "approved"}
        )
        self.assertEqual([line[h]["position"] for h in (C, *self.more[:2])], [1, 2, 3])
        self.desk.refuse(C, "no contact")
        self.assertEqual([self.places()[h]["position"] for h in self.more[:2]], [1, 2])
        # a refused coldkey does not hold a place: its other hotkey waits like anyone
        self.cold[self.more[2]] = "cold-c"
        self.assertEqual(self.ask(self.more[2])["waitlist"]["position"], 3)

    def test_an_admitted_miner_holds_its_coldkeys_place(self) -> None:
        self.onboard(A)
        self.cold[B] = "cold-a"
        got = self.ask(B)
        self.assertEqual(got["waitlist"]["duplicate_of"], A)
        self.assertEqual(got["waitlist"]["duplicate_kind"], "approved")
        self.assertEqual(self.ask(C)["waitlist"], {"position": 1, "of": 1, "coldkey_known": True})

    def test_an_unknown_coldkey_keeps_its_own_place(self) -> None:
        del self.cold[B]
        self.ask(A)
        got = self.ask(B)
        self.assertEqual(got["waitlist"], {"position": 2, "of": 2, "coldkey_known": False})
        self.cold[B] = "cold-a"  # read later: a duplicate after all
        self.assertEqual(self.places()[B]["duplicate_of"], A)
        self.assertEqual(self.places()[A]["of"], 1)

    def test_a_new_owner_of_an_approved_hotkey_goes_to_the_end(self) -> None:
        self.desk.approve(A)
        self.key(A)
        self.ask(B)
        self.cold[A] = "cold-other"
        got = self.ask(A)
        self.assertEqual(got["waitlist"], {"position": 2, "of": 2, "coldkey_known": True})

    def test_the_miner_sees_its_own_place_and_never_the_notes(self) -> None:
        self.ask(A)
        self.ask(B, note="discord: bob")
        standing = self.desk.key_standing([B, C])
        self.assertIsNone(standing[C])
        self.assertEqual(standing[B]["state"], "pending")
        self.assertEqual(standing[B]["waitlist"]["position"], 2)
        self.assertNotIn("note", standing[B])
        self.assertNotIn("ip", standing[B])

    def test_the_platforms_miner_stats_carry_the_place(self) -> None:
        from cadmind_einstein.stats import MinerStats

        self.ask(A)
        self.ask(B, note="discord: bob")
        docs = MinerStats(self.desk).docs([B, C])["miners"]
        self.assertEqual(docs[B]["key_request"]["waitlist"]["position"], 2)
        self.assertIsNone(docs[C]["key_request"])
        self.assertNotIn("discord", json.dumps(docs))

    def test_auto_approval_has_no_waitlist(self) -> None:
        self.desk.stop()
        self.approval = "auto"
        self.desk = self.make()
        self.assertEqual(self.desk._waitlist(), {})

    def test_the_place_survives_a_restart(self) -> None:
        self.ask(A)
        self.ask(B)
        self.desk.stop()
        self.desk = self.make()
        self.assertEqual(self.places()[B]["position"], 2)


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

    def test_the_entry_test_is_a_no_questions_job_with_the_one_deadline(self) -> None:
        self.desk.approve(A)
        self.key(A)
        adm = self.desk.admission(A)
        self.assertTrue(self.desk.wait_idle())
        self.assertNotIn("effort", adm)
        w = self.desk.work(A)
        self.assertIs(w["no_questions"], True)  # the miner is told to decide every question
        self.assertEqual(w["brief"], ADMISSION_BRIEF)
        acc = self.desk.accept(A, w["job"])
        self.assertEqual(acc["deadline_ms"], self.desk.now_ms() + JOB_DEADLINE_S * 1000)
        self.assertEqual(JOB_DEADLINE_S, 240 * 60)

    def test_the_deadline_is_a_setting(self) -> None:
        d = Desk(self.p, Ledger(), registered=lambda: [A], deadline_s=90 * 60)
        self.assertEqual(d.deadline_s, 90 * 60)
        d.stop()
        with self.assertRaises(ValueError):
            Desk(self.p, Ledger(), registered=lambda: [A], deadline_s=0)

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
        self.clock.t += JOB_DEADLINE_S + 60
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
        self.assertIn(holder, self.p.closed)  # its open job workspaces frozen on the platform
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

    def test_release_takes_a_client_job_back_and_another_miner_gets_a_whole_deadline(self) -> None:
        """Owner 2026-11-13: the operator takes a job back from a miner that cannot work it; the miner keeps its key."""
        self.desk.work(A)
        self.desk.work(B)
        self.p.queue.append({"job": "r2", "brief": "A wall hook."})
        self.tick()
        holder = next(h for h in (A, B) if self.desk.work(h)["job"] == "r2")
        other = B if holder == A else A
        self.desk.accept(holder, "r2")
        with self.assertRaises(DeskError):
            self.desk.release("r2", "  ")
        self.clock.t += 7_000  # nearly two hours of its deadline spent
        out = self.desk.release("r2", "its miner cannot get its key")
        self.assertEqual((out["job"], out["released_from"]), ("r2", holder))
        self.assertTrue(self.desk.wait_idle())
        st = self.desk.status("r2")
        self.assertEqual(st["state"], "queued")
        self.assertEqual(st["attempts"][-1]["kind"], "released")
        self.assertEqual(self.ledger.outcomes, [])  # nobody scored
        m = self.desk.miners[holder]
        self.assertTrue(m.admitted and m.key_id)  # keeps its key and admission
        self.assertEqual(self.desk.work(holder).get("cancelled"), "r2")  # told to stop
        self.desk.work(other)
        self.tick()
        self.assertEqual(self.desk.work(other)["job"], "r2")
        self.desk.accept(other, "r2")
        a = self.desk.jobs["r2"].current
        self.assertEqual(a.deadline_ms - self.desk.now_ms(), self.desk.deadline_s * 1000)
        with self.assertRaises(DeskError) as e:
            self.desk.release("nope", "x")
        self.assertEqual(e.exception.status, 404)

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
        job=job, hotkey=hk, ended_ms=ended_ms, deliverable=True, kind="pass", wall_s=1
    )


UIDS: dict[str, int] = {OWNER: 0, A: 1, B: 2, C: 3}


def _weight(*hks: str) -> dict[str, float]:
    """The weight of one passing job each (0.5, unrated), an hour old at hour 10."""
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


class TestHold(_Case):
    """The operator's hold (``--hold``): burn every hour whoever is admitted; give out no work."""

    def report(self, hold: bool, d: str) -> dict[str, Any]:
        ledger = Ledger(report_dir=Path(d))
        return ledger.report(
            self.desk.now_ms(),
            {OWNER: 0, A: 1, B: 2, C: 3},
            owner_hotkey=OWNER,
            admitted=self.desk.admitted(),
            reachable=self.desk.reachable(),
            genesis="coldkey",
            launch_equal=True,
            hold=hold,
        )

    def test_on_hold_no_work_is_given_out_and_nobody_is_weighted(self) -> None:
        self.onboard(A)
        self.assertEqual(self.desk.admitted(), [A])
        self.desk.hold = True
        self.desk.work(A)
        self.p.queue.append({"job": "c1", "brief": "A wall hook."})
        self.tick()
        self.assertEqual([j["job"] for j in self.p.queue], ["c1"])  # left in the platform's queue
        self.assertEqual(self.desk.work(A)["job"], None)
        self.assertEqual(self.desk.capacity()["active_miners"], 0)
        # a newly approved miner gets no practice job; its approval stays
        self.desk.approve(B)
        self.assertIn("key_id", self.key(B))
        with self.assertRaises(DeskError) as caught:
            self.desk.practice(B)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "hold"))
        with tempfile.TemporaryDirectory() as d:
            rep = self.report(True, d)
            self.assertEqual((rep["vector"], rep["rule"], rep["hold"]), ([], HOLD_RULE, True))
            published = json.loads(sorted(Path(d).glob("hour-*.json"))[-1].read_text())
            again = recompute(published)
            self.assertTrue(again["matches_published"])
            self.assertEqual(again["rule"], HOLD_RULE)
            self.assertEqual(
                self.report(False, d)["vector"], [[1, 65_535]]
            )  # lifted: the launch rule
        # lifted: the job goes to the admitted miner
        self.desk.hold = False
        self.tick()
        self.assertEqual(self.desk.work(A)["job"], "c1")

    def test_on_hold_the_entry_test_is_still_given_and_admits(self) -> None:
        """Owner 2026-11-12: miners take the test during the hold, ready for the day it opens."""
        self.desk.hold = True
        self.desk.approve(B)
        self.key(B)
        self.pass_test(B)
        self.assertEqual(self.desk.admitted(), [B])
        self.p.queue.append({"job": "c1", "brief": "A wall hook."})
        self.desk.work(B)
        self.tick()
        self.assertEqual(self.desk.work(B)["job"], None)  # admitted, but no client job on hold
        self.assertEqual([j["job"] for j in self.p.queue], ["c1"])

    def test_an_entry_test_handed_out_before_the_hold_runs_to_its_end(self) -> None:
        self.desk.approve(C)
        self.key(C)
        adm = self.desk.admission(C)
        self.desk.hold = True
        self.assertEqual(self.desk.admission(C)["job"], adm["job"])  # its own test, not a refusal
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(C)
        self.assertEqual(w["job"], adm["job"])
        self.desk.accept(C, w["job"])
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(C, w["job"], w["workspace"], "d")
        wait_state(self.desk, w["job"], ("done",))
        self.assertEqual(
            self.desk.admitted(), [C]
        )  # admitted; still no work and no weight while held


class TestOperatorAdmit(_Case):
    """Owner 2026-11-12: an entry test failed by our own fault; the operator admits by hand."""

    def test_the_entry_test_workspace_says_no_questions(self) -> None:
        self.desk.approve(A)
        self.key(A)
        self.desk.admission(A)
        self.assertTrue(self.desk.wait_idle())
        self.assertTrue(self.p.grants[-1]["no_questions"])

    def test_an_approved_keyed_hotkey_is_admitted_and_its_failures_taken_back(self) -> None:
        with self.assertRaises(DeskError):
            self.desk.admit(A, "")  # says why
        with self.assertRaises(DeskError) as caught:
            self.desk.admit(A, "our fault")  # never approved
        self.assertEqual(caught.exception.status, 409)
        self.desk.approve(A)
        with self.assertRaises(DeskError):
            self.desk.admit(A, "our fault")  # approved, but no key yet
        self.key(A)
        self.desk.admission_failed[A] = 1
        out = self.desk.admit(A, "the entry-test workspace did not say no-questions")
        self.assertEqual(
            out, {"hotkey": A, "admitted": True, "was_admitted": False, "failures_taken_back": 1}
        )
        self.assertEqual(self.desk.admitted(), [A])
        self.desk.stop()
        self.desk = self.make()
        self.assertEqual(self.desk.admitted(), [A])  # persisted
        self.assertEqual(self.desk.admission_failed.get(A, 0), 0)
        self.assertEqual(self.desk.admission(A), {"admitted": True})  # no test asked of it


class TestLaunchSwitch(_Case):
    def test_the_switch_is_persisted_and_changed_at_runtime(self) -> None:
        self.assertFalse(self.desk.launch_equal)
        self.assertEqual(self.desk.set_launch_equal(True), {"launch_equal": True, "was": False})
        self.desk.stop()
        self.desk = self.make()
        self.assertTrue(self.desk.launch_equal)  # persisted
        self.assertTrue(self.desk.launch_saved)
        self.desk.set_launch_equal(False)
        self.desk.stop()
        self.desk = self.make()
        self.assertFalse(self.desk.launch_equal)

    def test_a_fresh_state_has_no_saved_switch(self) -> None:
        self.assertFalse(self.desk.launch_saved or self.desk.launch_equal)

    def test_the_startup_flag_sets_and_saves_and_its_absence_keeps_the_saved_value(self) -> None:
        from cadmind_einstein.daemon import apply_launch_flag

        self.assertEqual(apply_launch_flag(self.desk, None), "default")
        self.assertFalse(self.desk.launch_equal)
        self.assertEqual(apply_launch_flag(self.desk, "on"), "flag")
        self.desk.stop()
        self.desk = self.make()  # a deploy regenerated the unit, without the flag
        self.assertEqual(apply_launch_flag(self.desk, None), "saved")
        self.assertTrue(self.desk.launch_equal)  # kept, never switched off silently
        self.desk.set_launch_equal(False)  # the operator's route
        self.desk.stop()
        self.desk = self.make()
        self.assertEqual(apply_launch_flag(self.desk, None), "saved")
        self.assertFalse(self.desk.launch_equal)
        self.assertEqual(apply_launch_flag(self.desk, "on"), "flag")
        self.assertTrue(self.desk.launch_equal)


class TestAskKey(unittest.TestCase):
    def test_the_miner_waits_while_pending_and_takes_the_key_once_approved(self) -> None:
        answers = [
            (202, {"pending": True, "message": "wait", "retry_after_s": 900}),
            (202, {"pending": True, "message": "wait", "retry_after_s": 900}),
            (200, {"key_id": "k"}),
        ]
        bodies: list[dict[str, Any]] = []
        slept: list[float] = []

        def call(path: str, body: dict[str, Any]) -> tuple[int, Any]:
            self.assertEqual(path, "/v1/miners/agent_key")
            bodies.append(body)
            return answers.pop(0)

        code, got = ask_key(call, contact="me", log=lambda _m: None, sleep=slept.append)
        self.assertEqual((code, got["key_id"]), (200, "k"))
        self.assertEqual(slept, [900.0, 900.0])
        self.assertEqual(bodies[0]["contact"], "me")

    def test_a_hotkey_the_platform_has_no_key_for_waits_and_reads_what_to_do(self) -> None:
        answers = [
            (409, {"error": "link the hotkey and create a key", "code": "miner_not_linked"}),
            (409, {"error": "link the hotkey and create a key", "code": "miner_not_linked"}),
            (200, {"key_id": "k"}),
        ]
        slept: list[float] = []
        said: list[str] = []
        code, got = ask_key(lambda _p, _b: answers.pop(0), log=said.append, sleep=slept.append)
        self.assertEqual((code, got["key_id"]), (200, "k"))
        self.assertEqual(slept, [900.0, 900.0])  # never a fast loop
        self.assertEqual(sum("link the hotkey and create a key" in m for m in said), 2)

    def test_too_many_asks_wait_never_exit(self) -> None:
        """v0.6.6: a 429 is waited out (a miner its supervisor restarted in a loop flooded the
        validator with 1,600 key requests in two hours)."""
        answers = [
            (429, {"error": "too many agent key requests: retry later"}),
            (429, {"error": "too many", "retry_after_s": 120}),
            (200, {"key_id": "k"}),
        ]
        slept: list[float] = []
        said: list[str] = []
        code, got = ask_key(lambda _p, _b: answers.pop(0), log=said.append, sleep=slept.append)
        self.assertEqual((code, got["key_id"]), (200, "k"))
        self.assertEqual(slept, [600.0, 120.0])
        self.assertEqual(sum("slow down" in m for m in said), 1)

    def test_another_409_ends_the_ask(self) -> None:
        code, _ = ask_key(
            lambda _p, _b: (409, {"error": "x"}), log=lambda _m: None, sleep=lambda _s: None
        )
        self.assertEqual(code, 409)

    def test_a_bounded_wait_gives_up_pending(self) -> None:
        def call(path: str, body: dict[str, Any]) -> tuple[int, Any]:
            return 202, {"pending": True, "retry_after_s": 900}

        code, _ = ask_key(call, log=lambda _m: None, sleep=lambda _s: None, max_wait_s=60)
        self.assertEqual(code, 202)


class TestAskKeyNewcomer(unittest.TestCase):
    """Onboarding test 2026-11-09: the miner quit on the early 403 and was silent while pending."""

    def test_a_registration_the_validator_has_not_read_yet_is_waited_for_not_fatal(self) -> None:
        answers = [
            (403, {"error": "hotkey is not registered on the subnet"}),
            (403, {"error": "hotkey is not registered on the subnet"}),
            (202, {"pending": True, "message": "wait", "retry_after_s": 900}),
            (200, {"key_id": "k"}),
        ]
        slept: list[float] = []
        said: list[str] = []
        code, got = ask_key(lambda _p, _b: answers.pop(0), log=said.append, sleep=slept.append)
        self.assertEqual((code, got["key_id"]), (200, "k"))
        self.assertEqual(slept, [60.0, 60.0, 900.0])
        self.assertEqual(sum("does not see this hotkey registered yet" in m for m in said), 1)

    def test_any_other_refusal_still_ends_the_ask(self) -> None:
        code, got = ask_key(
            lambda _p, _b: (403, {"error": "refused by the subnet team"}),
            log=lambda _m: None,
            sleep=lambda _s: None,
        )
        self.assertEqual((code, got["error"]), (403, "refused by the subnet team"))

    def test_every_pending_ask_is_logged_so_the_miner_is_seen_alive(self) -> None:
        answers = [(202, {"pending": True, "message": "write to us", "retry_after_s": 900})] * 3 + [
            (200, {"key_id": "k"})
        ]
        said: list[str] = []
        ask_key(lambda _p, _b: answers.pop(0), log=said.append, sleep=lambda _s: None)
        self.assertEqual(
            sum(m.startswith("agent key: pending approval -- write to us") for m in said), 1
        )
        self.assertEqual(sum(m.startswith("agent key: still pending at ") for m in said), 3)

    def test_the_place_on_the_waitlist_is_logged(self) -> None:
        answers = [
            (202, {"pending": True, "waitlist": {"position": 3, "of": 7}}),
            (202, {"pending": True, "waitlist": {"position": None, "of": 6, "duplicate_of": "5X"}}),
            (202, {"pending": True}),  # an older validator names no place
            (200, {"key_id": "k"}),
        ]
        said: list[str] = []
        ask_key(lambda _p, _b: answers.pop(0), log=said.append, sleep=lambda _s: None)
        self.assertIn("agent key: on the waitlist: #3 of 7", said)
        self.assertEqual(
            sum("already holds its one place (hotkey 5X is waiting)" in m for m in said), 1
        )
        self.assertEqual(sum("waitlist" in m for m in said), 2)


class TestUnblock(_Case):
    """A wrong disable is lifted by the operator, not by editing the state with the daemon down."""

    def strike(self, hk: str) -> None:
        with self.desk._mut():
            self.desk.copied[hk] = self.desk.copied.get(hk, 0) + 2
        acted = self.desk.enforce()
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual([a["hotkey"] for a in acted], [hk])

    def test_unblock_lifts_the_block_and_the_strike_and_the_counts_do_not_strike_again(
        self,
    ) -> None:
        self.onboard(A)
        self.strike(A)
        m = self.desk.miners[A]
        self.assertGreater(m.blocked_until_ms, self.desk.now_ms())
        self.assertFalse(m.admitted or m.key_id)
        self.assertIn(A, self.p.closed)  # its open job workspaces frozen on the platform
        with self.assertRaises(DeskError) as e:
            self.key(A)
        self.assertEqual(e.exception.status, 403)
        got = self.desk.unblock(A, "the copies were one job's own attempts", readmit=True)
        self.assertEqual((got["strikes"], got["readmitted"], got["keyed"]), (0, True, False))
        self.assertEqual(m.blocked_until_ms, 0)
        self.assertNotIn(A, self.desk.strikes)
        self.assertTrue(m.admitted)
        self.assertEqual(m.key_id, "")  # not keyed again: the miner asks as usual
        self.assertEqual(self.desk.enforce(), [])  # the same counts act once
        self.assertIn("key_id", self.key(A))
        self.assertIn(A, self.desk.admitted())

    def test_a_taken_back_strike_does_not_lengthen_the_next_block(self) -> None:
        self.onboard(A)
        self.strike(A)
        self.desk.unblock(A, "first", readmit=False)
        self.strike(A)
        self.assertEqual(self.desk.strikes[A], 1)
        self.desk.unblock(A, "again")
        self.assertNotIn(A, self.desk.strikes)
        self.strike(A)  # new counts: a real strike again, the first one's length
        until = self.desk.miners[A].blocked_until_ms - self.desk.now_ms()
        self.assertEqual(until, int(self.desk.abuse.disable_s * 1000))

    def test_without_readmit_it_takes_the_entry_test_again(self) -> None:
        self.onboard(A)
        self.strike(A)
        got = self.desk.unblock(A, "wrong", readmit=False)
        self.assertFalse(got["admitted"])
        self.assertNotIn(A, self.desk.admitted())

    def test_readmit_needs_a_passed_entry_test(self) -> None:
        self.desk.approve(A)
        self.key(A)
        self.desk.admission(A)  # asked, never passed
        self.assertTrue(self.desk.wait_idle())
        self.strike(A)
        with self.assertRaises(DeskError) as e:
            self.desk.unblock(A, "wrong", readmit=True)
        self.assertEqual(e.exception.status, 409)
        self.assertGreater(self.desk.miners[A].blocked_until_ms, 0)  # a refusal changes nothing
        self.assertEqual(self.desk.strikes[A], 1)

    def test_readmit_needs_the_approval_too(self) -> None:
        self.onboard(A)
        self.strike(A)
        self.desk.revoke(A, "gone")
        with self.assertRaises(DeskError):
            self.desk.unblock(A, "wrong", readmit=True)

    def test_a_block_from_before_the_flag_is_readmitted_on_its_passed_test(self) -> None:
        self.onboard(A)
        self.strike(A)
        self.desk.miners[A].admitted_when_blocked = False  # a state written by 0.5.15
        self.assertTrue(self.desk.unblock(A, "wrong", readmit=True)["readmitted"])

    def test_nothing_to_lift_and_a_reason_is_required(self) -> None:
        self.onboard(A)
        with self.assertRaises(DeskError) as e:
            self.desk.unblock(A, "nothing")
        self.assertEqual(e.exception.status, 409)
        self.strike(A)
        with self.assertRaises(DeskError):
            self.desk.unblock(A, " ")
        with self.assertRaises(DeskError):
            self.desk.unblock(OWNER, "x")

    def test_an_unblock_survives_a_restart(self) -> None:
        self.onboard(A)
        self.strike(A)
        self.desk.unblock(A, "wrong", readmit=True)
        self.desk.stop()
        self.desk = self.make()
        m = self.desk.miners[A]
        self.assertEqual((m.blocked_until_ms, m.admitted), (0, True))
        self.assertNotIn(A, self.desk.strikes)
        self.assertEqual(self.desk.enforce(), [])


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

    def ask(self, contact: str = "") -> tuple[int, dict[str, Any]]:
        alice = Keypair.create_from_uri("//Alice")
        body = json.dumps({"contact": contact}).encode()
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
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = onboarding.main(
                ["--validator", self.base, "--token-file", str(self.token_file), *args]
            )
        return code, out.getvalue(), err.getvalue()

    def test_a_hotkey_registered_just_now_is_told_when_to_retry(self) -> None:
        """Onboarding test 2026-11-10: a 403 just after registering read as a team refusal."""
        self.desk.registered = lambda: [B]  # the last metagraph read predates the registration
        code, got = self.ask()
        self.assertEqual(code, 403)
        self.assertIn("not registered on the subnet", got["error"])
        self.assertIn("every 5 minutes", got["error"])
        self.assertIn("if you registered just now, retry", got["error"])
        self.assertTrue(_not_registered_yet(got))  # the reference miner still waits, never quits
        slow = Desk(self.p, Ledger(), registered=lambda: [B], sync_s=600)
        with self.assertRaises(DeskError) as said:
            slow.agent_key(A)
        self.assertEqual(said.exception.status, 403)
        self.assertIn("every 10 minutes", str(said.exception))
        slow.stop()

    def test_pending_then_approved_over_http_and_the_cli(self) -> None:
        code, got = self.ask("forum: alice")
        self.assertEqual(code, 202)
        self.assertTrue(got["pending"])
        self.assertEqual(got["waitlist"], {"position": 1, "of": 1, "coldkey_known": False})
        rc, out, _ = self.cli("pending")
        self.assertEqual(rc, 0)
        listed = json.loads(out)
        self.assertEqual(listed["key_approval"], "manual")
        self.assertEqual([r["hotkey"] for r in listed["requests"]], [A])
        self.assertEqual(listed["requests"][0]["note"], "forum: alice")
        self.assertEqual(listed["requests"][0]["ip"], "127.0.0.1")
        self.assertEqual(listed["requests"][0]["waitlist"]["position"], 1)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(self.cli("approve", A)[0], 0)
        code, got = self.ask()
        self.assertEqual((code, got), (200, {"key_id": f"k-{A[:6]}"}))
        rc, out, err = self.cli("revoke", A, "--reason", "testing")
        self.assertEqual(rc, 0)
        self.assertNotIn(TOKEN, out + err)
        self.assertIn(A, self.p.closed)
        code, got = self.ask()
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

    def test_unblock_over_the_cli(self) -> None:
        self.desk.disable(A, for_s=3600.0, reason="wrong")
        with self.desk._mut():
            self.desk.strikes[A] = 1
        rc, out, err = self.cli("unblock", A, "--reason", "a false copy")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual((got["hotkey"], got["strikes"], got["readmitted"]), (A, 0, False))
        self.assertEqual(self.desk.miners[A].blocked_until_ms, 0)
        rc, _, err = self.cli("unblock", A, "--reason", "again", "--readmit")
        self.assertEqual(rc, 1)
        self.assertIn("not disabled", err)
        req = urllib.request.Request(  # noqa: S310 -- loopback
            self.base + "/v1/operator/unblock",
            data=json.dumps({"hotkey": A, "reason": "x", "readmit": "yes"}).encode(),
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
            ("/v1/operator/unblock", json.dumps({"hotkey": A, "reason": "x"}).encode()),
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
