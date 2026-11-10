"""The per-job score and the weights (DESIGN.md section 5). **Clients are the graders**: no model
decides a score, a weight or a route.

**A job's points.** A client job delivered by a builder that passes the validator's Watt gate
scores:

- **0.5** while the client has done nothing;
- **1** when it was downloaded (in the app or by API) or the client rated it 3, 4 or 5 stars;
- **0** when the client rated it 1 or 2 stars, or a refund was granted for its quality.

**A verdict outranks a download.** The client's latest rating or quality refund decides, whenever
there is one: a 1-star rating then a later 5-star one is 1; a 1-star rating then any download is
still 0. A download counts only while the client has given no verdict, so nobody's download can
lift a job its client rated down or was refunded for. Any other signal (a change request, a refund
not for quality) is recorded, not scored. Free and paid clients count the same (a ``trust`` the
platform may still send is ignored).

**Only a delivery has points.** An outcome scores as a delivery only when it is one (``kind``
``pass``): whatever its other facts say, any other kind scores 0.

An attempt that does not deliver -- failed the gate, timed out, handed back (rejected), a copy, a
delivery the platform refused for what the builder did -- scores **0**, at once.

**Weights, every hour, never burned.** A job counts for 7 days from its end (its delivery), flat:
no decay. A miner's **weight** is the sum of its job points over the last 7 days; the vector is
proportional to it over registered, admitted miners seen in the last 24 hours. Its **average**
(points / jobs in the window) is shown on the stats pages. The router reads its **record**
(:func:`record`): that average, with a miner of fewer than :data:`RECORD_JOBS` jobs counted as if
the missing ones were unrated deliveries, so one lucky job does not put a newcomer first. Signals
arrive late and change the job's points when they do; the next hourly vector reads the current
points.

The validators' votes on deliveries (:mod:`.votes`) are recorded and published for calibration and
practice comparisons; they never reach a score, a weight or the router.

An hour with no work or no success still emits, from that history. **Genesis** -- no admitted
miner has any points in the window -- follows ``genesis``:

- ``delivered`` (the default): equal per COLDKEY among admitted, registered, reachable miners that
  have delivered at least one scored client job (ever), each coldkey's share split among its
  hotkeys. Admission alone (a cheap, unscored entry test) earns nothing, so hotkeys registered to
  farm genesis get no weight; before the first delivery nothing is weighted.
- ``coldkey``: equal per coldkey among admitted, registered, reachable miners (hotkeys of one owner
  share one share).
- ``hotkey``: equal per hotkey among them (an earlier rule; a report without ``genesis`` was made
  by it).

**Launch** (the validator's saved switch: ``--launch-equal on|off``, or the operator's route): while
it is on, equal weight per hotkey among the admitted, registered, reachable miners -- each one
onboarded by hand -- whatever their history; off, the rules above. Each report records whether it
was on (``launch_equal``) and recompute follows that record.

**The subnet owner's hotkey is never weighted** by these rules: weighting it burns miner
emission. The one exception is the validator's opt-in burn mode (``--burn-when-empty``, in
:mod:`.chain`): an hour whose vector is empty -- no admitted miner -- sends all weight to the
owner's uid instead of nothing, so the validator keeps setting weights before the first miner.
**Only admitted hotkeys are ever weighted**, by every rule and fallback: a revoked, disabled or
deregistered miner is out from the next vector on. **The vector is never empty while an
admitted miner exists**: an hour whose rules weight nobody sends the last non-empty vector again
(its still-admitted hotkeys), or, before any, equal shares per coldkey among the admitted,
reachable miners (under Null consensus an empty vector would spread the miners' emission over
every registered UID). Nothing is emitted only when no miner is admitted at all (or, in burn
mode, all of it goes to the owner's uid).

Every hour a report holds the window's jobs with their facts and signals, and the vector;
:func:`recompute` rebuilds the scores and the vector from a report alone (``python -m
cadmind_einstein.scoring REPORT``).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import __spec_version__, __version__

WINDOW_H = 7 * 24
HOUR_MS = 3_600_000
UNRATED, APPROVED, DISAPPROVED = 0.5, 1.0, 0.0
"""a delivered job's points: no client signal yet; a download or 3-5 stars; 1-2 stars or a refund
granted for its quality"""


@dataclass
class JobOutcome:
    job: str
    hotkey: str
    ended_ms: int
    accepted: bool = True
    on_time: bool = True
    deliverable: bool = False
    stage: str = "none"
    wall_s: float = 0.0
    """the attempt's wall time (the queue's ETA reads it; never scored)"""
    copy_of: str = ""
    kind: str = ""
    """pass | reject | gate | timeout | copy | refused"""
    signals: list[dict[str, Any]] = field(default_factory=list)
    """the client's, in the order they came: {"kind": download | stars | change | refund,
    "stars": 1-5, "quality": a refund for the delivery's quality, "by": "visitor" on a download
    by someone other than the job's client (recorded, never scored), "at_ms"}"""
    s: float = 0.0
    parts: dict[str, Any] = field(default_factory=dict)
    """what decided ``s``: ``{"signal": none | download | stars | refund}`` on a delivered job,
    ``{"failed": kind}`` on any other"""

    @property
    def passed(self) -> bool:
        """A delivery: a ``pass`` whose facts agree. Any other kind is not one, whatever its facts
        say (an attempt the validator could not judge, a copy whose flag was lifted)."""
        return self.kind == "pass" and self.accepted and self.deliverable and not self.copy_of


