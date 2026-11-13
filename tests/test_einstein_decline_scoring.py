"""A right decline is neutral (0.6.9; a miner's report 2026-11-13: it declined three briefs that
said nothing to build, each still scored 0, and its router record sat at 0.35 against 0.50).

- A **confirmed decline** (the job ended declined: every substantive attempt a rejection, from
  ``second_opinion.coldkeys`` distinct coldkeys) voids its rejections: out of the records, the
  weights, the averages, the rejection telemetry and the abuse rule; published, recomputable.
- A rejection whose job is still open is a 0 and a rejection until the job ends.
- A rejection another owner's miner then delivered stays a 0 and counts as overturned.
- One owner's hotkeys running the fleet out of other owners confirm nothing.
- The operator's ``POST /v1/operator/void {job, reason}`` voids a job's rejections by hand, saved
  across a restart, idempotent, logged; deliveries are never touched.
"""

from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

import bittensor.http_auth as btauth

from cadmind_einstein import onboarding
from cadmind_einstein.desk import AbusePolicy, Desk, DeskError, SecondOpinion
from cadmind_einstein.scoring import (
    HOUR_MS,
    UNRATED,
    JobOutcome,
    Ledger,
    recompute,
    record,
    score_all,
    standing,
)
from cadmind_einstein.service import serve
from cadmind_einstein.stats import MinerStats
from cadmind_einstein.store import Store
from tests.test_einstein import GOOD, VALIDATOR, A, B, C, Clock, FakePlatform

TOKEN = "t" * 32
NOTHING = "@device_1 @device_2 @device_3 @device_4"


class TestTheRule(unittest.TestCase):
    def test_a_void_outcome_is_no_job_of_its_miner(self) -> None:
        now = 10 * HOUR_MS
        jobs = score_all(
            [
                JobOutcome(job="j1", hotkey=A, ended_ms=now, kind="reject", void="declined"),
                JobOutcome(job="j2", hotkey=A, ended_ms=now, kind="reject"),
                JobOutcome(job="j3", hotkey=B, ended_ms=now, kind="reject", void="operator: x"),
            ]
        )
        self.assertEqual([o.s for o in jobs], [0.0, 0.0, 0.0])  # never points
        self.assertEqual(jobs[0].parts, {"failed": "reject", "void": "declined"})
        # A: one counted job (a 0) padded with nine unrated; B: none, so unrated
        self.assertEqual(record(jobs, now), {A: round(9 * UNRATED / 10, 6)})
        weight, average = standing(jobs, now)
        self.assertEqual((weight, average), ({A: 0.0}, {A: 0.0}))

    def test_three_void_rejections_leave_the_record_unrated(self) -> None:
        now = 10 * HOUR_MS
        rejects = [JobOutcome(job=f"j{i}", hotkey=A, ended_ms=now, kind="reject") for i in range(3)]
        delivered = [
            JobOutcome(job=f"d{i}", hotkey=A, ended_ms=now, kind="pass", deliverable=True)
            for i in range(5)
        ]
        jobs = score_all(rejects + delivered)
        self.assertEqual(record(jobs, now)[A], round((5 * 0.5 + 2 * 0.5) / 10, 6))  # 0.35
        for o in rejects:
            o.void = "operator: the briefs said nothing to build"
        self.assertEqual(record(score_all(jobs), now)[A], UNRATED)  # 0.50

    def test_a_report_publishes_the_void_and_recompute_agrees(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(report_dir=Path(d))
            now = 10 * HOUR_MS
            ledger.add(JobOutcome(job="j1", hotkey=A, ended_ms=now, kind="reject"))
            ledger.add(JobOutcome(job="j2", hotkey=A, ended_ms=now, kind="pass", deliverable=True))
            self.assertEqual([o.hotkey for o in ledger.void("j1", "declined")], [A])
            self.assertEqual(ledger.void("j1", "again"), [])  # the first reason stays
            rep = ledger.report(now, {A: 1}, owner_hotkey="owner", admitted=[A], reachable=[A])
            self.assertEqual(rep["average"], {A: 0.5})  # the void rejection is no job
            row = next(r for r in rep["jobs"] if r["job"] == "j1")
            self.assertEqual(row["void"], "declined")
            again = recompute(json.loads((Path(d) / f"hour-{rep['hour']}.json").read_text()))
            self.assertTrue(again["matches_published"])
            self.assertEqual(again["mismatches"], [])


class _Rig(unittest.TestCase):
    """Three miners of three owners (unless ``cold`` says otherwise), on a store."""

    cold: dict[str, str] = {A: "owner-a", B: "owner-b", C: "owner-c"}  # noqa: RUF012
    hotkeys: tuple[str, ...] = (A, B, C)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p, self.clock = FakePlatform(), Clock()
        self.logs: list[str] = []
        self.boot()

    def boot(self, **kw: Any) -> Desk:
        self.store = Store(Path(self.tmp.name) / "s.sqlite3")
        self.ledger = Ledger(store=self.store)
        self.desk = Desk(
            self.p,
            self.ledger,
            registered=lambda: list(self.hotkeys),
            clock=self.clock,
            rng=random.Random(7),  # noqa: S311 -- a test's seed
            coldkeys=lambda: self.cold,
            second_opinion=kw.pop("second_opinion", SecondOpinion(wait_s=0.0)),
            abuse=kw.pop("abuse", AbusePolicy(rejections=False)),
            store=self.store,
            log=self.logs.append,
            max_inflight=8,
            **kw,
        )
        self.addCleanup(self.desk.stop)
        for hk in self.hotkeys:
            if not self.desk.miners.get(hk) or not self.desk.miners[hk].key_id:
                self.desk.agent_key(hk)
            self.desk.miners[hk].admitted = True
        return self.desk

    def restart(self) -> Desk:
        self.desk.stop()
        self.store.close()
        return self.boot()

    def only(self, *free: str) -> None:
        for hk in self.hotkeys:
            self.desk.miners[hk].blocked_until_ms = 0 if hk in free else self.desk.now_ms() + 10**9
            self.desk.work(hk)

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def take(self, job: str, hk: str, *, new: bool = True) -> dict[str, Any]:
        if new:
            self.p.queue.append({"job": job, "brief": NOTHING})
        self.only(hk)
        self.tick()
        w = self.desk.work(hk)
        self.assertEqual(w.get("job"), job)
        return w

    def reject(self, job: str, hk: str, *, new: bool = True) -> None:
        self.take(job, hk, new=new)
        self.desk.reject(hk, job, "not-capable", "The brief names projects but no device.")
        self.assertTrue(self.desk.wait_idle())

    def deliver(self, job: str, hk: str) -> None:
        w = self.take(job, hk, new=False)
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, job, w["workspace"], "sha256:x")
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status(job)["result"]["label"], "delivered")

    def of(self, job: str) -> list[JobOutcome]:
        return sorted((o for o in self.ledger.outcomes if o.job == job), key=lambda o: o.ended_ms)


