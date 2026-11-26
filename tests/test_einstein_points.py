"""The points rule of the open lanes (DESIGN.md "Open lanes"): two values per event, dated
changes, the rating and its padding, the dated weight and its floor, late signals, how long an
outcome is kept, recompute, and what the vector is made over in each state of the two switches."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bittensor.sp_core import Keypair

from cadmind_einstein import __spec_version__, __version__, _spec
from cadmind_einstein.desk import Desk, DeskError
from cadmind_einstein.scoring import (
    CONSTANTS,
    HOUR_MS,
    OUTCOME_KEEP_H,
    PAD_JOBS,
    POINTS,
    THRESHOLD,
    WINDOW_H,
    JobOutcome,
    Ledger,
    audit,
    changes_of,
    counts,
    lane_of,
    outcome_of,
    points,
    rating,
    rating_of,
    recompute,
    record,
    score,
    score_all,
    standing,
    weight,
)
from cadmind_einstein.store import Store
from tests.test_einstein import Clock, FakePlatform

A, B, C, D, E = (
    Keypair.create_from_uri(u).ss58_address
    for u in ("//Alice", "//Bob", "//Charlie", "//Dave", "//Eve")
)
OWNER = Keypair.create_from_uri("//Owner").ss58_address
DAY = 24 * HOUR_MS
NOW = 400 * DAY


def production(job: str, hk: str, ended_ms: int, *signals: dict[str, Any]) -> JobOutcome:
    return JobOutcome(
        job=job, hotkey=hk, ended_ms=ended_ms, deliverable=True, kind="pass", signals=list(signals)
    )


def failed(job: str, hk: str, ended_ms: int, kind: str = "gate") -> JobOutcome:
    return JobOutcome(job=job, hotkey=hk, ended_ms=ended_ms, kind=kind)


def improvement(
    job: str, hk: str, ended_ms: int, *signals: dict[str, Any], kind: str = "pass"
) -> JobOutcome:
    return JobOutcome(
        job=job,
        hotkey=hk,
        ended_ms=ended_ms,
        deliverable=True,
        kind=kind,
        lane="improvement",
        device="dev-1",
        levels={"base": 1, "result": 2, "judge": "1.3"},
        signals=list(signals),
    )


def sig(kind: str, at_ms: int, **more: Any) -> dict[str, Any]:
    return {"kind": kind, "at_ms": at_ms, **more}


class TestTheTable(unittest.TestCase):
    """Each event of the table, read from an outcome's facts and signals."""

    def event(self, o: JobOutcome) -> tuple[float, float, str]:
        got = points(o)
        self.assertEqual(got[:2], POINTS[got[2]])  # the event names its own points
        self.assertEqual(score(o), got[0])
        self.assertEqual((o.s, o.w, o.parts["event"]), got)
        return got

    def test_the_constants_are_the_published_ones(self) -> None:
        self.assertEqual((WINDOW_H, PAD_JOBS, THRESHOLD, OUTCOME_KEEP_H), (168, 10, 0.4, 4320))
        self.assertEqual(CONSTANTS["points"]["improvement_pending"], [0.5, 0.25])
        self.assertEqual(CONSTANTS["rating"], {"pad_jobs": 10, "pad_points": 0.0, "threshold": 0.4})
        self.assertEqual(CONSTANTS["router"], {"trial_every": 5, "record_jobs": 10})
        self.assertEqual((CONSTANTS["window_h"], CONSTANTS["keep_h"]), (168, 4320))
        self.assertEqual(
            set(POINTS),
            {
                "delivered",
                "approved",
                "disapproved",
                "failed",
                "improvement_pending",
                "improvement_accepted",
                "improvement_try_again",
                "improvement_not_interested",
                "improvement_withdrawn",
                "improvement_disapproved",
                "improvement_failed",
                "void",
            },
        )

    def test_a_production_job(self) -> None:
        self.assertEqual(self.event(production("j", A, 1)), (0.5, 0.5, "delivered"))
        self.assertEqual(
            self.event(production("j", A, 1, sig("download", 2))), (1.0, 1.0, "approved")
        )
        self.assertEqual(
            self.event(production("j", A, 1, sig("stars", 2, stars=4))), (1.0, 1.0, "approved")
        )
        self.assertEqual(
            self.event(production("j", A, 1, sig("download", 2), sig("stars", 3, stars=2))),
            (0.0, 0.0, "disapproved"),
        )
        self.assertEqual(
            self.event(production("j", A, 1, sig("refund", 2, quality=True))),
            (0.0, 0.0, "disapproved"),
        )
        # a change request, or a refund not for quality, is recorded and changes nothing
        self.assertEqual(
            self.event(production("j", A, 1, sig("change", 2), sig("refund", 3, quality=False))),
            (0.5, 0.5, "delivered"),
        )
        for kind in ("gate", "timeout", "reject", "copy", "refused"):
            self.assertEqual(self.event(failed("j", A, 1, kind)), (0.0, 0.0, "failed"))

    def test_an_improvement(self) -> None:
        self.assertEqual(self.event(improvement("t", A, 1)), (0.5, 0.25, "improvement_pending"))
        self.assertEqual(
            self.event(improvement("t", A, 1, sig("accepted", 2))),
            (1.0, 1.0, "improvement_accepted"),
        )
        self.assertEqual(
            self.event(improvement("t", A, 1, sig("try_again", 2))),
            (0.0, 0.0, "improvement_try_again"),
        )
        self.assertEqual(
            self.event(improvement("t", A, 1, sig("not_interested", 2))),
            (0.5, 0.25, "improvement_not_interested"),
        )
        self.assertEqual(
            self.event(improvement("t", A, 1, sig("withdrawn", 2))),
            (0.5, 0.25, "improvement_withdrawn"),
        )
        for kind in ("gate", "timeout", "copy", "refused", "not_better"):
            o = improvement("t", A, 1, kind=kind)
            self.assertFalse(o.passed)
            self.assertEqual(self.event(o), (0.0, 0.0, "improvement_failed"))

    def test_an_accepted_improvement_is_then_read_as_a_delivered_job(self) -> None:
        accepted = sig("accepted", 2)
        self.assertEqual(
            self.event(improvement("t", A, 1, accepted, sig("stars", 3, stars=5))),
            (1.0, 1.0, "improvement_accepted"),
        )
        self.assertEqual(
            self.event(improvement("t", A, 1, accepted, sig("stars", 3, stars=1))),
            (0.0, 0.0, "improvement_disapproved"),
        )
        self.assertEqual(
            self.event(improvement("t", A, 1, accepted, sig("refund", 3, quality=True))),
            (0.0, 0.0, "improvement_disapproved"),
        )
        # a download or a change request of the accepted version moves nothing
        self.assertEqual(
            self.event(improvement("t", A, 1, accepted, sig("download", 3), sig("change", 4))),
            (1.0, 1.0, "improvement_accepted"),
        )
        # the latest verdict decides, as on a client job
        self.assertEqual(
            self.event(
                improvement("t", A, 1, accepted, sig("stars", 3, stars=1), sig("stars", 4, stars=4))
            ),
            (1.0, 1.0, "improvement_accepted"),
        )

    def test_stars_do_nothing_to_an_offer_nobody_accepted(self) -> None:
        self.assertEqual(
            self.event(improvement("t", A, 1, sig("stars", 2, stars=1))),
            (0.5, 0.25, "improvement_pending"),
        )
        self.assertEqual(
            self.event(improvement("t", A, 1, sig("not_interested", 2), sig("stars", 3, stars=1))),
            (0.5, 0.25, "improvement_not_interested"),
        )

    def test_the_first_answer_to_an_offer_is_final(self) -> None:
        ledger = Ledger()
        ledger.add(improvement("t", A, 1))
        self.assertEqual(ledger.signal("t", sig("try_again", 2)), 0.0)
        self.assertEqual(ledger.signal("t", sig("accepted", 3)), 0.0)  # ignored
        [o] = ledger.outcomes
        self.assertEqual([x["kind"] for x in o.signals], ["try_again"])
        self.assertEqual(o.parts["event"], "improvement_try_again")

    def test_a_void_outcome_is_no_job_and_no_points(self) -> None:
        o = failed("j", A, 10, "reject")
        o.void = "declined"
        self.assertEqual(self.event(o), (0.0, 0.0, "void"))
        self.assertFalse(counts(o, 10))
        self.assertEqual(rating([o], 10), {})

    def test_stored_rows_from_before_the_lanes_load(self) -> None:
        o = outcome_of(
            {"job": "j", "hotkey": A, "ended_ms": 5, "deliverable": True, "kind": "pass"}
        )
        self.assertEqual((o.lane, o.device, o.levels, o.void_ms, o.w), ("production", "", {}, 0, 0))
        self.assertEqual(self.event(o), (0.5, 0.5, "delivered"))


