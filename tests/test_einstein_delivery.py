"""A delivered result's provenance: every grant names its job and every delivered result names the
validator's fork and the digest its gate read, so the platform can check the charge; a result
the platform refuses on provenance is a security ALERT, never sent again, its job's scores
withdrawn (the validator's side, not the miner's) and reported in the hour's ``watch``; a
delivery the platform cannot verify yet is sent again."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cadmind_einstein.desk import Desk, Job
from cadmind_einstein.platform import OperatorClient, PlatformError
from cadmind_einstein.scoring import Ledger
from cadmind_einstein.sealing import new_keypair
from cadmind_einstein.store import Store
from tests.test_einstein import GOOD, A, B, Clock, FakePlatform
from tests.test_einstein_operator_roles import _Stub


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.p, self.clock, self.logs = FakePlatform(), Clock(), []
        self.calls: list[dict[str, Any]] = []
        self.refuse: list[PlatformError] = []
        real = self.p.complete

        def complete(job: str, result: dict[str, Any]) -> None:
            self.calls.append(dict(result))
            if self.refuse:
                raise self.refuse.pop(0)
            real(job, result)

        self.p.complete = complete  # type: ignore[method-assign]
        self.granted: list[dict[str, Any]] = []
        grant = self.p.grant

        def named(hotkey: str, *, brief: str = "", seed: str = "", name: str = "job") -> str:
            self.granted.append({"hotkey": hotkey, "name": name})
            return grant(hotkey, brief=brief, seed=seed, name=name)

        self.p.grant = named  # type: ignore[method-assign]
        self.desk = Desk(
            self.p,
            Ledger(),
            registered=lambda: [A, B],
            clock=self.clock,
            rng=random.Random(2),  # noqa: S311 -- a test's seed
            log=self.logs.append,
        )
        self.addCleanup(self.desk.stop)
        for hk in (A, B):
            self.desk.agent_key(hk, new_keypair()[1])
            self.desk.miners[hk].admitted = True

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def holder(self, job: str) -> str:
        return next(h for h in (A, B) if self.desk.work(h)["job"] == job)

    def deliver(self, job: str, *, after_reject: bool = False) -> str:
        self.p.queue.append({"job": job, "brief": "A hook."})
        for hk in (A, B):
            self.desk.work(hk)
        self.tick()
        if after_reject:
            first = self.holder(job)
            self.desk.reject(first, job, "not-capable", "too hard")
            self.assertTrue(self.desk.wait_idle())
            for hk in (A, B):
                self.desk.work(hk)
            self.tick()
        hk = self.holder(job)
        w = self.desk.work(hk)
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, job, w["workspace"], "")
        self.assertTrue(self.desk.wait_idle())
        self.tick()
        return hk


class TestProvenanceIsSent(_Case):
    def test_every_grant_names_its_job_and_the_result_names_fork_and_gated_digest(self) -> None:
        self.deliver("j1")
        self.assertEqual([g["name"] for g in self.granted], ["j1"])
        res = self.p.completed["j1"]
        self.assertEqual(res["label"], "delivered")
        fork = res["workspace"]
        self.assertEqual(self.p.ws[fork]["hotkey"], "validator")  # the validator's own fork
        gated = self.p.run(fork, "status")["data"]["status"]["digest"]
        self.assertEqual(res["digest"], gated)
        self.assertTrue(res["digest"])

    def test_the_client_sends_the_job_with_every_grant(self) -> None:
        stub = _Stub()
        self.addCleanup(stub.close)
        seen: list[dict[str, Any]] = []
        import http.server

        class Body(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                return None

            def do_POST(self) -> None:
                seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = b'{"workspace": "ws-1"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        stub.server.RequestHandlerClass = Body
        OperatorClient(stub.url, "op").grant(A, brief="b", name="job-7")
        self.assertEqual(seen[0]["job"], "job-7")
        self.assertEqual(seen[0]["name"], "job-7")


class TestRefusedDelivery(_Case):
    def test_not_of_job_is_alerted_never_resent_and_withdrawn(self) -> None:
        self.refuse.append(PlatformError("not of this job", 409, "delivery_not_of_job"))
        hk = self.deliver("j1")
        for _ in range(4):
            self.tick()
        self.assertEqual(len(self.calls), 1)  # never sent again
        self.assertNotIn("j1", self.p.completed)
        st = self.desk.status("j1")["result"]
        self.assertEqual((st["label"], st["charge"]), ("delivery refused", False))
        self.assertEqual([o for o in self.desk.ledger.outcomes if o.job == "j1"], [])
        self.assertEqual(self.desk.delivered.get(hk, 0), 0)  # withdrawn: no genesis credit
        self.assertTrue(any(x.startswith("ALERT") and "j1" in x for x in self.logs))
        refusals = self.desk.watch({})["delivery_refusals"]
        self.assertEqual(
            [(r["job"], r["code"], r["hotkey"]) for r in refusals],
            [("j1", "delivery_not_of_job", hk)],
        )
        self.desk.clear_watch(0)
        self.assertEqual(self.desk.watch({})["delivery_refusals"], [])

    def test_the_refused_job_stays_leased_until_the_platform_lets_it_go(self) -> None:
        self.refuse.append(PlatformError("moved", 409, "delivery_digest_mismatch"))
        self.deliver("j1")
        self.desk.lease_every_s = 0
        leased: list[list[str]] = []
        real = self.p.lease

        def lease(jobs: list[str], working: list[str] | None = None) -> dict[str, Any]:
            leased.append(list(jobs))
            return real(jobs, working)

        self.p.lease = lease  # type: ignore[method-assign]
        self.desk._lease(self.desk.now_ms() + 1)
        self.assertIn("j1", leased[-1])
        self.p.lost.add("j1")
        self.desk._lease(self.desk.now_ms() + 2)
        self.assertTrue(self.desk.status("j1")["result"]["released"])
        n = len(leased)
        self.desk._lease(self.desk.now_ms() + 3)
        self.assertTrue(len(leased) == n or "j1" not in leased[-1])
        self.assertEqual(self.desk.status("j1")["result"]["label"], "delivery refused")

    def test_a_digest_mismatch_withdraws_the_overturned_rejection_too(self) -> None:
        self.refuse.append(PlatformError("moved", 409, "delivery_digest_mismatch"))
        self.deliver("j1", after_reject=True)
        self.assertEqual(self.desk.ledger.outcomes, [])  # the pass and the held 0 both withdrawn
        self.assertEqual(sum(self.desk.overturned.values()), 0)
        self.assertEqual(self.desk.status("j1")["result"]["refused"]["withdrawn"], 2)

    def test_unverified_is_sent_again(self) -> None:
        self.refuse.append(PlatformError("cannot read now", 503, "delivery_unverified", ours=True))
        self.deliver("j1")
        self.tick()
        self.assertEqual(len(self.calls), 1)  # backed off: not sent again before its delay
        self.clock.t += 10
        self.tick()
        self.assertEqual(len(self.calls), 2)  # refused once (503), sent again
        self.assertEqual(self.p.completed["j1"]["label"], "delivered")
        self.assertEqual(len(self.desk.ledger.outcomes), 1)


class TestClientCodes(unittest.TestCase):
    def test_refusals_are_plain_and_unverified_is_held(self) -> None:
        stub = _Stub()
        self.addCleanup(stub.close)
        t = [0.0]
        c = OperatorClient(stub.url, "op", clock=lambda: t[0])
        prefix = ("POST", "/api/engine/einstein/jobs/")
        stub.rules[prefix] = (409, {"error": "no", "code": "delivery_not_of_job"}, {})
        with self.assertRaises(PlatformError) as e:
            c.complete("j1", {"label": "delivered"})
        self.assertEqual(
            (e.exception.status, e.exception.code, e.exception.ours),
            (409, "delivery_not_of_job", False),
        )
        self.assertEqual(c.frozen(), [])
        stub.rules[prefix] = (503, {"code": "delivery_unverified"}, {"Retry-After": "3"})
        with self.assertRaises(PlatformError) as e:
            c.complete("j1", {"label": "delivered"})
        self.assertEqual(
            (e.exception.status, e.exception.ours, e.exception.held), (503, True, False)
        )
        stub.rules.clear()
        c.complete("j2", {"label": "delivered"})  # one job's 503 holds no other job back
        c.claim()
        stub.rules[prefix] = (503, {"error": "the server is busy", "code": "busy"}, {})
        with self.assertRaises(PlatformError) as e:
            c.complete("j1", {"label": "delivered"})
        self.assertTrue(e.exception.held)  # the platform's load holds the queue role
        stub.rules.clear()
        with self.assertRaises(PlatformError) as e:
            c.claim()
        self.assertEqual((e.exception.code, e.exception.held), ("backoff", True))
        t[0] += 6
        c.complete("j1", {"label": "delivered"})


class TestOneResultNeverStarvesTheOthers(unittest.TestCase):
    """A result the platform keeps refusing with a 503 of its own (it cannot export that one
    delivery) backs off alone: the other jobs' results reach the platform on the same tick, over
    the real client, and the refused one's back-off survives a restart."""

    def setUp(self) -> None:
        self.stub = _Stub()
        self.addCleanup(self.stub.close)
        self.clock, self.logs = Clock(), []
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(Path(self.dir.name) / "s.sqlite3")
        self.stub.rules[("POST", "/api/engine/einstein/jobs/jA/result")] = (
            503,
            {
                "error": "the Watt agent API did not answer; send the result again",
                "code": "agent_api_offline",
            },
            {"Retry-After": "5"},
        )

    def desk(self) -> Desk:
        client = OperatorClient(self.stub.url, "op", clock=self.clock, log=self.logs.append)
        desk = Desk(
            client,  # type: ignore[arg-type]
            Ledger(store=self.store),
            registered=list,
            clock=self.clock,
            store=self.store,
            log=self.logs.append,
        )
        self.addCleanup(desk.stop)
        return desk

    def sent(self) -> list[str]:
        return [p.split("/")[-2] for m, p, _ in self.stub.seen if p.endswith("/result")]

    def end(self, desk: Desk, job: str, label: str = "didn't finish") -> None:
        with desk._mut():
            desk.jobs[job] = Job(
                job=job,
                brief="",
                state="done",
                created_ms=desk.now_ms(),
                result={"label": label, "charge": False, "why": "test"},
            )
            desk._outbox.append(job)

    def test_a_refused_result_backs_off_alone(self) -> None:
        desk = self.desk()
        for job in ("jA", "jB", "jC"):  # the refused one first
            self.end(desk, job)
        desk._flush()
        self.assertEqual(sorted(self.sent()), ["jA", "jB", "jC"])  # B and C on the same tick
        self.assertEqual(desk._outbox, ["jA"])
        self.assertEqual(desk.platform.frozen(), [])
        # within its back-off, A is not sent again; a new result goes out at once
        self.end(desk, "jD")
        self.clock.t += 5
        desk._flush()
        self.assertEqual(self.sent()[3:], ["jD"])
        # past it, A is sent again -- after the fresh results -- and its delay doubles
        self.end(desk, "jE")
        self.clock.t += 6
        desk._flush()
        self.assertEqual(self.sent()[4:], ["jE", "jA"])
        self.assertEqual(desk._outbox_retry["jA"]["tries"], 2)
        self.assertEqual(desk._outbox_retry["jA"]["code"], "agent_api_offline")
        self.clock.t += 15
        desk._flush()
        self.assertEqual(self.sent()[6:], [])  # 20 s now
        self.assertFalse(any("ALERT" in x for x in self.logs))
        desk.stop()
        # a restart keeps the back-off
        again = self.desk()
        self.assertEqual(again._outbox, ["jA"])
        self.assertEqual(again._outbox_retry["jA"]["tries"], 2)
        again._flush()
        self.assertEqual(self.sent()[6:], [])
        # refused for a long time: one ALERT naming the job and the platform's words
        for _ in range(12):
            self.clock.t += 300
            again._flush()
        alerts = [x for x in self.logs if "ALERT" in x and "jA" in x]
        self.assertTrue(alerts)
        self.assertIn("did not answer", alerts[0])
        self.assertLessEqual(len(alerts), 2)  # 1 h of refusals, said at most every 30 min
        self.assertLessEqual(again._outbox_retry["jA"]["next_ms"] - again.now_ms(), 300_000)
        # the platform takes it at last: gone from the outbox and its back-off
        del self.stub.rules[("POST", "/api/engine/einstein/jobs/jA/result")]
        self.clock.t += 300
        again._flush()
        self.assertEqual(again._outbox, [])
        self.assertEqual(again._outbox_retry, {})

    def test_the_platforms_load_stops_the_walk_and_backs_off_only_the_job_it_answered(self) -> None:
        self.stub.rules.clear()
        self.stub.rules[("POST", "/api/engine/einstein/jobs/")] = (
            503,
            {"error": "the server is busy; try again", "code": "busy"},
            {"Retry-After": "5"},
        )
        desk = self.desk()
        for job in ("jA", "jB", "jC"):
            self.end(desk, job)
        desk._flush()
        self.assertEqual(len(self.sent()), 1)  # the role is held: nothing more sent this tick
        self.assertEqual(sorted(desk._outbox), ["jA", "jB", "jC"])
        self.assertEqual(set(desk._outbox_retry), {"jA"})  # only the one the platform answered
        self.stub.rules.clear()
        self.clock.t += 6
        desk._flush()
        self.assertEqual(desk._outbox, ["jA"])  # B and C at once, A after its own 10 s
        self.clock.t += 5
        desk._flush()
        self.assertEqual(desk._outbox, [])


if __name__ == "__main__":
    unittest.main()