class TestConfirmedDeclines(_Rig):
    def test_two_owners_declining_void_both_rejections(self) -> None:
        self.reject("j1", A)
        # pending: the job is open, so the rejection is a 0 and a rejection for now
        self.assertEqual(self.desk.status("j1")["state"], "queued")
        (o,) = self.of("j1")
        self.assertEqual((o.s, o.void), (0.0, ""))
        self.assertEqual(self.ledger.records(self.desk.now_ms())[A], 0.45)
        self.assertEqual(self.desk.telemetry()[A]["rejected"], 1)
        self.clock.t += 60
        self.reject("j1", B, new=False)
        self.assertEqual(self.desk.status("j1")["result"]["label"], "declined")
        self.assertEqual([x.void for x in self.of("j1")], ["declined", "declined"])
        self.assertEqual(self.ledger.records(self.desk.now_ms()), {})  # both unrated again
        tel = self.desk.telemetry()
        self.assertEqual((tel[A]["rejected"], tel[B]["rejected"]), (0, 0))
        self.assertEqual((tel[A]["assigned"], tel[B]["assigned"]), (1, 1))  # still assigned
        # the miner's own stats do not count it as a rejection either
        doc = MinerStats(self.desk).docs([A])["miners"][A]
        self.assertEqual(doc["windows"]["24h"]["rejected"], 0)
        self.assertEqual(doc["windows"]["lifetime"]["rejected"], 0)
        # and a restart keeps it so
        self.restart()
        self.assertEqual([x.void for x in self.of("j1")], ["declined", "declined"])
        self.assertEqual(self.desk.telemetry()[A]["rejected"], 0)
        self.assertEqual(self.ledger.records(self.desk.now_ms()), {})

    def test_confirmed_declines_never_reach_the_abuse_rule(self) -> None:
        self.desk.abuse = AbusePolicy(rejections_min=2, rejection_ratio=0.0, overturned=0)
        for i in range(3):
            self.reject(f"j{i}", A)
            self.reject(f"j{i}", B, new=False)
            self.assertEqual(self.desk.status(f"j{i}")["result"]["label"], "declined")
            self.clock.t += 60
        self.assertEqual(self.desk.enforce(), [])
        # the same three handed back on jobs another miner then could do: shedding work, struck
        for i in range(3):
            self.reject(f"k{i}", A)
            self.deliver(f"k{i}", C)
            self.clock.t += 60
        acted = self.desk.enforce()
        self.assertEqual([x["hotkey"] for x in acted], [A])
        self.assertIn("rejections: 3 of 6", acted[0]["reason"])

    def test_a_decline_another_owner_delivered_stays_zero_and_overturned(self) -> None:
        self.reject("j1", A)
        self.deliver("j1", C)
        (rej, _) = self.of("j1")
        self.assertEqual((rej.kind, rej.s, rej.void), ("reject", 0.0, ""))
        self.assertEqual(self.desk.overturned.get(A), 1)
        self.assertEqual(self.desk.telemetry()[A]["rejected"], 1)

    def test_a_delivery_still_scores_as_before(self) -> None:
        self.reject("j1", A)
        self.deliver("j1", C)
        self.assertEqual(self.of("j1")[-1].s, UNRATED)
        self.assertEqual(self.ledger.records(self.desk.now_ms())[C], UNRATED)