def client_points(signals: Iterable[Mapping[str, Any]]) -> tuple[float, str]:
    """A delivered job's points and the signal that decided them: the client's LATEST verdict (a
    star rating or a quality refund) when there is one; else :data:`APPROVED` for a download; else
    :data:`UNRATED`. A download never outranks a verdict, whenever it came. Only the job's own
    client scores: a signal by anyone else (``by``) is recorded and never read here, or a builder
    could lift its own delivery from a second account."""
    verdict: tuple[float, str] | None = None
    downloaded = False
    for x in signals:
        if x.get("by"):
            continue
        kind = x.get("kind")
        if kind == "download":
            downloaded = True
        elif kind == "stars" and 1 <= int(x.get("stars") or 0) <= 5:
            verdict = (APPROVED if int(x["stars"]) >= 3 else DISAPPROVED), "stars"
        elif kind == "refund" and x.get("quality") is True:
            verdict = DISAPPROVED, "refund"
    if verdict is not None:
        return verdict
    return (APPROVED, "download") if downloaded else (UNRATED, "none")


def score(o: JobOutcome) -> float:
    """Fill ``o.s`` and ``o.parts`` from its facts and its client's signals."""
    if o.passed:
        o.s, by = client_points(o.signals)
        o.parts = {"signal": by}
    else:
        o.s, o.parts = 0.0, {"failed": o.kind or "failed"}
    return o.s


def score_all(jobs: Iterable[JobOutcome]) -> list[JobOutcome]:
    """Score every job (each on its own facts and signals: no job's points depend on another's)."""
    jobs = list(jobs)
    for o in jobs:
        score(o)
    return jobs


def in_window(o: JobOutcome, now_ms: int) -> bool:
    """A job counts for :data:`WINDOW_H` from its end, flat."""
    return now_ms - o.ended_ms <= WINDOW_H * HOUR_MS


def standing(jobs: Iterable[JobOutcome], now_ms: int) -> tuple[dict[str, float], dict[str, float]]:
    """(weight, average) per miner over the window's jobs: the SUM of its job points, and that sum
    over its number of jobs (what the stats pages show; nothing routes by it)."""
    total: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for o in jobs:
        if not in_window(o, now_ms):
            continue
        total[o.hotkey] += o.s
        count[o.hotkey] += 1
    average = {h: round(total[h] / n, 6) for h, n in count.items() if n}
    return {h: round(v, 6) for h, v in total.items()}, average


RECORD_JOBS = 10
"""a miner with fewer jobs in the window is read as if the missing ones were unrated deliveries"""
TRIAL_EVERY = 5
"""one client job in this many goes to the miner that waited longest, whatever its record"""


def record(jobs: Iterable[JobOutcome], now_ms: int) -> dict[str, float]:
    """What the router ranks miners by: points per job over the window, each miner padded to
    :data:`RECORD_JOBS` jobs with :data:`UNRATED` ones. A miner with no job in the window is not in
    the answer: its record is :data:`UNRATED` (:func:`record_of`)."""
    total: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for o in jobs:
        if in_window(o, now_ms):
            total[o.hotkey] += o.s
            count[o.hotkey] += 1
    return {
        h: round((total[h] + UNRATED * max(0, RECORD_JOBS - n)) / max(n, RECORD_JOBS), 6)
        for h, n in count.items()
    }


