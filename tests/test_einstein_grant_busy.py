"""A revision's grant forks the delivered workspace, which may still have a run going on it (the
platform's own checks after a delivery). Watt refuses that fork as busy: the grant is offered again,
with back-off, until the job's own deadline, and never counted toward the refusals that end a job
whose workspace can never be made. Those still end the job after three."""

from __future__ import annotations

import unittest

from cadmind_einstein.desk import (
    GRANT_BUSY_RETRY_MAX_S,
    GRANT_PERMANENT_TRIES,
    WORKSPACE_BUSY,
    Desk,
)
from cadmind_einstein.platform import PlatformError
from tests.test_einstein import A, B, Clock
from tests.test_einstein_grant_refusals import Bench, QuotaPlatform

OLD_WORDS = "a run is going on ag-0123456789abcdef0123: fork it when it has ended"
"""how an older Watt refuses the fork (a bare 400), as the platform passes it on"""


def grants(p: QuotaPlatform) -> int:
    return sum(1 for _, call, _ in p.calls if call == "grant")


class TestBusyIsAWait(unittest.TestCase):
    def test_a_busy_source_is_its_own_kind_on_any_status(self) -> None:
        for e in (
            PlatformError("a run is going on it", 409, WORKSPACE_BUSY),  # Watt's code, passed on
            PlatformError(OLD_WORDS, 409, "busy"),  # a platform that maps Watt's 409 to busy
            PlatformError(OLD_WORDS, 400),  # an older Watt: the words alone
            PlatformError("`watt style` is still running on this workspace: wait for it", 400),
        ):
            self.assertEqual(Desk._grant_refusal(e), "busy", str(e))
        self.assertEqual(Desk._grant_refusal(PlatformError("no such workspace", 404)), "permanent")
        gone = PlatformError("this grant's seed workspace is gone", 404, "seed_not_found")
        self.assertEqual(Desk._grant_refusal(gone), "permanent")
        self.assertEqual(Desk._grant_refusal(PlatformError("bad gateway", 502)), "transient")

    def test_busy_refusals_are_offered_again_until_the_workspace_is_free(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock)
        b = Bench(p, clock, [A, B], deadline_s=3600)
        try:
            p.ws["ag-delivered"] = {
                "files": {},
                "deliverable": True,
                "frozen": True,
                "compute_ms": 0,
            }
            p.grant_error = PlatformError(OLD_WORDS, 400)  # the refusal of the incident
            b.desk.create({"job": "v1", "brief": "", "seed": "ag-delivered"})
            b.run(30 * 60, step=10)
            st = b.desk.status("v1")
            self.assertEqual(st["state"], "queued")  # never ended as "can never be made"
            self.assertGreater(grants(p), GRANT_PERMANENT_TRIES)
            # the back-off is capped short: the run it waits for ends in minutes
            self.assertGreaterEqual(grants(p), (30 * 60) // GRANT_BUSY_RETRY_MAX_S - 3)
            self.assertEqual(b.desk._grant_retry["v1"]["permanent"], 0)
            self.assertFalse(any(ln.startswith("ALERT") for ln in b.lines))
            said = [ln for ln in b.lines if "grant to" in ln]
            self.assertIn("busy: a wait, not counted", said[0])
            self.assertNotIn("permanent", said[0])
            p.grant_error = None  # the run on the delivered workspace has ended
            b.run(5 * 60, step=10)
            self.assertTrue(b.desk.status("v1")["attempts"])  # granted
            self.assertNotIn("v1", p.completed)
        finally:
            b.stop()

    def test_a_source_busy_until_the_jobs_deadline_ends_it_unscored(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock)
        b = Bench(p, clock, [A, B], deadline_s=1800)
        try:
            p.grant_error = PlatformError("a run is going on it", 409, WORKSPACE_BUSY)
            b.desk.create({"job": "v2", "brief": "", "seed": "ag-delivered"})
            b.run(20 * 60, step=10)
            self.assertEqual(b.desk.status("v2")["state"], "queued")
            b.run(20 * 60, step=10)
            st = b.desk.status("v2")
            self.assertEqual((st["state"], st["result"]["label"]), ("done", "didn't finish"))
            self.assertFalse(st["result"]["charge"])
            self.assertIn("stayed busy until the job's deadline", st["result"]["why"])
            self.assertEqual(p.completed["v2"]["label"], "didn't finish")  # the platform told
            self.assertEqual([o for o in b.desk.ledger.outcomes if o.job == "v2"], [])
            self.assertTrue(any(ln.startswith("ALERT: job v2") for ln in b.lines))
        finally:
            b.stop()

    def test_three_real_permanent_refusals_still_end_the_job_busy_ones_not_counted(self) -> None:
        clock = Clock()
        p = QuotaPlatform(clock)
        b = Bench(p, clock, [A, B])
        try:
            p.grant_error = PlatformError(OLD_WORDS, 400)
            b.desk.create({"job": "v3", "brief": "", "seed": "ag-delivered"})
            b.run(10 * 60, step=10)
            busy = grants(p)
            self.assertGreater(busy, GRANT_PERMANENT_TRIES)
            self.assertEqual(b.desk.status("v3")["state"], "queued")
            p.grant_error = PlatformError(
                "this grant's seed workspace is gone", 404, "seed_not_found"
            )
            b.run(60 * 60, step=10)
            st = b.desk.status("v3")
            self.assertEqual((st["state"], st["result"]["label"]), ("done", "didn't finish"))
            self.assertIn("cannot make this job's workspace", st["result"]["why"])
            self.assertEqual(grants(p) - busy, GRANT_PERMANENT_TRIES)
        finally:
            b.stop()


if __name__ == "__main__":
    unittest.main()
