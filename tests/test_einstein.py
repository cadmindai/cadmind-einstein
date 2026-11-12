"""Einstein (one miner per job): the miner's own agent key, the per-job points,
7-day weights with no owner weight, the recompute auditor, and the desk -- routing by wait,
acceptance, the Watt gate, reassignment from scratch up to the cap, "didn't finish" not charged,
copies -- against an in-memory platform."""

from __future__ import annotations

import io
import json
import random
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import bittensor.http_auth as btauth
from bittensor.sp_core import Keypair

from cadmind_einstein.desk import GATE_RUN_S, UNCOUNTED, Desk, DeskError, _survivors
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.scoring import (
    HOUR_MS,
    JobOutcome,
    Ledger,
    outcome_of,
    recompute,
    score_all,
    standing,
    vector,
)
from cadmind_einstein.service import _host_refused_of, serve

VALIDATOR = Keypair.create_from_uri("//Validator")
A, B, C, D = (
    Keypair.create_from_uri(u).ss58_address for u in ("//Alice", "//Bob", "//Charlie", "//Dave")
)
OWNER = Keypair.create_from_uri("//Owner").ss58_address


def project_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel, text in files.items():
            z.writestr(f"ws/{rel}", text)
    return buf.getvalue()


GOOD = {
    "brief/brief.md": "A wall hook.",
    "spec/sources.toml": "[source.pla]\nurl = 'x'\n",
    "spec/requirements.toml": "[requirement.holds]\nstatement = 'holds a coat'\n",
    "tests/test_hook.py": "def test_holds():\n    pass\n",
    "src/parts/hook.py": "\n".join(f"line {i} of the hook" for i in range(40)),
}


NOT_LINKED_WORDS = (
    "No agent key names this hotkey: link it to your developer platform account and create an "
    "agent key naming it."
)


class FakePlatform:
    """The operator routes and the validator's own workspaces, in memory. A miner 'works' by setting
    its
    workspace's files and verdict."""

    def __init__(self) -> None:
        self.ws: dict[str, dict[str, Any]] = {}
        self.grants: list[dict[str, Any]] = []
        self.unlinked: set[str] = set()  # hotkeys linked to no account, or named by no live key
        self.looked_up: list[str] = []  # the hotkeys whose key the validator asked for
        self.closed: list[str] = []  # the hotkeys whose open job workspaces it had frozen
        self.n = 0
        self.queue: list[dict[str, Any]] = []  # the platform's single queue (einstein's jobs)
        self.leased: list[str] = []
        self.lost: set[str] = set()
        self.working: list[str] | None = None  # the last lease's builders (the platform's L3 field)
        self.completed: dict[str, dict[str, Any]] = {}
        self.suspended: set[str] = set()  # miners Watt's telemetry suspended (platform side)
        self.runs: list[tuple[str, str]] = []  # (workspace, command) of every watt run
        self.forks = 0
        self.deleted: list[str] = []
        self.cancelled_runs: list[str] = []

    def claim(self) -> dict[str, Any] | None:
        if not self.queue:
            return None
        job = self.queue.pop(0)
        self.leased.append(str(job["job"]))
        return job

    def lease(self, jobs: list[str], working: list[str] | None = None) -> dict[str, Any]:
        self.working = None if working is None else list(working)
        return {
            "renewed": [j for j in jobs if j not in self.lost],
            "lost": [j for j in jobs if j in self.lost],
        }

    def claimed(self) -> list[dict[str, Any]]:
        return [{"job": j, "brief": "A wall hook."} for j in self.leased if j not in self.completed]

    def complete(self, job: str, result: dict[str, Any]) -> None:
        self.completed[job] = result

    def _new(self, **kw: Any) -> str:
        self.n += 1
        name = f"ag-{self.n:020d}"
        self.ws[name] = {"files": {}, "deliverable": False, "frozen": False, "compute_ms": 0, **kw}
        return name

    def _linked(self, hotkey: str) -> None:
        if hotkey in self.suspended:
            raise PlatformError("suspended", 403, "agent_suspended")
        if hotkey in self.unlinked:
            raise PlatformError(NOT_LINKED_WORDS, 404, "miner_not_linked")

    def miner_key(self, hotkey: str) -> dict[str, Any]:
        self._linked(hotkey)
        self.looked_up.append(hotkey)
        return {"hotkey": hotkey, "key_id": f"k-{hotkey[:6]}"}

    def close_grants(self, hotkey: str) -> dict[str, Any]:
        self.closed.append(hotkey)
        mine = [n for n, w in self.ws.items() if w.get("hotkey") == hotkey and not w["frozen"]]
        for n in mine:
            self.ws[n]["frozen"] = True
        return {"hotkey": hotkey, "frozen": mine}

    def grant(
        self,
        hotkey: str,
        *,
        brief: str = "",
        seed: str = "",
        name: str = "job",
        no_questions: bool = False,
    ) -> str:
        self._linked(hotkey)
        self.grants.append(
            {"hotkey": hotkey, "brief": brief, "seed": seed, "no_questions": no_questions}
        )
        files = dict(self.ws[seed]["files"]) if seed else {"brief/brief.md": brief}
        return self._new(hotkey=hotkey, files=files)

    def freeze(self, ws: str) -> dict[str, Any]:
        self.ws[ws]["frozen"] = True
        return {"compute_ms": self.ws[ws]["compute_ms"]}

    def fork(self, ws: str) -> str:
        src = self.ws[ws]
        self.forks += 1
        return self._new(
            files=dict(src["files"]),
            deliverable=src["deliverable"],
            weak=src.get("weak", False),
            draw=src.get("draw", ""),
            report_fails=src.get("report_fails", False),
            stage=src.get("stage", "check"),
            diagnostics=src.get("diagnostics", []),
            as_delivered=src.get("as_delivered", []),
            host_refused=src.get("host_refused", ""),
            old_watt=src.get("old_watt", False),
            pinned_old=src.get("pinned_old", False),
            hotkey="validator",
        )

    def run(self, ws: str, command: str, args: Any = None, **_: Any) -> dict[str, Any]:
        """``status``, and ``test --mutate`` as Watt's sampled guard on the operator's lane: a
        weak design (one only a lucky draw passes) is refused by the validator's draw, which then
        leaves the miner's record in place; a sound one is recorded with the validator's draw."""
        w = self.ws[ws]
        self.runs.append((ws, command))
        digest = str(abs(hash(json.dumps(w["files"], sort_keys=True))))
        if command == "check" and (args or {}).get("as_delivered"):
            # Watt's check of the tree as a delivery carries it (no .watt/, no build/)
            if w.get("old_watt"):  # a Watt that does not know the mode
                return {"state": "done", "ok": False, "error": "unrecognized arguments"}
            if w.get(
                "pinned_old"
            ):  # a workspace on a release from before the check (ADR-037 pinning)
                return {
                    "state": "done",
                    "ok": False,
                    "error": "`watt check` takes project; not ['as_delivered']",
                }
            if w.get("as_delivered"):
                return {"state": "done", "ok": False, "data": {"diagnostics": w["as_delivered"]}}
            return {"state": "done", "ok": True, "data": {"diagnostics": []}}
        if command in ("test", "report") and (args or {}).get("mutate") and w.get("host_refused"):
            # the platform refused the remote run's result for a reason of its own
            why = w["host_refused"]
            return {
                "state": "done",
                "ok": False,
                "error": "result refused",
                "host_refused": {"code": "host_refused", "why": why},
            }
        if command in ("test", "report") and (args or {}).get("mutate"):
            if w.get("weak"):
                guard = {
                    "digest": digest,
                    "sampled": {"seed": "sha256:validator"},
                    "survivors": [{"label": "drop hook", "requirements": ["holds"]}],
                    "requirements": [
                        {"requirement": "holds", "status": "unguarded"},
                        {"requirement": "fits", "status": "unproven"},
                        {"requirement": "mounts", "status": "killed"},
                    ],
                }
                return {"state": "done", "ok": False, "data": {"guard": guard}}
            if w.get("report_fails"):  # the guard passes but the delivery's files cannot be made
                guard = {"digest": digest, "sampled": {"seed": "sha256:validator"}, "survivors": []}
                return {"state": "done", "ok": False, "data": {"guard": guard}}
            w.update(deliverable=True, draw="sha256:validator")
            guard = {"digest": digest, "sampled": {"seed": "sha256:validator"}, "survivors": []}
            return {"state": "done", "ok": True, "data": {"guard": guard}}
        stage = "deliverable" if w["deliverable"] else w.get("stage", "check")
        draw = {"sampled": [3, 9], "seed": w.get("draw") or "sha256:miner"}
        return {
            "state": "done",
            "ok": True,
            "data": {
                "diagnostics": w.get("diagnostics", []),
                "status": {
                    "deliverable": w["deliverable"],
                    "stage": stage,
                    "digest": digest,
                    "guard_draw": draw if w["deliverable"] else None,
                },
            },
        }

    def export(self, ws: str) -> bytes:
        return project_zip(self.ws[ws]["files"])

    def delete(self, ws: str) -> None:
        self.ws.pop(ws, None)
        self.deleted.append(ws)

    def cancel_run(self, run: str) -> None:
        self.cancelled_runs.append(run)

    # the miner's side, for the tests
    def work(
        self,
        ws: str,
        files: dict[str, str],
        *,
        deliverable: bool,
        compute_ms: int = 60_000,
        weak: bool = False,
    ) -> None:
        """``deliverable``: the miner's own guard record says so; ``weak``: only because its draw
        was lucky (a re-rolled draw), so the validator's own draw refuses it."""
        assert not self.ws[ws]["frozen"]
        self.ws[ws].update(
            files={**self.ws[ws]["files"], **files},
            deliverable=deliverable,
            compute_ms=compute_ms,
            weak=weak,
            draw="sha256:miner",
        )


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def wait_state(
    desk: Desk, job: str, states: tuple[str, ...], timeout: float = 5.0
) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = desk.status(job)
        if st["state"] in states:
            return st
        time.sleep(0.02)
    raise AssertionError(f"{job} stayed {desk.status(job)['state']}")