class TestDatedChanges(unittest.TestCase):
    def test_step_zero_then_one_change_per_signal_that_moved_the_points(self) -> None:
        o = production(
            "j",
            A,
            1000,
            sig("change", 1500),  # moves nothing: no change
            sig("download", 2000),
            sig("stars", 3000, stars=1),
        )
        got = changes_of(o)
        self.assertEqual([c["id"] for c in got], [f"j/{A}/1000#{n}" for n in range(3)])
        self.assertEqual([c["at_ms"] for c in got], [1000, 2000, 3000])
        self.assertEqual([c["event"] for c in got], ["delivered", "approved", "disapproved"])
        self.assertEqual([c["signal"] for c in got], ["", "download", "stars"])
        self.assertEqual([c["rating"] for c in got], [[0.0, 0.5], [0.5, 1.0], [1.0, 0.0]])
        self.assertEqual([c["d_weight"] for c in got], [0.5, 0.5, -1.0])
        self.assertEqual(
            got[1],
            {
                "id": f"j/{A}/1000#1",
                "at_ms": 2000,
                "hotkey": A,
                "job": "j",
                "lane": "production",
                "device": "",
                "event": "approved",
                "signal": "download",
                "rating": [0.5, 1.0],
                "weight": [0.5, 1.0],
                "d_rating": 0.5,
                "d_weight": 0.5,
            },
        )
        # the changes add up to the points now
        score(o)
        self.assertEqual(sum(c["d_rating"] for c in got), o.s)
        self.assertEqual(sum(c["d_weight"] for c in got), o.w)

    def test_an_improvement_and_its_offer(self) -> None:
        o = improvement("t", A, 1000, sig("accepted", 5000), sig("stars", 9000, stars=2))
        got = changes_of(o)
        self.assertEqual(
            [(c["event"], c["rating"], c["weight"]) for c in got],
            [
                ("improvement_pending", [0.0, 0.5], [0.0, 0.25]),
                ("improvement_accepted", [0.5, 1.0], [0.25, 1.0]),
                ("improvement_disapproved", [1.0, 0.0], [1.0, 0.0]),
            ],
        )
        self.assertEqual([c["d_weight"] for c in got], [0.25, 0.75, -1.0])
        self.assertTrue(all(c["lane"] == "improvement" and c["device"] == "dev-1" for c in got))

    def test_a_failed_attempt_has_its_step_zero(self) -> None:
        [c] = changes_of(failed("j", A, 7))
        self.assertEqual(
            (c["at_ms"], c["event"], c["d_rating"], c["d_weight"]), (7, "failed", 0, 0)
        )

    def test_a_void_is_a_change_to_nothing_dated_when_it_was_made(self) -> None:
        o = production("j", A, 1000, sig("download", 2000), sig("stars", 9000, stars=5))
        o.void, o.void_ms = "operator: a test", 4000
        got = changes_of(o)
        self.assertEqual([c["event"] for c in got], ["delivered", "approved", "void"])
        self.assertEqual((got[-1]["at_ms"], got[-1]["signal"]), (4000, ""))
        self.assertEqual((got[-1]["rating"], got[-1]["weight"]), ([1.0, 0.0], [1.0, 0.0]))
        o.void_ms = 0  # an older void: dated the job's end
        self.assertEqual([(c["event"], c["at_ms"]) for c in changes_of(o)][-1], ("void", 1000))

    def test_the_ledger_stamps_a_void(self) -> None:
        ledger = Ledger()
        ledger.add(failed("j", A, 10, "reject"))
        [o] = ledger.void("j", "declined", at_ms=77)
        self.assertEqual((o.void, o.void_ms), ("declined", 77))

    def test_signals_replay_in_the_order_of_their_dates(self) -> None:
        o = production("j", A, 1000, sig("stars", 3000, stars=1), sig("download", 2000))
        self.assertEqual(
            [c["event"] for c in changes_of(o)], ["delivered", "approved", "disapproved"]
        )