def record_of(records: Mapping[str, float], hotkey: str) -> float:
    return float(records.get(hotkey, UNRATED))


CONSTANTS = {
    "window_h": WINDOW_H,
    "points": {"unrated": UNRATED, "approved": APPROVED, "disapproved": DISAPPROVED},
    "router": {"record_jobs": RECORD_JOBS, "trial_every": TRIAL_EVERY},
}
"""the rule's numbers, published in every report"""

GENESIS = ("delivered", "coldkey", "hotkey")
LAUNCH_RULE = "launch: equal among admitted miners"


def vector(
    weight: Mapping[str, float],
    uid_by_hotkey: Mapping[str, int],
    *,
    owner_hotkey: str,
    admitted: Iterable[str],
    reachable: Iterable[str],
    genesis: str = "delivered",
    coldkeys: Mapping[str, str] | None = None,
    proven: Iterable[str] = (),
    last: Mapping[str, float] | None = None,
    launch_equal: bool = False,
) -> tuple[list[tuple[int, int]], str]:
    """The weights and the rule that made them. Never the subnet owner's hotkey; never burned.
    ``coldkeys`` (hotkey -> coldkey; a hotkey with none stands alone) and ``proven`` (hotkeys with
    a delivered client job) serve the genesis rule only.

    **Never empty while anyone could be weighted** (DESIGN.md section 10): an empty vector under
    Null consensus spreads the miners' emission evenly over every registered UID. So when the
    rules above weight nobody, ``last`` -- the last non-empty vector, hotkey -> weight -- is sent
    again (its hotkeys still registered); before any vector was ever set, equal shares per coldkey
    among the admitted, reachable miners. Empty only with no admitted miner at all.

    Every rule and every fallback weights ADMITTED hotkeys only (registered, never the owner's):
    a miner whose admission was dropped -- its key revoked, disabled, deregistered -- is out of the
    vector from the next one on, its history and the last vector notwithstanding.

    **Launch** (``launch_equal``): equal weight per hotkey among the admitted, registered,
    reachable miners (each one onboarded by hand); history and the last vector are ignored; with
    none reachable, equal among the admitted ones (never empty). Off, the rules above apply."""
    if genesis not in GENESIS:
        raise ValueError(f"genesis is one of {GENESIS}")
    adm = {h for h in admitted if h in uid_by_hotkey and h != owner_hotkey}
    live = adm & set(reachable)
    if launch_equal:
        if live:
            return sorted((uid_by_hotkey[h], 65_535) for h in live), LAUNCH_RULE
        if adm:
            return (
                sorted((uid_by_hotkey[h], 65_535) for h in adm),
                f"{LAUNCH_RULE} (none reachable; never empty)",
            )
        return [], "no admitted miner: nothing to weight"

    def scaled(w: Mapping[str, float]) -> list[tuple[int, int]]:
        top = max(w.values())
        return sorted((uid_by_hotkey[h], max(1, round(65_535 * v / top))) for h, v in w.items())

    earned = {h: v for h, v in weight.items() if v > 0 and h in live}
    if earned:
        return scaled(earned), "history"
    if genesis == "hotkey" and live:
        return sorted(
            (uid_by_hotkey[h], 65_535) for h in live
        ), "genesis: equal among admitted, reachable miners"
    pool = live & set(proven) if genesis == "delivered" else live
    if pool:
        owner_of = {h: str((coldkeys or {}).get(h) or "") or f"hotkey:{h}" for h in pool}
        per = {ck: sum(1 for x in owner_of.values() if x == ck) for ck in owner_of.values()}
        what = "delivered miners" if genesis == "delivered" else "admitted, reachable miners"
        return sorted(
            (uid_by_hotkey[h], max(1, round(65_535 / per[owner_of[h]]))) for h in pool
        ), f"genesis: equal per coldkey among {what}"
    earned = {h: v for h, v in weight.items() if v > 0 and h in adm}
    if earned:
        return scaled(earned), "history (no admitted miner reachable)"
    kept = {
        h: float(v)
        for h, v in (last or {}).items()
        if float(v) > 0 and h in adm  # still admitted: a revoked miner is never sent again
    }
    if kept:
        return scaled(kept), "kept: the last non-empty vector, sent again (never empty)"
    if live:
        owner_of = {h: str((coldkeys or {}).get(h) or "") or f"hotkey:{h}" for h in live}
        per = {ck: sum(1 for x in owner_of.values() if x == ck) for ck in owner_of.values()}
        return (
            sorted((uid_by_hotkey[h], max(1, round(65_535 / per[owner_of[h]]))) for h in live),
            (
                "genesis: equal per coldkey among admitted, reachable miners"
                " (no delivery yet; never empty)"
            ),
        )
    return [], "no admitted miner: nothing to weight"