def passing(job: str, hk: str, *, ended_ms: int = 0, **kw: Any) -> JobOutcome:
    facts: dict[str, Any] = {"wall_s": 1800, **kw}
    return JobOutcome(job=job, hotkey=hk, ended_ms=ended_ms, deliverable=True, kind="pass", **facts)


def points(*signals: dict[str, Any]) -> float:
    """A delivered job's points after the client's ``signals``, in order."""
    return score_all([passing("a", A, signals=list(signals))])[0].s


DOWNLOAD = {"kind": "download"}


def stars(n: int) -> dict[str, Any]:
    return {"kind": "stars", "stars": n}


QUALITY_REFUND = {"kind": "refund", "quality": True}


class TestTheRule(unittest.TestCase):
    """The owner's rule (2026-11-06), replayed one line at a time."""

    def test_an_unrated_delivery_is_half_a_point(self) -> None:
        self.assertEqual(points(), 0.5)

    def test_a_download_is_one(self) -> None:
        self.assertEqual(points(DOWNLOAD), 1.0)

    def test_three_four_or_five_stars_are_one(self) -> None:
        for n in (3, 4, 5):
            self.assertEqual(points(stars(n)), 1.0, n)

    def test_one_or_two_stars_are_zero(self) -> None:
        for n in (1, 2):
            self.assertEqual(points(stars(n)), 0.0, n)

    def test_a_quality_refund_is_zero_and_another_refund_is_not_scored(self) -> None:
        self.assertEqual(points(QUALITY_REFUND), 0.0)
        self.assertEqual(points(DOWNLOAD, QUALITY_REFUND), 0.0)
        self.assertEqual(points({"kind": "refund", "quality": False}), 0.5)
        self.assertEqual(points(DOWNLOAD, {"kind": "refund", "quality": False}), 1.0)

    def test_the_latest_signal_decides(self) -> None:
        self.assertEqual(points(DOWNLOAD, stars(1)), 0.0)  # a download, then 1 star
        self.assertEqual(points(stars(1), stars(5)), 1.0)  # 1 star, then 5
        self.assertEqual(points(QUALITY_REFUND, stars(4)), 1.0)

    def test_a_download_never_outranks_the_clients_verdict(self) -> None:
        self.assertEqual(points(stars(2), DOWNLOAD), 0.0)  # rated down, then downloaded by anyone
        self.assertEqual(points(QUALITY_REFUND, DOWNLOAD), 0.0)
        self.assertEqual(points(stars(1), {**DOWNLOAD, "by": "visitor"}), 0.0)
        self.assertEqual(points({**DOWNLOAD, "by": "visitor"}), 0.5)  # only the client scores
        self.assertEqual(points({**DOWNLOAD, "by": "visitor"}, DOWNLOAD), 1.0)
        self.assertEqual(points(DOWNLOAD, stars(4), DOWNLOAD), 1.0)

    def test_only_a_delivery_has_points(self) -> None:
        for kind in ("ours", "copy", "", "disabled"):  # the facts of a delivery, another kind
            o = JobOutcome(
                job="k", hotkey=A, ended_ms=0, deliverable=True, kind=kind, signals=[DOWNLOAD]
            )
            self.assertEqual(score_all([o])[0].s, 0.0, kind)
            self.assertFalse(o.passed)

    def test_changes_reruns_and_trust_change_nothing(self) -> None:
        self.assertEqual(points({"kind": "change"}), 0.5)
        self.assertEqual(points({"kind": "rerun"}), 0.5)
        self.assertEqual(points(DOWNLOAD, {"kind": "change"}, {"kind": "rerun"}), 1.0)
        for trust in ("free", "paid"):
            self.assertEqual(points({**stars(2), "trust": trust}), 0.0)
            self.assertEqual(points({**DOWNLOAD, "trust": trust}), 1.0)

    def test_every_attempt_that_does_not_deliver_is_zero(self) -> None:
        for kind in ("gate", "timeout", "reject", "refused"):
            o = JobOutcome(job="f", hotkey=A, ended_ms=0, deliverable=False, kind=kind)
            self.assertEqual(score_all([o])[0].s, 0.0, kind)
            self.assertEqual(o.parts, {"failed": kind})
        copy = passing("c", A, copy_of="other", signals=[DOWNLOAD])
        self.assertEqual(score_all([copy])[0].s, 0.0)  # a copy, even downloaded
        failed = JobOutcome(job="g", hotkey=A, ended_ms=0, deliverable=False, signals=[DOWNLOAD])
        self.assertEqual(score_all([failed])[0].s, 0.0)

    def test_a_job_older_than_seven_days_drops_out(self) -> None:
        now = 30 * 24 * HOUR_MS
        jobs = score_all(
            [
                passing("in", A, ended_ms=now - 7 * 24 * HOUR_MS, signals=[DOWNLOAD]),  # the edge
                passing("out", A, ended_ms=now - 7 * 24 * HOUR_MS - 1, signals=[DOWNLOAD]),
                passing("new", A, ended_ms=now),
            ]
        )
        weight, average = standing(jobs, now)
        self.assertEqual(weight[A], 1.5)  # flat: no decay inside the window
        self.assertEqual(average[A], 0.75)
        ledger = Ledger()
        for o in jobs:
            ledger.add(o)
        window, weight, _ = ledger.snapshot(now)
        self.assertEqual(({o.job for o in window}, weight[A]), ({"in", "new"}, 1.5))
        self.assertEqual({o.job for o in ledger.outcomes}, {"in", "new"})  # forgotten

    def test_weights_are_proportional_to_points(self) -> None:
        uids = {OWNER: 0, A: 1, B: 2, C: 3}
        now = 10 * HOUR_MS
        jobs = score_all(
            [
                passing("a1", A, ended_ms=now, signals=[DOWNLOAD]),
                passing("a2", A, ended_ms=now, signals=[stars(5)]),
                passing("a3", A, ended_ms=now - 6 * 24 * HOUR_MS, signals=[stars(4)]),
                passing("a4", A, ended_ms=now),  # 0.5
                passing("b1", B, ended_ms=now, signals=[DOWNLOAD]),
                passing("b2", B, ended_ms=now),  # 0.5
                passing("c1", C, ended_ms=now, signals=[stars(1)]),
                JobOutcome(job="c2", hotkey=C, ended_ms=now, deliverable=False, kind="gate"),
            ]
        )
        weight, _ = standing(jobs, now)
        self.assertEqual(weight, {A: 3.5, B: 1.5, C: 0.0})
        vec, rule = vector(
            weight, uids, owner_hotkey=OWNER, admitted=[A, B, C, OWNER], reachable=[A, B, C]
        )
        self.assertEqual(rule, "history")
        got = dict(vec)
        self.assertEqual(got[1], 65_535)
        self.assertEqual(got[2], round(65_535 * 1.5 / 3.5))
        self.assertNotIn(3, got)  # no points, no weight
        self.assertNotIn(0, got)  # never the owner

    def test_genesis_when_nobody_has_points(self) -> None:
        uids = {OWNER: 0, A: 1, B: 2, C: 3}
        vec, rule = vector(
            {},
            uids,
            owner_hotkey=OWNER,
            admitted=[A, B, OWNER],
            reachable=[A, B, OWNER],
            genesis="hotkey",
        )
        self.assertEqual(vec, [(1, 65_535), (2, 65_535)])
        self.assertTrue(rule.startswith("genesis"))
        # delivered: only miners with a delivered client job, even when it earned 0 points
        vec, rule = vector(
            {A: 0.0}, uids, owner_hotkey=OWNER, admitted=[A, B], reachable=[A, B], proven=[A]
        )
        self.assertEqual(vec, [(1, 65_535)])
        self.assertEqual(rule, "genesis: equal per coldkey among delivered miners")
        self.assertEqual(vector({}, uids, owner_hotkey=OWNER, admitted=[], reachable=[A])[0], [])

    def test_reports_recompute_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(report_dir=Path(d))
            ledger.add(passing("1", A, ended_ms=1000))
            ledger.add(passing("3", B, ended_ms=1000))
            ledger.add(JobOutcome(job="2", hotkey=B, ended_ms=2000, deliverable=False, kind="gate"))
            self.assertEqual(ledger.signal("1", {**stars(4), "trust": "paid", "at_ms": 3000}), 1.0)
            self.assertIsNone(ledger.signal("2", stars(5)))  # not delivered
            rep = ledger.report(
                4000, {OWNER: 0, A: 1, B: 2}, owner_hotkey=OWNER, admitted=[A, B], reachable=[A, B]
            )
            self.assertEqual(rep["weight"], {A: 1.0, B: 0.5})
            self.assertEqual(rep["average"], {A: 1.0, B: 0.25})
            self.assertNotIn("quality", rep)
            self.assertEqual(rep["vector"], [[1, 65_535], [2, round(65_535 / 2)]])
            audit = recompute(json.loads(next(Path(d).glob("hour-*.json")).read_text()))
            self.assertTrue(audit["matches_published"])
            self.assertEqual(audit["mismatches"], [])
            again = Ledger(report_dir=Path(d))
            self.assertEqual(len(again.outcomes), 3)
            self.assertNotIn("trust", again.outcomes[0].signals[0])  # never kept
            self.assertEqual(again.snapshot(4000)[1], {A: 1.0, B: 0.5})

    def test_a_restart_rescores_older_rows_by_this_rule(self) -> None:
        """Rows an older release stored (its efficiency facts, trust, vote, remix credit) load and
        score by this rule alone: no miner keeps a mix of old and new points."""
        old = {
            "job": "j",
            "hotkey": A,
            "ended_ms": 1000,
            "deliverable": True,
            "kind": "pass",
            "size": "short",
            "compute_s": 9.0,
            "effort": "max",
            "credit": [B],
            "vote": {"V": 0.0, "u": 0.4},
            "s": 0.123,
            "parts": {"I": 0.9},
            "signals": [{"kind": "download", "trust": "free", "at_ms": 2000}],
        }
        with tempfile.TemporaryDirectory() as d:
            report = {"jobs": [old], "vector_by_hotkey": {}}
            (Path(d) / "hour-1.json").write_text(json.dumps(report))
            ledger = Ledger(report_dir=Path(d))
            [o] = ledger.outcomes
            self.assertEqual((o.s, o.parts), (1.0, {"signal": "download"}))
            self.assertEqual(ledger.snapshot(3000)[1], {A: 1.0})  # B gets no remix credit
        self.assertEqual(outcome_of(old).signals, [{"kind": "download", "at_ms": 2000}])