class TestRatingAndWeight(unittest.TestCase):
    def test_the_rating_pads_to_ten_jobs_with_zero(self) -> None:
        jobs = score_all([production(f"j{i}", A, NOW - i) for i in range(8)])
        self.assertEqual(rating(jobs, NOW), {A: 0.4})  # 8 of 10: on the threshold
        self.assertEqual(lane_of(rating_of(rating(jobs, NOW), A)), "production")
        self.assertEqual(rating(jobs[:7], NOW), {A: 0.35})
        self.assertEqual(lane_of(0.35), "improvement")
        self.assertEqual((rating_of({}, B), lane_of(0.0)), (0.0, "improvement"))
        # the old record pads with unrated jobs instead: published beside it
        self.assertEqual(record(jobs[:7], NOW), {A: 0.5})

    def test_ten_jobs_or_more_are_not_padded(self) -> None:
        jobs = [production(f"j{i}", A, NOW - i) for i in range(12)]
        jobs += [failed(f"f{i}", A, NOW - i) for i in range(4)]
        self.assertEqual(rating(score_all(jobs), NOW), {A: 0.375})  # 6 points over 16 jobs

    def test_the_rating_reads_the_jobs_that_ended_in_the_window_at_their_points_now(self) -> None:
        old = production("old", A, NOW - 8 * DAY, sig("download", NOW - DAY))
        new = production("new", A, NOW - DAY, sig("download", NOW))
        jobs = score_all([old, new])
        self.assertEqual(rating(jobs, NOW), {A: 0.1})  # the new job alone, 1 point over 10
        # a reaction on a job older than the window changes the pay, not the rating
        self.assertEqual(weight(jobs, NOW), {A: 1.5})

    def test_an_improvement_counts_in_the_rating_and_pays_less(self) -> None:
        jobs = score_all([improvement(f"t{i}", A, NOW - i) for i in range(10)])
        self.assertEqual(rating(jobs, NOW), {A: 0.5})
        self.assertEqual(weight(jobs, NOW), {A: 2.5})
        self.assertEqual(record(jobs, NOW), {})  # the old record reads client jobs only

    def test_the_weight_reads_the_changes_dated_in_the_window(self) -> None:
        late = production("late", A, NOW - 30 * DAY, sig("download", NOW - 2 * DAY))
        gone = production("gone", B, NOW - 30 * DAY)
        jobs = score_all([late, gone])
        self.assertEqual(weight(jobs, NOW), {A: 0.5})  # only the late change is in the window
        self.assertEqual(rating(jobs, NOW), {})

    def test_a_late_low_rating_takes_points_away_and_the_weight_never_goes_below_zero(self) -> None:
        bad = production("bad", A, NOW - 30 * DAY, sig("stars", NOW - DAY, stars=1))
        jobs = score_all([bad])
        self.assertEqual(weight(jobs, NOW), {A: 0.0})  # -0.5 dated in the window, floored
        fresh = production("fresh", A, NOW - DAY)
        self.assertEqual(weight(score_all([bad, fresh]), NOW), {A: 0.0})  # 0.5 - 0.5
        more = production("more", A, NOW - DAY, sig("download", NOW))
        self.assertEqual(weight(score_all([bad, fresh, more]), NOW), {A: 1.0})

    def test_standing_is_the_dated_weight_and_the_old_average(self) -> None:
        jobs = score_all(
            [
                production("a", A, NOW - DAY, sig("download", NOW)),
                production("b", A, NOW - DAY),
                improvement("t", A, NOW - DAY),
            ]
        )
        w, average = standing(jobs, NOW)
        self.assertEqual((w, average), ({A: 1.75}, {A: 0.75}))


