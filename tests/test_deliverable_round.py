"""The miner's deliverable check before the hand-over (a miner's ask, 2026-11-18)."""

from __future__ import annotations

import unittest
from typing import Any

from cadmind_einstein.agent_miner import Build, deliverable_round


class DeliverableRoundTests(unittest.TestCase):
    def _run(
        self,
        statuses: list[dict[str, Any] | None],
        *,
        text: str = "built\nDONE sha256:aa\n",
        said: str = "fixed\nDONE sha256:bb\n",
        left_s: float = 3600.0,
        stopped: bool = False,
    ) -> tuple[Build, list[str], list[str], int]:
        got = Build(text, "", timed_out=False, cancelled=False)
        notes: list[str] = []
        logs: list[str] = []
        seen = {"n": 0}

        def status() -> dict[str, Any] | None:
            st = statuses[min(seen["n"], len(statuses) - 1)]
            seen["n"] += 1
            return st

        def correct(message: str, budget_s: float) -> tuple[str, bool]:
            notes.append(message)
            return said, stopped

        made = deliverable_round(
            got,
            job="j1",
            ws="ag-1",
            deadline_ms=int((1000.0 + left_s) * 1000),
            status=status,
            correct=correct,
            cancelled=lambda: False,
            log=logs.append,
            clock=lambda: 1000.0,
        )
        return got, notes, logs, made

    def test_a_deliverable_workspace_is_handed_over_as_it_is(self) -> None:
        got, notes, logs, made = self._run([{"deliverable": True, "stage": "deliverable"}])
        self.assertEqual((made, notes, logs), (0, [], []))
        self.assertIn("sha256:aa", got.text)

    def test_done_on_an_undeliverable_workspace_sends_the_builder_back_once(self) -> None:
        got, notes, logs, made = self._run(
            [{"deliverable": False, "stage": "none"}, {"deliverable": True, "stage": "deliverable"}]
        )
        self.assertEqual(made, 1)
        self.assertIn("stage `none`", notes[0])
        self.assertIn("`watt check` lists the errors", notes[0])
        self.assertIn("about 57 minutes", notes[0])  # the hour less the submit's reserve
        self.assertIn("sha256:bb", got.text)
        self.assertIn("deliverable after 1 correction(s)", logs[-1])

    def test_it_runs_at_most_once(self) -> None:
        _, notes, logs, made = self._run([{"deliverable": False, "stage": "test"}])
        self.assertEqual(made, 1)
        self.assertIn("guard is not recorded", notes[0])
        self.assertIn("handed over as it is (stage test)", logs[-1])

    def test_no_time_left_hands_over(self) -> None:
        got, notes, _, made = self._run([{"deliverable": False, "stage": "none"}], left_s=200.0)
        self.assertEqual((made, notes), (0, []))
        self.assertIn("sha256:aa", got.text)

    def test_no_done_or_no_status_is_left_alone(self) -> None:
        _, notes, _, made = self._run([{"deliverable": False}], text="stopped midway\n")
        self.assertEqual((made, notes), (0, []))
        _, notes, logs, made = self._run([None])
        self.assertEqual((made, notes), (0, []))
        self.assertIn("skipped", logs[-1])

    def test_a_correction_without_done_keeps_the_first_text(self) -> None:
        got, _, _, made = self._run([{"deliverable": False, "stage": "none"}], said="ran out\n")
        self.assertEqual(made, 1)
        self.assertIn("sha256:aa", got.text)

    def test_a_stopped_correction_cancels(self) -> None:
        got, _, _, _ = self._run([{"deliverable": False, "stage": "none"}], stopped=True)
        self.assertTrue(got.cancelled)


if __name__ == "__main__":
    unittest.main()