class TestOneOwnerConfirmsNothing(_Rig):
    cold = {A: "owner", B: "owner"}  # noqa: RUF012
    hotkeys = (A, B)

    def test_one_owners_hotkeys_running_the_fleet_out_keep_their_zeros(self) -> None:
        self.desk.second_opinion = SecondOpinion(coldkeys=2, wait_s=0.0)
        self.reject("j1", A)
        self.reject("j1", B, new=False)
        self.tick()
        self.assertEqual(self.desk.status("j1")["result"]["label"], "declined")  # exhausted
        self.assertEqual([x.void for x in self.of("j1")], ["", ""])
        self.assertEqual(self.desk.telemetry()[A]["rejected"], 1)


class TestOperatorVoid(_Rig):
    def test_the_operator_voids_an_open_or_overturned_jobs_rejections(self) -> None:
        self.reject("j1", A)
        self.deliver("j1", C)  # overturned: a 0 and an overturn
        self.reject("j2", A)  # still open
        rec = self.ledger.records(self.desk.now_ms())[A]
        self.assertEqual(rec, round(8 * UNRATED / 10, 6))
        with self.assertRaises(DeskError) as e:
            self.desk.void("j1", "   ")
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(DeskError) as e:
            self.desk.void("nope", "x")
        self.assertEqual(e.exception.status, 404)
        out = self.desk.void("j1", "the brief said nothing to build")
        self.assertEqual(([v["hotkey"] for v in out["voided"]], out["already_void"]), ([A], 0))
        self.assertTrue(any("rejections voided by the operator" in x for x in self.logs))
        self.assertEqual(self.desk.overturned.get(A), 0)
        self.assertEqual(self.desk.telemetry()[A]["rejected"], 1)  # j2's still counts
        self.desk.void("j2", "the brief said nothing to build")
        self.assertEqual(self.desk.telemetry()[A]["rejected"], 0)
        self.assertEqual(self.desk.void("j2", "twice")["already_void"], 1)
        self.assertEqual(self.desk.telemetry()[A]["rejected"], 0)  # voiding twice: nothing
        self.assertNotIn(A, self.ledger.records(self.desk.now_ms()))
        # the delivery is untouched
        self.assertEqual(
            [(o.kind, o.void) for o in self.of("j1")],
            [("reject", "operator: the brief said nothing to build"), ("pass", "")],
        )
        # j2 is still open: a later delivery by another owner overturns nothing voided
        self.deliver("j2", C)
        self.assertEqual(self.desk.overturned.get(A), 0)
        # a restart keeps every void and every count
        self.restart()
        self.assertEqual(
            {o.void for o in self.ledger.outcomes if o.kind == "reject"},
            {"operator: the brief said nothing to build"},
        )
        self.assertEqual((self.desk.rejected.get(A), self.desk.overturned.get(A)), (0, 0))

    def test_a_void_below_a_strike_never_reads_negative(self) -> None:
        self.desk.abuse = AbusePolicy(rejections_min=2, rejection_ratio=0.0, overturned=0)
        self.reject("j1", A)
        self.deliver("j1", C)
        self.reject("j2", A)
        self.assertEqual([x["hotkey"] for x in self.desk.enforce()], [A])
        self.desk.void("j1", "a brief with nothing to build")
        self.assertEqual(self.desk.abuse_seen[A]["rejected"], self.desk.rejected[A])
        self.assertEqual(self.desk.enforce(), [])


class TestTheRoute(_Rig):
    def test_post_operator_void_takes_the_bearer_token(self) -> None:
        self.reject("j1", A)
        server = serve(
            self.desk,
            "127.0.0.1",
            0,
            token=TOKEN,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/operator/void"
        body = json.dumps({"job": "j1", "reason": "nothing to build"}).encode()
        req = urllib.request.Request(url, data=body)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)  # noqa: S310
        self.assertEqual(e.exception.code, 401)
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            out = json.loads(r.read())
        self.assertEqual((out["job"], [v["hotkey"] for v in out["voided"]]), ("j1", [A]))

    def test_the_operator_cli_sends_it(self) -> None:
        tok = Path(self.tmp.name) / "token"
        tok.write_text(TOKEN)
        tok.chmod(0o600)
        with mock.patch.object(onboarding, "call", return_value=(200, {"ok": True})) as call:
            code = onboarding.main(
                ["--token-file", str(tok), "void", "e2c9eb59a084", "--reason", "no device named"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            call.call_args.args[2:],
            ("POST", "/v1/operator/void", {"job": "e2c9eb59a084", "reason": "no device named"}),
        )


if __name__ == "__main__":
    unittest.main()