class TestTheLedger(unittest.TestCase):
    def test_an_outcome_is_kept_180_days_so_a_late_signal_finds_its_job(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "state.sqlite")
            ledger = Ledger(store=store)
            ledger.add(production("j", A, NOW - 100 * DAY))
            window, w, _ = ledger.snapshot(NOW)
            self.assertEqual((window, w), ([], {}))  # out of the window, still held
            self.assertEqual(ledger.signal("j", sig("download", NOW)), 1.0)
            self.assertEqual(ledger.weights(NOW), {A: 0.5})  # paid in the week it arrived
            self.assertEqual(ledger.ratings(NOW), {})
            # a restart replays the stored facts and signals into the same changes
            again = Ledger(store=Store(Path(d) / "state.sqlite"))
            self.assertEqual(again.changes(NOW), ledger.changes(NOW))
            self.assertEqual([c["event"] for c in again.changes(NOW)], ["approved"])
            ledger.snapshot(NOW + 81 * DAY)  # 181 days after its end: gone
            self.assertEqual(ledger.outcomes, [])
            self.assertIsNone(ledger.signal("j", sig("stars", NOW + 81 * DAY, stars=5)))

    def test_a_pending_offer_is_never_pruned(self) -> None:
        ledger = Ledger()
        ledger.add(improvement("pending", A, NOW - 400 * DAY))
        ledger.add(improvement("answered", A, NOW - 400 * DAY, sig("not_interested", 5)))
        ledger.add(improvement("failed", A, NOW - 400 * DAY, kind="not_better"))
        ledger.snapshot(NOW)
        self.assertEqual([o.job for o in ledger.outcomes], ["pending"])
        self.assertEqual(ledger.signal("pending", sig("accepted", NOW)), 1.0)
        self.assertEqual(ledger.weights(NOW), {A: 0.75})  # 1 - 0.25, dated now
        ledger.snapshot(NOW + DAY)
        self.assertEqual(ledger.outcomes, [])  # answered, and past its keep

    def test_changes_by_hotkey_and_since(self) -> None:
        ledger = Ledger()
        ledger.add(production("a", A, NOW - 20 * DAY, sig("download", NOW - DAY)))
        ledger.add(production("b", B, NOW - DAY))
        self.assertEqual([c["job"] for c in ledger.changes(NOW)], ["a", "b"])
        self.assertEqual([c["job"] for c in ledger.changes(NOW, A)], ["a"])
        every = ledger.changes(NOW, A, since_ms=NOW - 30 * DAY)
        self.assertEqual([c["event"] for c in every], ["delivered", "approved"])

    def test_signals_find_only_a_passing_outcome(self) -> None:
        ledger = Ledger()
        ledger.add(failed("j", A, NOW))
        ledger.add(improvement("t", A, NOW, kind="not_better"))
        self.assertIsNone(ledger.find("j"))
        self.assertIsNone(ledger.signal("t", sig("accepted", NOW)))


