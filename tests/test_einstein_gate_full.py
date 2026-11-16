"""The gate asks Watt for the full guard (``full``), so a bench not set to run one by itself
(``WATT_GUARD_FULL``) still proves a design in full; a platform or bench that refuses ``full`` is
asked the earlier way, once, and not asked for it again."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_einstein import A, B, C, Clock

from cadmind_einstein.desk import Desk
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.scoring import Ledger


class RecordingPlatform:
    def __init__(self, refuse_full: bool) -> None:
        self.refuse_full = refuse_full
        self.asked: list[dict[str, Any]] = []

    def run(self, ws: str, command: str, args: Any = None, **_: Any) -> dict[str, Any]:
        self.asked.append(dict(args or {}))
        if self.refuse_full and (args or {}).get("full"):
            raise PlatformError("run takes command, args ...", 400, "bad_request")
        return {"state": "done", "ok": True, "run": "r1"}


def desk_with(p: RecordingPlatform) -> Desk:
    return Desk(p, Ledger(hour_ms=3_600_000), registered=lambda: [A, B, C], clock=Clock())  # type: ignore[arg-type]


def test_the_guard_is_asked_in_full() -> None:
    p = RecordingPlatform(refuse_full=False)
    desk = desk_with(p)
    desk._guard_run("fork", False, lambda _r: None, threading.Event())
    assert p.asked == [{"mutate": True, "full": True}]
    desk.stop()


def test_a_full_record_is_asked_for_its_files_only() -> None:
    p = RecordingPlatform(refuse_full=False)
    desk = desk_with(p)
    desk._guard_run("fork", True, lambda _r: None, threading.Event())
    assert p.asked == [{}]
    desk.stop()


def test_a_refused_full_is_asked_the_earlier_way_and_not_again() -> None:
    p = RecordingPlatform(refuse_full=True)
    desk = desk_with(p)
    out = desk._guard_run("fork", False, lambda _r: None, threading.Event())
    assert out["ok"] is True
    desk._guard_run("fork", False, lambda _r: None, threading.Event())
    assert p.asked == [{"mutate": True, "full": True}, {"mutate": True}, {"mutate": True}]
    desk.stop()


def test_another_refusal_is_not_hidden() -> None:
    class Busy(RecordingPlatform):
        def run(self, ws: str, command: str, args: Any = None, **_: Any) -> dict[str, Any]:
            raise PlatformError("busy", 409, "run_in_progress")

    desk = desk_with(Busy(refuse_full=False))
    try:
        desk._guard_run("fork", False, lambda _r: None, threading.Event())
    except PlatformError as e:
        assert e.status == 409
    else:
        raise AssertionError("a 409 must reach the gate")
    assert desk._full_refused is False
    desk.stop()


def test_the_fake_miner_reads_a_run_beside_a_notice() -> None:
    from cadmind_einstein.fake_miner import answer_of

    run = '{"state": "done", "ok": true}'
    assert answer_of([run, "Note: your workspace expires in 2 days."]) == {
        "state": "done",
        "ok": True,
    }
    assert answer_of(["plain words"]) == {"text": "plain words"}
    assert answer_of(['{"a"', ": 1}"]) == {"a": 1}