class TestDesk(unittest.TestCase):
    def setUp(self) -> None:
        self.p = FakePlatform()
        self.clock = Clock()
        self.ledger = Ledger(hour_ms=3_600_000)
        self.reg = [A, B, C]
        self.desk = Desk(
            self.p,
            self.ledger,
            registered=lambda: self.reg,
            clock=self.clock,
            rng=random.Random(7),  # noqa: S311 -- a test's seed,
        )
        for hk in (A, B, C):
            self.desk.agent_key(hk)
            self.desk.miners[hk].admitted = True  # the entry test has its own test

    def tearDown(self) -> None:
        self.desk.stop()

    def tick(self) -> None:
        """One pass of the desk, and its worker tasks (grants, judgements, results) finished."""
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def enqueue(self, body: dict[str, Any]) -> None:
        """A client job enters the platform's queue; the validator claims it on its next tick."""
        self.p.queue.append(dict(body))
        self.tick()

    def poll(self, *hks: str) -> dict[str, dict[str, Any]]:
        return {hk: self.desk.work(hk) for hk in hks}

    def assigned(self) -> tuple[str, dict[str, Any]]:
        for hk, w in self.poll(A, B, C).items():
            if w["job"]:
                return hk, w
        raise AssertionError("nobody has the job")

    def test_one_miner_gets_the_job_and_a_pass_is_delivered_and_charged(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j1", "brief": "A wall hook for my coat."})
        hk, w = self.assigned()
        self.assertEqual(
            sum(1 for x in self.poll(A, B, C).values() if x["job"]), 1
        )  # one miner per job
        self.desk.accept(hk, "j1")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, "j1", w["workspace"], "d")
        st = wait_state(self.desk, "j1", ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("delivered", True))
        self.assertTrue(self.p.ws[w["workspace"]]["frozen"])
        self.assertEqual(self.ledger.outcomes[-1].s, 0.5)  # delivered, the client silent so far
        self.tick()
        self.assertEqual(self.p.completed["j1"]["label"], "delivered")  # back to the platform

    def attempt(self, job: str, *, deliverable: bool, files: dict[str, str] | None = None) -> str:
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(
            w["workspace"], files or {"notes.md": f"tried by {hk[:6]}"}, deliverable=deliverable
        )
        self.desk.submit(hk, job, w["workspace"], "d")
        wait_state(self.desk, job, ("queued", "done"))
        self.poll(A, B, C)
        self.tick()
        return hk

    def reject(self, job: str) -> str:
        hk, _ = self.assigned()
        self.desk.reject(hk, job, "not-allowed", f"{hk[:6]}: this is a weapon part")
        self.poll(A, B, C)
        self.tick()
        return hk

    def scored(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for o in self.ledger.outcomes:
            out.setdefault(o.hotkey, []).append(o.s)
        return out

    def test_a_failed_gate_ends_the_round_not_finished_with_its_project_kept(self) -> None:
        """One worked attempt that does not deliver ends the round (DESIGN.md "Continuing an
        unfinished round"): the project is kept for the client, never handed to another miner
        from scratch; nothing is charged; one owner's failure counts (0)."""
        self.poll(A, B, C)
        self.enqueue({"job": "j2", "brief": "A wall hook."})
        first = self.attempt("j2", deliverable=False, files={"src/parts/broken.py": "x"})
        st = self.desk.status("j2")
        res = st["result"]
        self.assertEqual(
            (st["state"], res["label"], res["charge"]), ("done", "not finished", False)
        )
        self.assertEqual(len(st["attempts"]), 1)
        fork = self.desk.jobs["j2"].attempts[0].fork
        self.assertEqual(res["workspace"], fork)
        self.assertNotIn(fork, self.p.deleted)  # kept: the client may continue it
        self.assertIn("src/parts/broken.py", self.p.ws[fork]["files"])
        self.assertEqual(self.scored()[first], [0.0])
        self.assertEqual(self.p.completed["j2"]["label"], "not finished")

    def test_two_rejections_decline_the_job_with_the_messages_and_each_scores_zero(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j7", "brief": "A suppressor for my rifle."})
        r1 = self.reject("j7")
        r2 = self.reject("j7")
        self.assertNotEqual(r1, r2)
        st = self.desk.status("j7")
        res = st["result"]
        self.assertEqual((res["label"], res["charge"], res["review"]), ("declined", False, True))
        self.assertEqual(len(res["messages"]), 2)
        self.assertEqual(res["messages"][0]["category"], "not-allowed")
        self.assertEqual(self.scored(), {r1: [0.0], r2: [0.0]})  # a hand-back is 0, at once
        self.assertEqual(self.desk.telemetry()[r1]["rejected"], 1)

    def test_needs_client_is_no_rejection(self) -> None:
        """Owner 2026-10-24: no questions -- the builder assumes, records and delivers; a need is
        never a reason to decline a job."""
        self.poll(A, B, C)
        self.enqueue(
            {"job": "jn", "brief": "A gamepad that charges the phone.", "no_questions": True}
        )
        hk, _ = self.assigned()
        with self.assertRaisesRegex(DeskError, "not-allowed or not-capable"):
            self.desk.reject(hk, "jn", "needs-client", "Charge the phone, or leave it out?")

    def test_a_rejection_overturned_by_a_pass_counts_against_the_rejecter(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j8", "brief": "A wall hook."})
        rejecter = self.reject("j8")
        hk, w = self.assigned()
        self.desk.accept(hk, "j8")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, "j8", w["workspace"], "d")
        wait_state(self.desk, "j8", ("done",))
        self.assertEqual(self.scored()[rejecter], [0.0])
        self.assertEqual(self.desk.telemetry()[rejecter]["overturned"], 1)
        with self.assertRaises(DeskError):
            self.desk.reject(hk, "j8", "maybe", "x")  # a closed category

    def test_an_unaccepted_job_times_out_into_the_same_path(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j4", "brief": "A wall hook."})
        first, _ = self.assigned()
        self.clock.t += 200  # past the 120 s acceptance window
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.ledger.outcomes[-1].hotkey, first)
        self.assertEqual(self.ledger.outcomes[-1].s, 0.0)
        second, _ = self.assigned()
        self.assertNotEqual(second, first)

    def test_four_timeouts_end_the_job_didnt_finish_and_each_counts(self) -> None:
        self.reg.append(D)
        self.desk.agent_key(D)
        self.desk.miners[D].admitted = True
        for _ in range(4):
            self.poll(A, B, C, D)
            if not self.desk.jobs:
                self.enqueue({"job": "j9", "brief": "A wall hook."})
            self.tick()
            self.clock.t += 200
        self.poll(A, B, C, D)
        self.tick()
        st = self.desk.status("j9")
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("didn't finish", False))
        self.assertEqual(
            [o.s for o in self.ledger.outcomes], [0.0] * 4
        )  # reliability is the miner's

    def test_a_copy_of_another_miners_output_fails(self) -> None:
        self.poll(A, B, C)
        for job in ("j5", "j6"):
            self.enqueue({"job": job, "brief": "A wall hook."})
            self.poll(A, B, C)
            self.tick()
        jobs = {w["job"]: (hk, w) for hk, w in self.poll(A, B, C).items() if w["job"]}
        (h5, w5), (h6, w6) = jobs["j5"], jobs["j6"]
        self.assertNotEqual(h5, h6)
        for hk, w, job in ((h5, w5, "j5"), (h6, w6, "j6")):
            self.desk.accept(hk, job)
            self.p.work(w["workspace"], GOOD, deliverable=True)
            self.desk.submit(hk, job, w["workspace"], "d")
            wait_state(self.desk, job, ("done", "queued"))
        states = sorted(self.desk.status(j)["state"] for j in ("j5", "j6"))
        self.assertEqual(states, ["done", "queued"])  # the second identical output is a copy

    def test_private_jobs_never_reach_miners(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "p1", "brief": "secret", "privacy": "private"})
        self.assertEqual(
            (self.p.completed["p1"]["label"], self.p.completed["p1"]["charge"]), ("refused", False)
        )
        self.assertTrue(all(w["job"] is None for w in self.poll(A, B, C).values()))

    def free_at(self, **clocks: int) -> None:
        for hk, ms in clocks.items():
            self.desk.miners[{"A": A, "B": B, "C": C}[hk]].free_ms = ms

    def test_the_router_gives_the_job_to_the_best_record_and_every_fifth_to_the_longest_wait(
        self,
    ) -> None:
        from cadmind_einstein.desk import Job

        def routed() -> str:
            return self.desk.route(Job(job="x", brief="b")).hotkey  # type: ignore[union-attr]

        self.poll(A, B, C)
        self.free_at(A=3000, B=1000, C=2000)
        self.assertEqual(routed(), B)  # equal records (none has a job): the longest wait
        now = self.desk.now_ms()
        for i in range(6):  # C's clients downloaded; B's rated it down
            self.ledger.add(passing(f"c{i}", C, ended_ms=now, signals=[{"kind": "download"}]))
            self.ledger.add(passing(f"b{i}", B, ended_ms=now, signals=[stars(1)]))
        self.assertEqual(routed(), C)  # the best record, although B waited longer
        self.desk.miners[C].job = "busy"  # C is at work: the next best
        self.assertEqual(routed(), A)  # no job yet reads as unrated (0.5), above B's
        self.desk.miners[C].job = ""
        # every fifth client assignment goes to the longest wait, whatever the record
        self.desk.assigned[A] = 4
        self.assertEqual(routed(), B)
        self.desk.assigned[A] = 5
        self.assertEqual(routed(), C)

    def test_one_lucky_job_does_not_put_a_newcomer_first(self) -> None:
        from cadmind_einstein.scoring import record

        now = self.desk.now_ms()
        jobs = [passing("n", A, ended_ms=now, signals=[{"kind": "download"}])]
        jobs += [
            passing(f"v{i}", B, ended_ms=now, signals=[{"kind": "download"}] if i < 9 else [])
            for i in range(12)
        ]
        got = record(score_all(jobs), now)
        self.assertEqual(got[A], 0.55)  # (1 + 9 * 0.5) / 10
        self.assertEqual(got[B], 0.875)  # (9 + 3 * 0.5) / 12: no padding past ten jobs
        self.assertGreater(got[B], got[A])

    def test_more_hotkeys_of_one_owner_buy_no_more_turns_in_the_trial_lane(self) -> None:
        from cadmind_einstein.desk import Job

        self.poll(A, B, C)
        self.desk.coldkeys = lambda: {A: "cold-1", B: "cold-1", C: "cold-2"}
        self.free_at(A=5000, B=0, C=3000)  # B is cold-1's fresh hotkey; cold-1 was served last
        self.desk.assigned[A] = 4  # the trial lane
        self.assertEqual(self.desk.route(Job(job="x", brief="b")).hotkey, C)  # type: ignore[union-attr]

    def test_jobs_go_round_the_line_and_a_hand_back_goes_to_the_back(self) -> None:
        self.poll(A, B, C)
        self.free_at(A=1, B=2, C=3)
        self.enqueue({"job": "w1", "brief": "A wall hook."})
        hk, _ = self.assigned()
        self.assertEqual(hk, A)
        self.clock.t += 10
        self.desk.reject(A, "w1", "not-capable", "not for me")  # A hands it back: back of the line
        self.assertEqual(self.desk.miners[A].free_ms, self.desk.now_ms())
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.assigned()[0], B)  # w1 goes on to B (A tried it)
        self.enqueue({"job": "w2", "brief": "A shelf."})
        self.assertEqual(self.desk.jobs["w2"].attempts[-1].hotkey, C)  # C waited longer than A
        self.desk.reject(C, "w2", "not-capable", "no")
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.desk.jobs["w2"].attempts[-1].hotkey, A)  # A is next: B is busy

    def test_a_revision_prefers_its_builder_over_the_line(self) -> None:
        from cadmind_einstein.desk import Job

        self.poll(A, B, C)
        self.free_at(A=1, B=2, C=3)
        self.assertEqual(self.desk.route(Job(job="x", brief="b", prefer=C)).hotkey, C)  # type: ignore[union-attr]
        self.assertEqual(self.desk.route(Job(job="x", brief="b")).hotkey, A)  # type: ignore[union-attr]

    def test_the_judges_vote_changes_nothing(self) -> None:
        """Votes are recorded elsewhere (:mod:`.votes`): the ledger has no way to take one, and a
        vote stored on an older row is dropped."""
        self.assertFalse(hasattr(self.ledger, "vote"))
        o = outcome_of(
            {
                "job": "v",
                "hotkey": A,
                "ended_ms": 0,
                "deliverable": True,
                "kind": "pass",
                "vote": {"V": 0.0, "u": 0.4},
            }
        )
        self.assertEqual(score_all([o])[0].s, 0.5)
        self.assertFalse(hasattr(o, "vote"))

    def test_only_admitted_miners_get_client_jobs_and_the_entry_test_admits(self) -> None:
        for hk in (A, B, C):
            self.desk.miners[hk].admitted = False
        self.poll(A, B, C)
        self.enqueue({"job": "c1", "brief": "A wall hook."})
        self.assertEqual([j["job"] for j in self.p.queue], ["c1"])  # nobody admitted: not claimed
        adm = self.desk.admission(A)
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(A)
        self.assertEqual(w["job"], adm["job"])  # assigned to A alone
        self.assertIsNone(self.desk.work(B)["job"])
        self.desk.accept(A, w["job"])
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(A, w["job"], w["workspace"], "d")
        st = wait_state(self.desk, w["job"], ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("admitted", False))
        self.assertEqual(self.desk.admitted(), [A])
        self.assertEqual(self.ledger.outcomes, [])  # the entry test is never scored
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.desk.work(A)["job"], "c1")

    def test_with_no_free_admitted_miner_nothing_is_claimed_and_jobs_stay_on_the_platform(
        self,
    ) -> None:
        for hk in (A, B, C):
            self.desk.miners[hk].admitted = False
        self.enqueue({"job": "q1", "brief": "A wall hook."})
        self.enqueue({"job": "q2", "brief": "A wall hook."})
        self.clock.t += 10 * 3600  # hours pass: never rejected, never claimed without a miner
        self.tick()
        self.assertEqual([j["job"] for j in self.p.queue], ["q1", "q2"])
        self.assertEqual(self.desk.jobs, {})
        cap = self.desk.capacity()
        self.assertEqual(
            (cap["active_miners"], cap["free_miners"], cap["median_job_s"]), (0, 0, None)
        )
        self.desk.miners[A].admitted = True
        self.poll(A, B, C)
        self.tick()  # one free admitted miner: one claim, assigned at once
        self.assertEqual([j["job"] for j in self.p.queue], ["q2"])
        self.assertEqual(self.desk.work(A)["job"], "q1")
        st = self.desk.cancel("q1")  # the client cancels a claimed job: not charged, miner freed
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("cancelled", False))
        self.assertIsNone(self.desk.work(A)["job"])
        self.assertEqual(self.ledger.outcomes, [])

    def test_a_reassignment_with_no_free_miner_holds_the_claim_and_renews_its_lease(self) -> None:
        # B and C could take it later (admitted, held off for now): the job waits for them; with
        # no such miner left it would end (TestAllTried)
        for hk in (B, C):
            self.desk.miners[hk].blocked_until_ms = self.desk.now_ms() + 10**9
        self.poll(A, B, C)
        self.enqueue({"job": "h1", "brief": "A wall hook."})
        self.enqueue({"job": "h2", "brief": "A wall hook."})
        self.assertEqual([j["job"] for j in self.p.queue], ["h2"])  # one miner, one claim
        self.desk.reject(A, "h1", "not-capable", "too hard for me")
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status("h1")["state"], "queued")  # held, still ours
        self.clock.t += 60
        self.poll(A, B, C)
        self.tick()
        self.assertEqual([j["job"] for j in self.p.queue], ["h2"])  # nothing claimed while held
        self.assertIn("h1", self.p.leased)
        self.desk.miners[B].blocked_until_ms = 0
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.desk.work(B)["job"], "h1")  # the held job takes the free miner first

    def test_a_lost_lease_drops_the_job_and_a_restart_reconciles(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "l1", "brief": "A wall hook."})
        hk, _ = self.assigned()
        self.p.lost.add("l1")
        self.clock.t += 60
        self.tick()
        self.assertEqual(self.desk.status("l1")["result"]["label"], "cancelled")
        self.assertIsNone(self.desk.work(hk)["job"])
        fresh = Desk(self.p, Ledger(), registered=lambda: self.reg, clock=self.clock)
        self.p.leased.append("l2")
        self.assertEqual(fresh.reconcile(), 2)  # l1 is still leased on this fake, l2 too
        self.assertEqual(fresh.status("l2")["state"], "queued")

    def test_client_signals_and_a_rerun_update_the_score(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "s1", "brief": "A wall hook."})
        hk, w = self.assigned()
        self.desk.accept(hk, "s1")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, "s1", w["workspace"], "d")
        wait_state(self.desk, "s1", ("done",))
        out = self.desk.signal("s1", {"kind": "stars", "stars": 2, "trust": "paid"})
        self.assertEqual(out["score"], 0.0)
        out = self.desk.signal("s1", {"kind": "download", "by": "visitor"})
        self.assertEqual(out["score"], 0.0)  # a download never outranks the client's rating
        self.assertEqual(self.ledger.outcomes[0].signals[-1]["by"], "visitor")
        self.assertEqual(self.desk.signal("s1", {"kind": "stars", "stars": 4})["score"], 1.0)
        self.enqueue({"job": "s2", "brief": "A wall hook.", "rerun_of": "s1", "trust": "paid"})
        self.assertEqual(self.ledger.outcomes[0].s, 1.0)  # a re-run is recorded, not scored
        self.assertEqual(self.ledger.outcomes[0].signals[-1]["kind"], "rerun")
        self.poll(A, B, C)
        self.tick()
        nxt = [h for h, x in self.poll(A, B, C).items() if x["job"] == "s2"]
        self.assertTrue(nxt and nxt[0] != hk)  # a different miner
        with self.assertRaises(DeskError):
            self.desk.signal("s1", {"kind": "stars", "stars": 9, "trust": "paid"})

    def test_a_miner_watt_suspended_gets_no_job_and_no_key_lookup(self) -> None:
        self.p.suspended.add(A)  # Watt's telemetry suspended A: no lookup, no grant for it
        self.poll(A, B, C)
        for n in range(3):
            self.enqueue({"job": f"s{n}", "brief": "A wall hook."})
            self.poll(A, B, C)
            self.tick()
        self.assertEqual(self.desk.miners[A].key_id, "")
        owners = {a.hotkey for j in self.desk.jobs.values() for a in j.attempts}
        self.assertNotIn(A, owners)
        self.assertLessEqual(sum(1 for g in self.p.grants if g["hotkey"] == A), 1)
        with self.assertRaises(DeskError) as e:
            self.desk.agent_key(A)
        self.assertEqual(e.exception.status, 403)