def report_bytes(rep: Mapping[str, Any]) -> bytes:
    """A report's published bytes: canonical JSON (sorted keys, no spaces). Its SHA-256 is what
    goes on chain, and every report names the previous report's, so the newest commitment
    authenticates the whole published history."""
    return json.dumps(rep, sort_keys=True, separators=(",", ":")).encode()


def report_order(path: Path) -> tuple[int, int]:
    """``hour-N.json`` sorts as (N, 0); a second report closed in the same hour, ``hour-N-K.json``,
    as (N, K)."""
    parts = path.stem.split("-")
    return int(parts[1]), int(parts[2]) if len(parts) > 2 else 0


def _key(o: JobOutcome) -> str:
    return f"{o.job}/{o.hotkey}/{o.ended_ms}"


def _public(o: JobOutcome) -> dict[str, Any]:
    """An outcome as a report publishes it."""
    return asdict(o)


def _row(o: JobOutcome) -> dict[str, Any]:
    return {k: v for k, v in _public(o).items() if k not in ("s", "parts")}


@dataclass
class Ledger:
    """Job outcomes and late client signals in; hourly snapshots out.

    With a :class:`~.store.Store` every outcome and signal is written as it lands (inside the
    desk's transaction when there is one), and a restart reloads them all -- mid-hour too. Without
    one (tests, recompute) a restart falls back to the newest report's window. Either way the
    loaded jobs are scored again by THIS rule (a stored outcome keeps its facts and signals, never
    its points), so no miner holds a mix of an older rule's points and this one's.
    """

    report_dir: Path | None = None
    hour_ms: int = HOUR_MS
    store: Any = None
    jobs: dict[str, JobOutcome] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.RLock)
    _last_vector: dict[str, float] = field(default_factory=dict)
    current: dict[str, Any] = field(default_factory=dict)
    """the newest report's vector by hotkey, its rule and hour (the stats' shares)"""

    def __post_init__(self) -> None:
        if self.store is not None:
            self._lock = self.store.lock
            for row in self.store.items("outcome").values():
                o = outcome_of(row)
                self.jobs[_key(o)] = o
            if self.jobs:
                score_all(self.jobs.values())
                return
        if self.report_dir is None or not self.report_dir.is_dir():
            return
        reports = sorted(self.report_dir.glob("hour-*.json"), key=report_order)
        if reports:
            newest = json.loads(reports[-1].read_text(encoding="utf-8"))
            for row in newest["jobs"]:
                o = outcome_of(row)
                self.jobs[_key(o)] = o
                self._save(o)
            self._last_vector = dict(newest.get("vector_by_hotkey") or {})
            score_all(self.jobs.values())

    def _save(self, o: JobOutcome) -> None:
        if self.store is not None:
            self.store.put("outcome", _key(o), _row(o))

    @property
    def outcomes(self) -> list[JobOutcome]:
        with self._lock:
            return list(self.jobs.values())

    def add(self, o: JobOutcome) -> float:
        with self._lock:
            self.jobs[_key(o)] = o
            self._save(o)
            return score(o)

    def signal(self, job: str, sig: Mapping[str, Any]) -> float | None:
        """A client's late signal on a delivered job (its passing attempt). None when the job is
        unknown."""
        with self._lock:
            for o in self.jobs.values():
                if o.job == job and o.passed:
                    o.signals.append({k: v for k, v in sig.items() if k != "trust"})
                    self._save(o)
                    return score(o)
        return None

    def withdraw(self, job: str) -> list[JobOutcome]:
        """Remove every outcome of ``job`` (a delivery the platform refused: nobody is scored on
        it); answers what was removed."""
        with self._lock:
            keys = [k for k, o in self.jobs.items() if o.job == job]
            gone = [self.jobs.pop(k) for k in keys]
            for k in keys:
                if self.store is not None:
                    self.store.delete("outcome", k)
            return gone

    def _window(self, now_ms: int) -> list[JobOutcome]:
        return [o for o in self.jobs.values() if in_window(o, now_ms)]

    def snapshot(self, now_ms: int) -> tuple[list[JobOutcome], dict[str, float], dict[str, float]]:
        with self._lock:
            window = score_all(self._window(now_ms))
            old = [k for k, o in self.jobs.items() if not in_window(o, now_ms)]
            for key in old:
                del self.jobs[key]
                if self.store is not None:
                    self.store.delete("outcome", key)
            weight, average = standing(window, now_ms)
            return window, weight, average

    def records(self, now_ms: int) -> dict[str, float]:
        """Each miner's record now (:func:`record`): what the router ranks by."""
        with self._lock:
            return record(score_all(self._window(now_ms)), now_ms)

    def last_vector(self) -> dict[str, float]:
        """The last non-empty vector, hotkey -> weight ({} before the first): what an hour that
        would weight nobody sends again (:func:`vector`)."""
        if self.store is not None:
            return dict(self.store.get("meta", "last_vector") or {})
        return dict(self._last_vector)

    def last_sha256(self) -> str:
        """The newest published report's SHA-256 ('' before the first)."""
        if self.store is not None:
            return str(self.store.get("meta", "report_sha256", ""))
        if self.report_dir is None or not self.report_dir.is_dir():
            return ""
        sums = sorted(self.report_dir.glob("hour-*.sha256"), key=report_order)
        return sums[-1].read_text().split()[0] if sums else ""

    def report(
        self,
        now_ms: int,
        uid_by_hotkey: Mapping[str, int],
        *,
        owner_hotkey: str,
        admitted: Iterable[str],
        reachable: Iterable[str],
        chain: Mapping[str, Any] | None = None,
        watch: Mapping[str, Any] | None = None,
        genesis: str = "delivered",
        coldkeys: Mapping[str, str] | None = None,
        proven: Iterable[str] = (),
        votes: Mapping[str, Any] | None = None,
        launch_equal: bool = False,
    ) -> dict[str, Any]:
        """The hour's report, written as its canonical bytes (``hour-N.json``) beside its SHA-256
        (``hour-N.sha256``), both atomically. ``chain`` (the block, our stake share, the weights
        path) and ``watch`` (the desk's observations: copy similarities, endings and
        admissions per coldkey, disables) ride along for readers; recompute needs neither. The
        genesis rule, the admitted miners' coldkeys and the ``proven`` hotkeys are published:
        recompute reads them."""
        window, weight, average = self.snapshot(now_ms)
        adm, reach = sorted(admitted), sorted(reachable)
        cold = {h: str((coldkeys or {}).get(h) or "") for h in adm}
        prov = sorted(set(proven) & set(adm))
        last = self.last_vector()
        vec, rule = vector(
            weight,
            uid_by_hotkey,
            owner_hotkey=owner_hotkey,
            admitted=adm,
            reachable=reach,
            genesis=genesis,
            coldkeys=cold,
            proven=prov,
            last=last,
            launch_equal=launch_equal,
        )
        hotkey_of = {u: h for h, u in uid_by_hotkey.items()}
        by_hotkey = {hotkey_of[u]: w for u, w in vec if u in hotkey_of}
        rep = {
            "hour": now_ms // self.hour_ms,
            "at_ms": now_ms,
            "prev_sha256": self.last_sha256(),
            "jobs": [_public(o) for o in window],
            "weight": weight,
            "average": average,
            "uids": dict(uid_by_hotkey),
            "owner": owner_hotkey,
            "admitted": adm,
            "reachable": reach,
            "genesis": genesis,
            "launch_equal": bool(launch_equal),
            "coldkeys": cold,
            "proven": prov,
            "vector": [list(x) for x in vec],
            "vector_by_hotkey": by_hotkey,
            "last_vector": last,
            "rule": rule,
            "chain": dict(chain or {}),
            "watch": dict(watch or {}),
            "votes": dict(votes or {}),
            "einstein": {"version": __version__, "spec_version": __spec_version__},
            "constants": CONSTANTS,
        }
        raw = report_bytes(rep)
        sha = hashlib.sha256(raw).hexdigest()
        if self.report_dir is not None:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            name, k = f"hour-{rep['hour']}", 0
            while (self.report_dir / f"{name}.json").exists():  # closed twice in one hour
                k += 1
                name = f"hour-{rep['hour']}-{k}"
            _atomic_write(self.report_dir / f"{name}.json", raw)
            _atomic_write(self.report_dir / f"{name}.sha256", f"{sha}  {name}.json\n".encode())
        if self.store is not None:
            self.store.put("meta", "report_sha256", sha)
        self.current = {"by_hotkey": by_hotkey, "rule": rule, "hour": rep["hour"]}
        if self.store is not None:
            self.store.put("meta", "current_vector", self.current)
        if by_hotkey:
            self._last_vector = dict(by_hotkey)
            if self.store is not None:
                self.store.put("meta", "last_vector", by_hotkey)
        rep["sha256"] = sha  # not part of the hashed bytes: the caller's handle on them
        return rep


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


