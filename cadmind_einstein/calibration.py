"""The calibration set (DESIGN.md section 13.6): known answers a reviewing model is held to.

**Items.** Three kinds, each one review of one delivered version:

- ``reference`` -- a device the operator scored by hand (``hand_score`` in [0, 1], e.g. 2/10 is
  0.2), or one known only to be bad (``expect_max``) or good (``expect_min``, e.g. a paid client's
  4-5 stars);
- ``brief_swap`` -- a real delivery reviewed against ANOTHER job's brief: any honest evaluator
  scores it low (below :data:`SWAP_MAX`);
- ``repeat`` -- a live item reviewed again later: it must land within :data:`REPEAT_NOISE` of its
  first value.

The operator's set is private (a file the validator reads, ``--calibration-file``): a validator
that knew which items are calibration items could answer them differently.

**Go/no-go** (:func:`go_no_go`). Before votes enter scores, our model must score every reference
device with a hand score within :data:`TOLERANCE` of it, every bound reference within its bound,
and every brief swap below :data:`SWAP_MAX`. An item without a value (the model gave no vote) is a
no-go. The same check picks our model and accepts a rubric change.

**Reliability** (:func:`reliability`): a validator's ``r = 1 - mean(|v - ref|)`` over its last
scored calibration items, the measure third-party votes will be weighted by.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("reference", "brief_swap", "repeat")
TOLERANCE = 0.15
SWAP_MAX = 0.35
SWAP_REF = 0.15
REPEAT_NOISE = 0.15
R_MIN = 0.6
R_NEW = 0.5


@dataclass
class Item:
    id: str
    kind: str
    job: str
    """the delivered job whose bundle is reviewed"""
    hand_score: float | None = None
    """a reference's hand score in [0, 1]; None when the review gave no number"""
    expect_max: float | None = None
    expect_min: float | None = None
    brief_of: str = ""
    """a brief swap: the job whose brief is shown instead"""
    first_value: float | None = None
    """a repeat: the value the first review gave"""
    note: str = ""
    source: str = ""
    """where the known answer comes from (a review file, a paid client's stars)"""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"a calibration item's kind is one of {KINDS}")
        for name in ("hand_score", "expect_max", "expect_min", "first_value"):
            v = getattr(self, name)
            if v is not None and not 0.0 <= float(v) <= 1.0:
                raise ValueError(f"{self.id}: {name} is in [0, 1]")

    def reference_value(self) -> float | None:
        """The value a reviewer is compared with (reliability), or None."""
        if self.kind == "reference":
            return self.hand_score
        if self.kind == "brief_swap":
            return SWAP_REF
        return self.first_value

    def check(self, value: float | None) -> tuple[bool | None, str]:
        """(passed, why) for one model value; passed None when the item states nothing checkable
        (a reference with neither a score nor a bound: reported, not judged)."""
        if value is None:
            return False, "no vote"
        if self.kind == "brief_swap":
            return value < SWAP_MAX, f"{value:.3f} {'<' if value < SWAP_MAX else '>='} {SWAP_MAX}"
        if self.kind == "repeat":
            if self.first_value is None:
                return None, "no first value"
            gap = abs(value - self.first_value)
            return gap <= REPEAT_NOISE, f"{gap:.3f} from the first value (noise {REPEAT_NOISE})"
        checks: list[tuple[bool, str]] = []
        if self.hand_score is not None:
            gap = abs(value - self.hand_score)
            checks.append(
                (gap <= TOLERANCE, f"{gap:.3f} from the hand score {self.hand_score} ({TOLERANCE})")
            )
        if self.expect_max is not None:
            checks.append((value <= self.expect_max, f"{value:.3f} vs at most {self.expect_max}"))
        if self.expect_min is not None:
            checks.append((value >= self.expect_min, f"{value:.3f} vs at least {self.expect_min}"))
        if not checks:
            return None, "no hand score or bound stated: reported only"
        return all(ok for ok, _ in checks), "; ".join(why for _, why in checks)


@dataclass
class CalibrationSet:
    items: list[Item]
    version: int = 1
    note: str = ""

    @classmethod
    def load(cls, path: Path) -> CalibrationSet:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        items = [Item(**row) for row in raw.get("items") or []]
        ids = [i.id for i in items]
        if len(set(ids)) != len(ids):
            raise ValueError("calibration item ids are unique")
        return cls(items, int(raw.get("version") or 1), str(raw.get("note") or ""))

    def dump(self) -> str:
        return json.dumps(
            {"version": self.version, "note": self.note, "items": [asdict(i) for i in self.items]},
            indent=1,
        )


def go_no_go(cset: CalibrationSet, values: Mapping[str, float | None]) -> dict[str, Any]:
    """The verdict on one model's values (item id -> its vote's value): ``go`` only when every
    checkable item passes."""
    rows = []
    for item in cset.items:
        v = values.get(item.id)
        ok, why = item.check(None if v is None else float(v))
        rows.append({"id": item.id, "kind": item.kind, "value": v, "passed": ok, "why": why})
    judged = [r for r in rows if r["passed"] is not None]
    failed = [r["id"] for r in judged if not r["passed"]]
    return {"go": bool(judged) and not failed, "failed": failed, "items": rows}


def reliability(pairs: Iterable[tuple[float, float]], *, scored: int = 0) -> float:
    """``r = 1 - mean(|v - ref|)`` over (value, reference) pairs; :data:`R_NEW` before ten."""
    gaps = [abs(float(v) - float(ref)) for v, ref in pairs]
    if len(gaps) < 10 and scored < 10:
        return R_NEW
    return round(max(0.0, 1.0 - statistics.fmean(gaps)), 6) if gaps else R_NEW


def main(argv: list[str] | None = None) -> int:
    """``python -m cadmind_einstein.calibration SET.json VALUES.json``: the go/no-go of a model's
    values (VALUES: item id -> value, e.g. collected from ``cadmind-review`` runs)."""
    import sys  # noqa: PLC0415

    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print(main.__doc__, file=sys.stderr)  # noqa: T201 -- a CLI
        return 2
    cset = CalibrationSet.load(Path(args[0]))
    values = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    out = go_no_go(cset, values)
    print(json.dumps(out, indent=1))  # noqa: T201 -- a CLI
    return 0 if out["go"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