class TestGateDraw(unittest.TestCase):
    """The gate draws its own sample of the mutation guard: a miner's record is one draw it may have
    re-rolled (any change draws anew) until a weak design passed, so the validator runs
    ``watt test --mutate`` on its fork (the operator's lane, its own secret draw) and passes only a
    record that run wrote."""

    setUp = TestDesk.setUp
    tearDown = TestDesk.tearDown
    tick = TestDesk.tick
    enqueue = TestDesk.enqueue
    poll = TestDesk.poll
    assigned = TestDesk.assigned

    def submit(self, job: str, *, weak: bool, deliverable: bool = True) -> tuple[str, str]:
        self.poll(A, B, C)
        self.enqueue({"job": job, "brief": "A wall hook."})
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], GOOD, deliverable=deliverable, weak=weak)
        self.desk.submit(hk, job, w["workspace"], "d")
        return hk, w["workspace"]

    def gate_runs(self, ws: str) -> list[str]:
        return [c for w, c in self.p.runs if w != ws]

    def test_a_re_rolled_draw_the_validators_draw_refuses_is_not_accepted(self) -> None:
        hk, ws = self.submit("r1", weak=True)
        st = wait_state(self.desk, "r1", ("queued", "done"))
        self.assertEqual(st["result"]["label"], "not finished")  # not delivered, nothing charged
        a = self.desk.jobs["r1"].attempts[0]
        self.assertEqual((a.hotkey, a.kind), (hk, "gate"))
        self.assertIn("validator's own mutation guard did not pass", a.outcome)
        self.assertIn("unguarded R.holds; unproven R.fits", a.outcome)
        self.assertNotIn("R.mounts", a.outcome)  # killed: guarded, not named
        self.assertEqual(self.p.completed["r1"]["charge"], False)
        # held, then counted (0) at the ending: one owner's opinion only
        self.assertEqual([o.s for o in self.ledger.outcomes], [0.0])
        self.assertEqual(
            self.gate_runs(ws), ["status", "check", "report"]
        )  # the miner's own record said yes
        self.assertTrue(self.p.ws[ws]["deliverable"])  # ... and the miner's workspace still does

    def test_the_validator_makes_the_delivery_files_and_refuses_a_design_it_cannot_report(
        self,
    ) -> None:
        """The files the client gets are made by the validator from the judged design (2026-10-16: a
        revision handed over the earlier version's files, copied with its workspace)."""
        _, ws = self.submit("r3", weak=False)
        self.p.ws[ws]["report_fails"] = True  # the fork copies it
        st = wait_state(self.desk, "r3", ("queued", "done"))
        self.assertEqual(st["result"]["label"], "not finished")
        a = self.desk.jobs["r3"].attempts[0]
        self.assertIn("did not pass", a.outcome)
        self.assertNotEqual(self.p.completed.get("r3", {}).get("label"), "delivered")

    def test_a_sound_design_passes_on_the_validators_own_draw(self) -> None:
        hk, ws = self.submit("r2", weak=False)
        st = wait_state(self.desk, "r2", ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["hotkey"]), ("delivered", hk))
        self.assertEqual(self.gate_runs(ws), ["status", "check", "report", "status"])
        fork = self.desk.jobs["r2"].attempts[0].fork
        self.assertEqual(st["result"]["workspace"], fork)
        self.assertEqual(self.p.ws[fork]["draw"], "sha256:validator")  # the record is ours
        self.assertEqual(self.p.ws[ws]["draw"], "sha256:miner")  # the miner's is untouched
        self.assertEqual(self.p.forks, 1)

    def test_a_record_the_validators_run_did_not_write_is_refused(self) -> None:
        real = self.p.run

        def stale(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "report":  # a run that says it passed, but the record is not its draw
                self.p.runs.append((ws, command))
                guard = {
                    "digest": real(ws, "status")["data"]["status"]["digest"],
                    "sampled": {"seed": "sha256:validator"},
                }
                return {"state": "done", "ok": True, "data": {"guard": guard}}
            return real(ws, command, args, **kw)

        self.p.run = stale  # type: ignore[method-assign]
        self.submit("r3", weak=False)
        st = wait_state(self.desk, "r3", ("queued", "done"))
        self.assertEqual(st["result"]["label"], "not finished")
        self.assertEqual(self.desk.jobs["r3"].attempts[0].kind, "gate")

    def test_no_guard_run_when_the_miners_own_record_is_not_deliverable(self) -> None:
        _, ws = self.submit("r4", weak=False, deliverable=False)
        wait_state(self.desk, "r4", ("queued", "done"))
        self.assertEqual(self.gate_runs(ws), ["status"])  # bounded: nothing heavy for a no

    def test_a_guard_run_that_does_not_end_is_retried_on_the_same_fork(self) -> None:
        real, calls = self.p.run, {"report": 0}

        def slow_once(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "report":
                calls["report"] += 1
                self.assertEqual(kw.get("budget_s"), GATE_RUN_S)
                if calls["report"] == 1:
                    return {"state": "running", "run": "run-1"}
            return real(ws, command, args, **kw)

        self.p.run = slow_once  # type: ignore[method-assign]
        hk, _ = self.submit("r5", weak=False)
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status("r5")["state"], "judging")  # our side: retried
        self.desk._retry_at.clear()
        self.tick()
        st = wait_state(self.desk, "r5", ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["hotkey"]), ("delivered", hk))
        self.assertEqual(self.p.forks, 1)  # the same fork, its guard outcomes reused

    def test_a_guard_run_stopped_by_its_wall_clock_limit_does_not_pass(self) -> None:
        real = self.p.run

        def wall(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "report":
                return {"state": "done", "ok": False, "stopped": "wall", "data": None}
            return real(ws, command, args, **kw)

        self.p.run = wall  # type: ignore[method-assign]
        self.submit("r6", weak=False)
        self.assertTrue(self.desk.wait_idle())
        # once may be load on our side: judged again, nobody blamed
        self.assertEqual(self.desk.status("r6")["state"], "judging")
        self.assertEqual(self.desk.jobs["r6"].attempts[0].gate_timeouts, 1)
        self.desk._retry_at.clear()
        self.tick()
        st = wait_state(self.desk, "r6", ("queued", "done"))
        self.assertEqual(st["result"]["label"], "not finished")  # twice: the attempt fails the gate
        a = self.desk.jobs["r6"].attempts[0]
        self.assertEqual(a.kind, "gate")
        self.assertIn("did not finish within the validator's budget", a.outcome)
        self.assertNotIn(a.fork, self.p.deleted)  # its fork is the project kept


class TestHostBlockAndAsDelivered(unittest.TestCase):
    """A host refusal ends the round blocked by the platform, against nobody; the gate checks the
    tree as a delivery carries it; a client's cancel keeps the builder's place in the line."""

    setUp = TestDesk.setUp
    tearDown = TestDesk.tearDown
    tick = TestDesk.tick
    enqueue = TestDesk.enqueue
    poll = TestDesk.poll
    assigned = TestDesk.assigned

    def start(self, job: str, *, deliverable: bool = True, **ws: Any) -> tuple[str, str]:
        self.poll(A, B, C)
        self.enqueue({"job": job, "brief": "A wall hook."})
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], GOOD, deliverable=deliverable)
        self.p.ws[w["workspace"]].update(ws)  # the fork copies it
        return hk, w["workspace"]

    def test_a_host_refusal_on_the_validators_run_blocks_the_round_against_nobody(self) -> None:
        hk, ws = self.start("h1", host_refused="a remote run writes only build/ and .watt/")
        before = self.desk.miners[hk].free_ms
        self.desk.submit(hk, "h1", ws, "d")
        self.assertNotEqual(self.desk.miners[hk].free_ms, before)  # the submit moved it ...
        st = wait_state(self.desk, "h1", ("done",))
        self.tick()
        res = self.p.completed["h1"]
        self.assertEqual((res["label"], res["kind"]), ("host_blocked", "host_blocked"))
        self.assertIs(res["charge"], False)
        self.assertEqual(res["host_refused"]["code"], "host_refused")
        self.assertIn("remote run writes only", res["why"])
        a = self.desk.jobs["h1"].attempts[0]
        self.assertEqual(a.kind, "host_blocked")
        self.assertEqual(res["workspace"], a.fork)  # the project kept for the client
        self.assertIn(a.fork, self.p.ws)
        self.assertNotIn(a.fork, self.p.deleted)
        self.assertEqual(self.ledger.outcomes, [])  # no 0 for anyone
        self.assertEqual(self.desk.failed.get(hk, 0), 0)
        self.assertEqual(self.desk.not_finished.get(hk, 0), 0)
        self.assertEqual(self.desk.miners[hk].free_ms, before)  # ... and the block put it back
        self.assertEqual(st["state"], "done")

    def test_a_miner_reported_host_refusal_is_reproduced_by_the_validators_own_run(self) -> None:
        # the miner's record has no guard (every guard run of its was refused): not deliverable
        hk, ws = self.start("h2", deliverable=False, stage="test", host_refused="refused")
        self.desk.submit(hk, "h2", ws, "", host_refused="result refused by the host")
        wait_state(self.desk, "h2", ("done",))
        self.assertEqual(self.desk.jobs["h2"].result["label"], "host_blocked")
        self.assertEqual(self.ledger.outcomes, [])
        runs = [c for w, c in self.p.runs if w != ws]
        self.assertEqual(runs, ["status", "report"])  # the validator's own guard run, refused

    def test_a_reported_host_refusal_the_validator_does_not_meet_counts_for_nothing(self) -> None:
        hk, ws = self.start("h3", deliverable=False, stage="test", weak=True)
        self.desk.submit(hk, "h3", ws, "", host_refused="result refused by the host")
        wait_state(self.desk, "h3", ("done",))
        job = self.desk.jobs["h3"]
        self.assertEqual(job.result["label"], "not finished")  # the record's own verdict
        self.assertIn("did not recur", job.attempts[0].outcome)
        self.assertEqual(job.attempts[0].kind, "gate")
        self.assertEqual([o.s for o in self.ledger.outcomes], [0.0])

    def test_a_reported_host_refusal_whose_validator_run_passes_is_delivered(self) -> None:
        hk, ws = self.start("h4", deliverable=False, stage="test")
        self.desk.submit(hk, "h4", ws, "", host_refused="result refused by the host")
        st = wait_state(self.desk, "h4", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")  # the host was fixed meanwhile
        runs = [c for w, c in self.p.runs if w != ws]
        self.assertEqual(runs, ["status", "report", "status", "check"])

    def test_a_blocked_round_takes_no_place_in_the_attempt_cap(self) -> None:
        hk, ws = self.start("h5", host_refused="refused")
        self.desk.submit(hk, "h5", ws, "d")
        wait_state(self.desk, "h5", ("done",))
        job = self.desk.jobs["h5"]
        self.assertEqual(len(job.attempts), 1)
        self.assertIn("host_blocked", UNCOUNTED)

    def test_the_gate_checks_the_tree_as_delivered(self) -> None:
        found = "decision fused affects 'bay.pinch', which does not exist"
        errors = [{"severity": "error", "code": "W0204", "found": found}]
        hk, ws = self.start("d1", as_delivered=errors)
        self.desk.submit(hk, "d1", ws, "d")
        st = wait_state(self.desk, "d1", ("done",))
        self.assertEqual(st["result"]["label"], "not finished")
        res = self.desk.jobs["d1"].result
        self.assertEqual(res["stage"], "as-delivered")
        self.assertIn("W0204", res["error"])
        self.assertIn("bay.pinch", res["error"])
        self.assertEqual(res["cause"], {"stage": "as-delivered", "codes": ["W0204"]})
        runs = [c for w, c in self.p.runs if w != ws]
        self.assertEqual(runs, ["status", "check"])  # before the guard: no guard run spent
        a = self.desk.jobs["d1"].attempts[0]
        self.assertEqual(a.kind, "gate")
        # the kept project is named, so the platform can read its change requests
        self.assertEqual(res["workspace"], a.fork)
        self.assertNotIn(a.fork, self.p.deleted)

    def test_an_as_delivered_check_that_does_not_answer_is_our_side(self) -> None:
        hk, ws = self.start("d2", old_watt=True)
        self.desk.submit(hk, "d2", ws, "d")
        for _ in range(100):
            if self.desk.jobs["d2"].attempts[0].retries:
                break
            time.sleep(0.02)
        a = self.desk.jobs["d2"].attempts[0]
        self.assertGreaterEqual(a.retries, 1)  # judged again later, never against the miner
        self.assertEqual(self.desk.jobs["d2"].state, "judging")
        self.assertEqual(self.ledger.outcomes, [])

    def test_a_workspace_on_a_release_from_before_the_check_is_gated_without_it(self) -> None:
        hk, ws = self.start("d3", pinned_old=True)
        self.desk.submit(hk, "d3", ws, "d")
        st = wait_state(self.desk, "d3", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")  # gated as it was when it started
        self.assertEqual(self.desk.jobs["d3"].attempts[0].retries, 0)

    def test_a_client_cancel_keeps_the_builders_place_in_the_line(self) -> None:
        hk, _ = self.start("c1")
        before = self.desk.miners[hk].free_ms
        self.clock.t += 600
        self.desk.cancel("c1")
        self.assertEqual(self.desk.miners[hk].free_ms, before)
        self.assertEqual(self.desk.miners[hk].job, "")

    def test_a_lapsed_lease_keeps_the_builders_place_in_the_line(self) -> None:
        hk, _ = self.start("c2")
        before = self.desk.miners[hk].free_ms
        self.clock.t += 600
        self.desk.cancel("c2", lost=True)
        self.assertEqual(self.desk.miners[hk].free_ms, before)

    def test_a_submits_host_refused_is_read_in_either_form(self) -> None:
        self.assertEqual(_host_refused_of({"code": "host_refused", "why": " a  b "}), "a b")
        self.assertEqual(_host_refused_of("why"), "why")
        self.assertEqual(_host_refused_of(True), "the platform refused the run's result")
        for nothing in (None, {}, "", 3, False):
            self.assertEqual(_host_refused_of(nothing), "")

    def test_the_builders_own_hand_back_still_moves_it(self) -> None:
        hk, _ = self.start("c3")
        self.clock.t += 600
        self.desk.reject(hk, "c3", "not-capable", "too big for one round")
        self.assertEqual(self.desk.miners[hk].free_ms, self.desk.now_ms())


class TestSurvivorsWords(unittest.TestCase):
    """The gate's refusal names each must the validator's guard refused, by its verdict: the
    survivors' unguarded musts, and the per-requirement ``unproven``, ``read`` and ``excused`` ones
    (watt-guard 9 refuses those too, with no survivor to name)."""

    def test_a_refusal_with_no_survivor_names_its_unproven_read_and_excused_musts(self) -> None:
        guard = {
            "survivors": [],
            "requirements": [
                {"requirement": "seals", "status": "excused"},
                {"requirement": "fits", "status": "unproven"},
                {"requirement": "grips", "status": "read"},
                {"requirement": "bears", "status": "unproven"},
                {"requirement": "holds", "status": "killed"},
                {"requirement": "rests", "status": "checked"},
                {"requirement": "joins", "status": "pending interface"},
            ],
        }
        self.assertEqual(
            _survivors(guard), ": unproven R.bears, R.fits; read R.grips; excused R.seals"
        )

    def test_unguarded_musts_come_from_survivors_and_verdicts_once_each(self) -> None:
        guard = {
            "survivors": [
                {"label": "drop hook", "requirements": ["holds"]},
                {"label": "thin wall", "requirements": ["holds"]},
            ],
            "requirements": [
                {"requirement": "holds", "status": "unguarded"},
                {"requirement": "spans", "status": "unguarded"},
            ],
        }
        self.assertEqual(_survivors(guard), ": unguarded R.holds, R.spans")

    def test_a_guard_older_than_per_requirement_verdicts_names_its_survivors(self) -> None:
        guard = {"survivors": [{"label": "drop hook", "requirements": ["holds"]}]}
        self.assertEqual(_survivors(guard), ": unguarded R.holds")

    def test_nothing_to_name_says_nothing(self) -> None:
        for guard in (None, {}, [], {"survivors": [], "requirements": []}, {"requirements": [1]}):
            self.assertEqual(_survivors(guard), "")


class TestService(unittest.TestCase):
    def test_a_signed_agent_key_request_answers_the_key_id_and_no_secret(self) -> None:
        p = FakePlatform()
        desk = Desk(p, Ledger(), registered=lambda: [A])
        server = serve(
            desk,
            "127.0.0.1",
            0,
            token="t" * 32,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        alice = Keypair.create_from_uri("//Alice")
        body = json.dumps({"x25519_public": "ignored: a miner before 0.6.0"}).encode()
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
            base + "/v1/miners/agent_key", data=body, headers=headers
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 -- loopback
            out = json.loads(r.read())
        self.assertEqual(out, {"key_id": f"k-{A[:6]}"})
        req = urllib.request.Request(base + "/v1/jobs/work", data=b"{}")  # noqa: S310 -- unsigned
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)  # noqa: S310
        self.assertEqual(e.exception.code, 401)
        server.shutdown()


if __name__ == "__main__":
    unittest.main()


def test_every_job_has_one_120_minute_deadline() -> None:
    """No effort levels: one deadline for every job, whatever effort an older platform sends."""
    from cadmind_einstein import desk

    assert desk.JOB_DEADLINE_S == 14400
    assert not hasattr(desk, "DEADLINE_S")


def test_an_older_record_with_an_effort_still_loads() -> None:
    """Jobs and outcomes saved by older releases carry ``effort`` (and jobs held failures,
    outcomes an efficiency bucket); they are dropped."""
    from cadmind_einstein.desk import _job_from

    o = {"job": "j", "hotkey": "h", "ended_ms": 1, "effort": "max", "size": "small"}
    job = _job_from({"job": "j", "brief": "b", "effort": "low", "pending": [o]})
    assert not hasattr(job, "pending")
    assert outcome_of(o).hotkey == "h"