def uids(*hks: str) -> dict[str, int]:
    return {hk: i + 1 for i, hk in enumerate(hks)}


class TestTheReport(unittest.TestCase):
    def ledger(self, d: str) -> Ledger:
        ledger = Ledger(report_dir=Path(d))
        ledger.add(production("a1", A, NOW - DAY, sig("download", NOW - HOUR_MS)))
        ledger.add(production("a2", A, NOW - 2 * DAY))
        ledger.add(failed("b1", B, NOW - DAY))
        ledger.add(improvement("t1", B, NOW - DAY, sig("accepted", NOW - HOUR_MS)))
        ledger.add(improvement("t2", C, NOW - 3 * DAY))
        ledger.add(production("late", C, NOW - 60 * DAY, sig("stars", NOW - DAY, stars=5)))
        ledger.add(production("silent", C, NOW - 60 * DAY))  # nothing of it in the window
        return ledger

    def report(self, ledger: Ledger, **kw: Any) -> dict[str, Any]:
        return ledger.report(
            NOW,
            uids(A, B, C),
            owner_hotkey=OWNER,
            admitted=[A, B, C],
            reachable=[A, B, C],
            eligible=[A, B, C],
            **kw,
        )

    def test_the_report_publishes_the_new_numbers_beside_the_old(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rep = self.report(self.ledger(d))
        # the jobs: ended in the window, or with a change dated in it
        self.assertEqual({j["job"] for j in rep["jobs"]}, {"a1", "a2", "b1", "t1", "t2", "late"})
        row = next(j for j in rep["jobs"] if j["job"] == "t1")
        self.assertEqual(
            (row["s"], row["w"], row["lane"], row["device"], row["parts"]["event"]),
            (1.0, 1.0, "improvement", "dev-1", "improvement_accepted"),
        )
        self.assertEqual(row["levels"], {"base": 1, "result": 2, "judge": "1.3"})
        self.assertEqual(rep["rating"], {A: 0.15, B: 0.1, C: 0.05})
        self.assertEqual(rep["weight"], {A: 1.5, B: 1.0, C: 0.75})
        self.assertEqual(rep["record"], {A: 0.55, B: 0.45})  # the old rule, client jobs only
        self.assertEqual(rep["average"], {A: 0.75, B: 0.0})
        self.assertEqual(rep["lane"], dict.fromkeys((A, B, C), "improvement"))
        self.assertIs(rep["lanes_open"], False)
        self.assertEqual(rep["constants"], json.loads(json.dumps(CONSTANTS)))
        self.assertEqual(
            [(c["job"], c["event"]) for c in rep["changes"] if c["job"] in ("late", "t1")],
            [("late", "approved"), ("t1", "improvement_pending"), ("t1", "improvement_accepted")],
        )
        self.assertTrue(all(NOW - 7 * DAY <= c["at_ms"] <= NOW for c in rep["changes"]))
        self.assertEqual(
            rep["einstein"], {"version": __version__, "spec_version": __spec_version__}
        )

    def test_recompute_rebuilds_everything_from_the_report_alone(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rep = self.report(self.ledger(d))
            path = next(Path(d).glob("hour-*.json"))
            out = recompute(json.loads(path.read_text()))
            self.assertEqual(
                (out["matches_published"], out["mismatches"], out["differences"]), (True, [], [])
            )
            self.assertEqual((out["rating"], out["weight"]), (rep["rating"], rep["weight"]))
            self.assertTrue(audit([path])["ok"])

    def test_recompute_lists_what_differs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.report(self.ledger(d))
            rep = json.loads(next(Path(d).glob("hour-*.json")).read_text())
        rep["rating"][A] = 0.9
        rep["weight"][B] = 7.0
        rep["changes"][0]["d_weight"] = 9.0
        next(j for j in rep["jobs"] if j["job"] == "t2")["w"] = 0.5
        out = recompute(rep)
        self.assertEqual(
            [m for m in out["mismatches"] if m.get("points") == "weight"],
            [{"job": "t2", "hotkey": C, "points": "weight", "published": 0.5, "recomputed": 0.25}],
        )
        self.assertIn(
            {"rating": True, "hotkey": A, "published": 0.9, "recomputed": 0.15}, out["differences"]
        )
        self.assertIn(
            {"weight": True, "hotkey": B, "published": 7.0, "recomputed": 1.0}, out["differences"]
        )
        self.assertTrue(any(x.get("change") == rep["changes"][0]["id"] for x in out["differences"]))

    def test_a_report_of_another_version_is_not_called_a_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.report(self.ledger(d))
            path = next(Path(d).glob("hour-*.json"))
            rep = json.loads(path.read_text())
            rep["einstein"] = {"version": "0.6.26", "spec_version": _spec("0.6.26")}
            rep["weight"] = {A: 9.0}  # the earlier rule's numbers
            path.write_bytes(json.dumps(rep, sort_keys=True, separators=(",", ":")).encode())
            out = audit([path])
            self.assertTrue(out["ok"])
            self.assertEqual(out["not_checked"], [str(path)])
        self.assertEqual(__spec_version__, _spec("0.6.26") + 1)


class TestFiveMinersReplay(unittest.TestCase):
    """The miners of before the lanes keep their history: stored outcomes are scored again by
    this rule on load, and their dated signals replay into changes. With 10 jobs or more in the
    window the rating is the points per job, unpadded: production."""

    def test_stored_history_replays_into_production_ratings(self) -> None:
        rng = random.Random(3)  # noqa: S311 -- a test's seed
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "state.sqlite")
            expect: dict[str, float] = {}
            sums: dict[str, float] = {}
            for hk in (A, B, C, D, E):
                total = 0.0
                for i in range(64):
                    ended = NOW - rng.randrange(1, 7 * DAY)
                    row: dict[str, Any] = {
                        "job": f"{hk[:4]}-{i}",
                        "hotkey": hk,
                        "ended_ms": ended,
                        "deliverable": i % 16 != 0,
                        "kind": "pass" if i % 16 else "gate",
                        "s": 0.123,  # an older rule's points: never kept
                        "parts": {"signal": "none"},
                        "signals": [],
                    }
                    if i % 16 and i % 9 == 0:
                        row["signals"] = [{"kind": "download", "at_ms": ended + 1000}]
                    total += 0.0 if not i % 16 else (1.0 if row["signals"] else 0.5)
                    store.put("outcome", f"{row['job']}/{hk}/{ended}", row)
                expect[hk], sums[hk] = round(total / 64, 6), total
            ledger = Ledger(store=store)
            ratings = ledger.ratings(NOW)
        self.assertEqual(ratings, expect)
        for hk, value in ratings.items():
            self.assertTrue(0.45 <= value <= 0.6, value)
            self.assertEqual(lane_of(rating_of(ratings, hk)), "production")
        # every outcome and every download is one dated change, and the weight is their sum
        changes = ledger.changes(NOW)
        self.assertEqual(len(changes), 5 * 64 + 5 * 7)
        self.assertEqual(ledger.weights(NOW), sums)


class TestTheFourSwitchStates(unittest.TestCase):
    """What the hourly vector is made over, for each state of ``launch_equal`` and
    ``lanes_open``: the list the daemon passes (:meth:`Desk.weighted`) and the rule."""

    def setUp(self) -> None:
        self.ledger = Ledger()
        self.clock = Clock()
        self.clock.t = NOW / 1000
        self.desk = Desk(
            FakePlatform(),
            self.ledger,
            registered=lambda: [A, B, C],
            clock=self.clock,
            key_approval="manual",
        )
        for hk in (A, B, C):
            self.desk.agent_key(hk)  # a pending request
            self.desk.approve(hk)
            self.desk.agent_key(hk)
        self.desk.miners[A].admitted = True
        self.desk.miners[B].admitted = True  # C is approved and keyed, never passed a test
        self.ledger.add(production("a1", A, NOW - DAY, sig("download", NOW - HOUR_MS)))
        self.ledger.add(production("b1", B, NOW - DAY))
        self.ledger.add(improvement("t1", C, NOW - DAY))

    def tearDown(self) -> None:
        self.desk.stop()

    def report(self) -> dict[str, Any]:
        return self.ledger.report(
            NOW,
            uids(A, B, C),
            owner_hotkey=OWNER,
            admitted=self.desk.weighted(),
            reachable=[A, B, C],
            launch_equal=self.desk.launch_equal,
            lanes_open=self.desk.lanes_open,
            eligible=self.desk.eligible(),
            proven=self.desk.proven(),
        )

    def test_launch_on_lanes_closed_is_equal_among_the_admitted(self) -> None:
        self.desk.set_launch_equal(True)
        rep = self.report()
        self.assertEqual(rep["admitted"], sorted([A, B]))
        self.assertEqual(rep["vector_by_hotkey"], {A: 65_535, B: 65_535})
        self.assertTrue(rep["rule"].startswith("launch"))
        # the new numbers are published all the same
        self.assertEqual(rep["weight"], {A: 1.0, B: 0.5, C: 0.25})
        self.assertEqual(set(rep["lane"]), {A, B, C})

    def test_launch_off_lanes_closed_is_history_by_the_dated_weight_among_the_admitted(
        self,
    ) -> None:
        rep = self.report()
        self.assertEqual((rep["rule"], rep["admitted"]), ("history", sorted([A, B])))
        self.assertEqual(rep["vector_by_hotkey"], {A: 65_535, B: 32_768})

    def test_lanes_open_is_history_among_every_eligible_miner(self) -> None:
        self.desk.set_lanes_open(True)
        rep = self.report()
        self.assertEqual((rep["rule"], rep["admitted"]), ("history", sorted([A, B, C])))
        self.assertEqual(rep["vector_by_hotkey"], {A: 65_535, B: 32_768, C: 16_384})
        self.assertIs(rep["lanes_open"], True)
        self.assertTrue(recompute(rep)["matches_published"])

    def test_both_on_is_refused(self) -> None:
        self.desk.set_launch_equal(True)
        out = self.desk.set_lanes_open(True)  # opening the lanes turns the launch rule off
        self.assertEqual(out, {"lanes_open": True, "was": False, "launch_equal": False})
        with self.assertRaises(DeskError) as e:
            self.desk.set_launch_equal(True)
        self.assertEqual((e.exception.status, e.exception.code), (409, "lanes_open"))
        self.assertFalse(self.desk.launch_equal)
        self.desk.set_lanes_open(False)
        self.assertEqual(self.desk.set_launch_equal(True), {"launch_equal": True, "was": False})

    def test_a_blocked_miner_is_not_eligible(self) -> None:
        self.desk.set_lanes_open(True)
        self.desk.miners[C].blocked_until_ms = NOW + HOUR_MS
        self.assertEqual(sorted(self.desk.weighted()), sorted([A, B]))

    def test_genesis_counts_a_judged_improvement_as_proven(self) -> None:
        self.assertEqual(self.desk.proven(), [])
        self.desk.improved[C] = 1
        self.desk.delivered[A] = 2
        self.assertEqual(self.desk.proven(), sorted([A, C]))


if __name__ == "__main__":
    unittest.main()