_FACTS = frozenset(f.name for f in fields(JobOutcome)) - {"s", "parts"}
"""what an outcome is rebuilt from: an older row's other keys (its effort, size, compute, vote,
remix credit) are dropped, as its points are"""


def outcome_of(row: Mapping[str, Any]) -> JobOutcome:
    """An outcome from its stored or published row (unscored: :func:`score` fills ``s``)."""
    o = JobOutcome(**{k: v for k, v in row.items() if k in _FACTS})
    o.signals = [{k: v for k, v in dict(x).items() if k != "trust"} for x in o.signals or []]
    return o


def recompute(report: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild every job's score and the vector from one published report; list any job that
    differs."""
    jobs = score_all(outcome_of(r) for r in report["jobs"])
    mismatches = [
        {"job": o.job, "hotkey": o.hotkey, "published": r["s"], "recomputed": o.s}
        for o, r in zip(jobs, report["jobs"], strict=True)
        if abs(o.s - float(r["s"])) > 1e-6
    ]
    weight, _ = standing(jobs, int(report["at_ms"]))
    vec, rule = vector(
        weight,
        report["uids"],
        owner_hotkey=report["owner"],
        admitted=report["admitted"],
        reachable=report["reachable"],
        genesis=str(report.get("genesis") or "hotkey"),
        coldkeys=report.get("coldkeys") or {},
        proven=report.get("proven") or (),
        last=report.get("last_vector") or {},
        launch_equal=report.get("launch_equal") is True,
    )
    same = [list(x) for x in vec] == [list(x) for x in report["vector"]]
    return {"vector": vec, "rule": rule, "matches_published": same, "mismatches": mismatches}


def _hex(value: Any) -> str:
    """A committed hash as lowercase hex: ``0x...`` text, bytes, or a list of byte values."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)) and all(isinstance(b, int) for b in value):
        try:
            return bytes(value).hex()
        except ValueError:
            return ""
    text = str(value or "").strip().lower().removeprefix("0x")
    return text if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text) else ""


