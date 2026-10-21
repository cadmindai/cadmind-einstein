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
import time
from collections.abc import Callable, Iterable, Mapping
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
    bundle: str = ""
    """a review bundle kept on file, read instead of asking the platform for one: an item whose
    version the platform no longer serves (its client deleted the project) stays in the set"""

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
        for item in items:  # a kept bundle's path is relative to the set's own file
            if item.bundle and not Path(item.bundle).is_absolute():
                item.bundle = str(Path(path).parent / item.bundle)
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


def run(
    cset: CalibrationSet,
    platform: Any,
    model: Any,
    *,
    samples: int = 3,
    spend: Any = None,
    wait_s: float = 1800.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda _line: None,
) -> dict[str, Any]:
    """Review every reference and repeat item of ``cset`` with ``model`` from the platform's own
    bundles (``platform``: an :class:`~.platform.OperatorClient` with the queue key; asking for a
    bundle that is not made yet queues it, and it is waited for up to ``wait_s``), then judge the
    values. Brief swaps need a bundle made with another job's brief (not served yet): skipped and
    said. An item with a kept ``bundle`` file is reviewed from it; one the platform refuses (no
    such delivered version) gets no vote, and the run goes on. Answers ``{go, failed, items,
    votes, skipped, spend}``."""
    from .platform import PlatformError  # noqa: PLC0415
    from .review import (  # noqa: PLC0415
        MAX_BUNDLE_BYTES,
        Vote,
        ask_samples,
        read_bundle,
        rubric_digest,
    )

    values: dict[str, float | None] = {}
    votes: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for item in cset.items:
        if item.kind == "brief_swap":
            skipped[item.id] = (
                "a brief swap needs a swapped bundle, which the platform does not make yet"
            )
            continue
        if item.bundle:
            bundle = read_bundle(Path(item.bundle).read_bytes())
        else:
            end = clock() + wait_s
            got: dict[str, Any] = {}
            try:
                while True:
                    got = platform.review_bundle(item.job, "ours")
                    if got.get("ready") or got.get("state") in ("failed", "off") or clock() >= end:
                        break
                    log(f"{item.id}: bundle {got.get('state') or 'not ready'}; waiting")
                    sleep(min(120.0, max(10.0, float(got.get("retry_s") or 30))))
            except PlatformError as e:  # this item has no vote (a no-go); the others are still run
                got = {"ready": False, "state": f"refused ({e})"}
            if not got.get("ready"):
                values[item.id] = None
                votes[item.id] = {"error": f"no bundle: {got.get('state') or 'not ready'}"}
                log(f"{item.id}: no bundle: {got.get('state') or 'not ready'}")
                continue
            bundle = read_bundle(platform.fetch_bundle(str(got["bundle_url"]), MAX_BUNDLE_BYTES))
            if got.get("image_set") and got["image_set"] != bundle.image_set:
                raise ValueError(f"{item.id}: the bundle is not the image set the platform named")
        got_samples, failed = ask_samples(model, bundle, samples=samples, spend=spend)
        vote = Vote(
            review=item.job,
            image_set=bundle.image_set,
            rubric=rubric_digest(),
            model=str(getattr(model, "name", "")),
            validator="",
            samples=[x.as_json() for x in got_samples],
            at_ms=int(time.time() * 1000),
        )
        values[item.id] = vote.value if len(got_samples) >= min(2, samples) else None
        votes[item.id] = {**vote.as_json(), "unavailable": failed}
        n = len(got_samples)
        log(f"{item.id}: {values[item.id]} ({n} sample(s); {len(failed)} unavailable)")
        for why in sorted(set(failed)):
            log(f"{item.id}: unavailable: {why}")
    judged = CalibrationSet([i for i in cset.items if i.id not in skipped], cset.version)
    out = go_no_go(judged, values)
    out["votes"] = votes
    out["skipped"] = skipped
    out["spend"] = spend.today() if spend is not None else {}
    return out


def calibrate_main(argv: list[str] | None = None) -> int:
    """``cadmind-calibrate SET.json --platform URL --queue-key-file K --model PROVIDER:MODEL
    --key-file M --out RESULTS.json``: the calibration run (PAID: one vote per item, three model
    calls each). Prints the go/no-go and writes every vote to ``--out``."""
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from .daemon import read_key  # noqa: PLC0415
    from .models import Prices, Spend, model_from  # noqa: PLC0415
    from .platform import OperatorClient  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="cadmind-calibrate")
    ap.add_argument("set", type=Path, help="the calibration set (private)")
    ap.add_argument("--platform", required=True, help="the platform's base URL")
    ap.add_argument(
        "--queue-url", default="", help="the job queue's base URL (default: --platform)"
    )
    ap.add_argument("--queue-key-file", type=Path, required=True, help="the validator's queue key")
    ap.add_argument("--model", required=True, help="anthropic:<model> or openai:<model>")
    ap.add_argument("--key-file", type=Path, required=True, help="the model's API key (0600)")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--daily-usd", type=float, default=10.0, help="stop past this spend (0: none)")
    ap.add_argument("--usd-per-mtok-in", type=float, default=0.0)
    ap.add_argument("--usd-per-mtok-out", type=float, default=0.0)
    ap.add_argument("--usd-per-mtok-cached", type=float, default=0.0)
    ap.add_argument("--wait-s", type=float, default=1800.0, help="how long to wait for a bundle")
    ap.add_argument("--out", type=Path, required=True, help="where every vote is written")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    def say(line: str) -> None:
        print(f"[cadmind-calibrate] {line}", file=sys.stderr, flush=True)  # noqa: T201 -- a CLI

    queue_key = read_key(a.queue_key_file)
    platform = OperatorClient(
        a.platform, queue_key, queue_key=queue_key, queue_url=a.queue_url, log=say
    )
    model = model_from(a.model, read_key(a.key_file), base_url=a.base_url, effort=a.effort)
    spend = Spend(
        a.daily_usd,
        Prices(a.usd_per_mtok_in, a.usd_per_mtok_out, a.usd_per_mtok_cached),
        alert=say,
    )
    out = run(
        CalibrationSet.load(a.set),
        platform,
        model,
        samples=a.samples,
        spend=spend,
        wait_s=a.wait_s,
        log=say,
    )
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1), encoding="utf-8")
    summary = {k: out[k] for k in ("go", "failed", "skipped", "spend")}
    summary["items"] = [{k: r[k] for k in ("id", "value", "passed", "why")} for r in out["items"]]
    print(json.dumps(summary, indent=1))  # noqa: T201 -- a CLI
    return 0 if out["go"] else 1


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