def commitment_sha256(c: Any) -> str:
    """The SHA-256 in a commitment as the chain answers it: the ``Sha256`` field the validator
    commits (:func:`.chain.commitment_call`; it reads ``fields=[{'Sha256': '0x...'}]`` with an
    empty ``data``), else a ``sha256:<hex>`` word in its text ``data`` (the older form); '' when
    neither."""
    fields = c.get("fields") if isinstance(c, Mapping) else getattr(c, "fields", None)
    for f in fields or []:
        if isinstance(f, Mapping):
            for k, v in f.items():
                if str(k).lower() == "sha256" and _hex(v):
                    return _hex(v)
    data = c.get("data") if isinstance(c, Mapping) else getattr(c, "data", "")
    for part in str(data or "").split():
        if part.startswith("sha256:"):
            return part.split(":", 1)[1].removeprefix("0x").lower()
    return ""


def onchain_sha256(network: str, netuid: int, hotkey: str, block: int | None = None) -> str:
    """The SHA-256 the validator's hotkey last committed on ``netuid`` (at ``block`` on an archive
    node), '' when none. A free read."""
    import bittensor as bt  # noqa: PLC0415 -- the auditor's optional chain read

    c = bt.Subtensor(network=network).identity.commitment(netuid, hotkey, block=block)
    return commitment_sha256(c) if c is not None else ""


def report_version(report: Mapping[str, Any]) -> str:
    """The einstein version that computed a report ('' for a report from before 0.5.3, which did
    not say)."""
    e = report.get("einstein")
    return str(e.get("version") or "") if isinstance(e, Mapping) else ""


def check_votes(votes: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The published votes that do not hold: a signature that is not its validator's (over the
    published samples' hash), or a value that is not what its published scores give -- ``V_MAP``
    of their lower median (vote/2, the device judge), or rubric 3's weighted mean of the scores,
    each capped by its published cap (vote/1, before einstein 0.5.0). An unsigned vote (a local
    stack) is not checked."""
    from .review import published_value_holds, verify_vote  # noqa: PLC0415 -- the auditor's

    out: list[dict[str, Any]] = []
    for item in votes.get("items") or []:
        why = ""
        if item.get("sig") and not verify_vote(item):
            why = "signature"
        if not published_value_holds(item):
            why = why or "value"
        if why:
            out.append({"job": item.get("job"), "validator": item.get("validator"), "why": why})
    return out


def audit(paths: list[Path], *, expect_sha256: str = "") -> dict[str, Any]:
    """Recompute every report and check the hash chain: each report's ``prev_sha256`` is the SHA-256
    of the report before it, and the newest one's SHA-256 is ``expect_sha256`` (what is on chain)
    when given.

    The scoring rule can change between releases, so a report is recomputed only by the version
    that made it. A report from another version whose numbers differ is not called a mismatch:
    its row says ``other_version`` (recompute it with that version) and it is listed in
    ``not_checked``; its hash and chain links are still checked."""
    out: list[dict[str, Any]] = []
    not_checked: list[str] = []
    ok, prev = True, ""
    for i, path in enumerate(paths):
        raw = path.read_bytes()
        rep = json.loads(raw)
        sha = hashlib.sha256(raw).hexdigest()
        row: dict[str, Any] = {"report": str(path), "sha256": sha, **recompute(rep)}
        row["canonical"] = report_bytes(rep) == raw
        made_by = report_version(rep)
        row["einstein"] = {"report": made_by or "before 0.5.3 (not stated)", "this": __version__}
        differs = not row["matches_published"] or bool(row["mismatches"])
        if made_by != __version__ and differs:
            row["other_version"] = (
                f"made by einstein {made_by or 'before 0.5.3'}, recomputed with {__version__}: the "
                "scoring rule may differ between versions, so this is not a mismatch; recompute "
                f"it with einstein {made_by or 'of that hour'} to check its numbers"
            )
            not_checked.append(str(path))
            differs = False
        bad_votes = check_votes(rep.get("votes") or {})
        if bad_votes:
            row["bad_votes"] = bad_votes
            ok = False
        if i and rep.get("prev_sha256") != prev:
            row["chain_break"] = {"prev_sha256": rep.get("prev_sha256"), "expected": prev}
            ok = False
        ok = ok and not differs and row["canonical"]
        prev = sha
        out.append(row)
    result: dict[str, Any] = {"reports": out, "ok": ok}
    if not_checked:
        result["not_checked"] = not_checked
    if expect_sha256:
        result["expected_sha256"] = expect_sha256
        result["newest_matches_expected"] = bool(out) and out[-1]["sha256"] == expect_sha256
        result["ok"] = ok and result["newest_matches_expected"]
    return result


def main(argv: list[str] | None = None) -> int:
    """``cadmind-recompute REPORT [REPORT ...]``: rebuild the scores and the vector of each
    published report, check the reports chain together in the order given, and (optionally) that
    the newest one is what the validator committed on chain."""
    import argparse  # noqa: PLC0415

    from .cli import add_subtensor_args, network_of  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="cadmind-recompute")
    ap.add_argument("reports", nargs="+", type=Path, help="hour-N.json, oldest first")
    ap.add_argument("--expect-sha256", default="", help="the newest report's committed hash")
    add_subtensor_args(ap, network_default="")  # empty: no chain read
    ap.add_argument("--validator-hotkey", default="")
    ap.add_argument("--block", type=int, default=None, help="read the commitment at this block")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    expect = a.expect_sha256.lower().removeprefix("0x")
    if network_of(a):
        expect = onchain_sha256(network_of(a), a.netuid, a.validator_hotkey, a.block)
        if not expect:
            print("no sha256 commitment found for that hotkey", file=sys.stderr)  # noqa: T201
            return 1
    out = audit(a.reports, expect_sha256=expect)
    print(json.dumps(out, indent=1))  # noqa: T201 -- a CLI
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
