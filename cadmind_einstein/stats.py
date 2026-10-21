"""Miner stats (DESIGN.md section 12, docs/MINER.md "Your stats"): what affects a miner's weight,
per hotkey and per coldkey, over 24 hours, 7 days (the scoring window) and its lifetime.

**One source of truth.** Everything here is read from the desk (assignments, endings, the abuse
counts), the ledger (each scored job with its parts and the client's signals) and the votes; the
platform's dashboard only renders this answer. The same numbers are in the public hourly reports.

**Lifetime.** The ledger forgets a job after the 7-day window, so a daily rollup (``miner_day``
rows, one per UTC day and hotkey) keeps what the window held: written at every hour close for each
day that still lies wholly inside the window (all its jobs are still there), frozen after. The
desk's own tallies (assignments, rejections, overturned rejections, gate failures, copies,
deliveries, unfinished rounds) are lifetime counts already.

**Imports** (devices other projects imported) do not exist yet: the answer says so.

**Privacy.** A miner sees its own jobs' signals as kinds and counts, and the votes with their
evidence; never a client's identity, a flag's words, or anything of another miner but the public
leaderboard (:func:`network`).

``cadmind-miner-stats`` (:func:`main`) asks the validator for the calling hotkey's stats with a
``btauth/1``-signed request and checks the validator's signature on the answer.
"""

from __future__ import annotations

import calendar
import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .scoring import HALF_LIFE_H, HOUR_MS, TRUST, W_CLIENT, W_IMMEDIATE, WINDOW_H, JobOutcome

DAY_MS = 24 * HOUR_MS
JOBS_SHOWN = 100
DAYS_SHOWN = 30
MAX_HOTKEYS = 20

COUNTERS = (
    "assigned",
    "delivered",
    "not_finished",
    "gate_failed",
    "rejected",
    "overturned",
    "timeouts",
    "copies",
    "rated",
    "stars_mean",
    "downloaded",
    "changes",
    "reruns",
    "refunds",
    "refunds_quality",
    "voted",
    "vote_mean",
    "score_sum",
)
_SUMS = ("stars_sum", "vote_sum")
"""what a rollup row carries besides the counters, for the means"""
ROLLED = (
    "delivered",
    "timeouts",
    "rated",
    "downloaded",
    "changes",
    "reruns",
    "refunds",
    "refunds_quality",
    "voted",
    "score_sum",
    *_SUMS,
)
"""what a day's rollup row counts, from the ledger's outcomes"""

FORMULA = {
    "text": "s = 0.4*I + 0.6*(w*C + (1-w)*B);  B = u*V + (1-u)*I;  I = 0.5 + 0.3*E + 0.2*T",
    "weight": "weight = sum over your jobs in the last 7 days of 0.5^(age_h/24) * s",
    "share": "share = your weight / the sum of every weighted miner's weight (the vector)",
    "parts": {
        "I": "immediate: passed the Watt gate (0.5), efficiency E (0.3), on time T (0.2)",
        "E": "efficiency against the bucket's medians (1 when the bucket has fewer than 5 jobs)",
        "C": "the client's signal: stars (s-1)/4; a download 0.75; each change or re-run -0.25;"
        " a quality refund counts as one star",
        "w": "how much C counts: the client's trust (paid 1, free 0.1), half for a download",
        "V": "the validators' quality vote on the rubric (0..1)",
        "u": "how much V counts (0 while votes are in shadow)",
        "B": "the baseline the client's signal moves from",
    },
}


def day_of(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def day_start(day: str) -> int:
    return calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000


def _empty() -> dict[str, float]:
    return {k: 0 for k in (*COUNTERS, *_SUMS) if k not in ("stars_mean", "vote_mean")}


def _signals(o: JobOutcome) -> dict[str, Any]:
    sig = o.signals or []
    stars = [int(x["stars"]) for x in sig if x.get("kind") == "stars" and x.get("stars")]
    return {
        "stars": stars,
        "downloaded": any(x.get("kind") == "download" for x in sig),
        "changes": sum(1 for x in sig if x.get("kind") == "change"),
        "reruns": sum(1 for x in sig if x.get("kind") == "rerun"),
        "refunds": sum(1 for x in sig if x.get("kind") == "refund"),
        "refunds_quality": sum(
            1 for x in sig if x.get("kind") == "refund" and x.get("quality") is True
        ),
    }


def outcome_row(o: JobOutcome) -> dict[str, float]:
    """One scored outcome's contribution to a day's rollup."""
    r = _empty()
    if o.kind == "timeout":
        r["timeouts"] = 1
    if not o.passed:
        return r
    r["delivered"] = 1
    r["score_sum"] = float(o.s)
    sig = _signals(o)
    if sig["stars"]:
        r["rated"], r["stars_sum"] = 1, sig["stars"][-1]
    r["downloaded"] = 1 if sig["downloaded"] else 0
    for k in ("changes", "reruns", "refunds", "refunds_quality"):
        r[k] = sig[k]
    if o.vote and o.vote.get("V") is not None:
        r["voted"], r["vote_sum"] = 1, float(o.vote["V"])
    return r


def _add(into: dict[str, float], row: Mapping[str, Any]) -> None:
    for k, v in row.items():
        if k in into and isinstance(v, int | float):
            into[k] += v


def _finish(c: dict[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {k: c.get(k, 0) for k in COUNTERS if k not in ("stars_mean", "vote_mean")}
    out["stars_mean"] = round(c["stars_sum"] / c["rated"], 3) if c.get("rated") else None
    out["vote_mean"] = round(c["vote_sum"] / c["voted"], 4) if c.get("voted") else None
    out["score_sum"] = round(float(out["score_sum"]), 6)
    return {k: out[k] for k in COUNTERS}


def rollup(outcomes: Iterable[JobOutcome], now_ms: int) -> dict[str, dict[str, float]]:
    """``{"<day>/<hotkey>": row}`` for every day still wholly inside the window."""
    cutoff = now_ms - WINDOW_H * HOUR_MS
    rows: dict[str, dict[str, float]] = {}
    for o in outcomes:
        day = day_of(o.ended_ms)
        if day_start(day) < cutoff:
            continue
        _add(rows.setdefault(f"{day}/{o.hotkey}", _empty()), outcome_row(o))
    return rows


class MinerStats:
    """The stats of the miners the desk knows, as :func:`doc` answers them."""

    def __init__(
        self,
        desk: Any,
        *,
        reviewer: Any = None,
        uids: Callable[[], Mapping[str, int]] = dict,
        coldkeys: Callable[[], Mapping[str, str]] = dict,
        current: Callable[[], Mapping[str, Any]] | None = None,
        votes_mode: str = "shadow",
    ) -> None:
        self.desk, self.reviewer = desk, reviewer
        self.uids, self.coldkeys = uids, coldkeys
        self.current = current or self._current
        self.votes_mode = votes_mode
        self._frozen: dict[str, dict[str, float]] = {}

    # -- the rollup ----------------------------------------------------------------------------

    def _store(self) -> Any:
        return getattr(self.desk, "store", None)

    def _current(self) -> Mapping[str, Any]:
        store = self._store()
        if store is not None:
            return store.get("meta", "current_vector") or {}
        return getattr(self.desk.ledger, "current", {}) or {}

    def roll(self, now_ms: int | None = None) -> int:
        """At the hour's close: rewrite the rows of the days still wholly in the window (frozen
        after). Answers how many rows were written."""
        now = self.desk.now_ms() if now_ms is None else now_ms
        fresh = rollup(self.desk.ledger.outcomes, now)
        cutoff = now - WINDOW_H * HOUR_MS
        store = self._store()
        old = store.items("miner_day") if store is not None else dict(self._frozen)
        for key in old:
            if day_start(key.split("/", 1)[0]) >= cutoff and key not in fresh:
                if store is not None:
                    store.delete("miner_day", key)
                self._frozen.pop(key, None)
        for key, row in fresh.items():
            if store is not None:
                store.put("miner_day", key, row)
            self._frozen[key] = row
        return len(fresh)

    def _days(self, now_ms: int) -> dict[str, dict[str, float]]:
        """Every rollup row: the frozen ones as stored, the window's ones fresh."""
        store = self._store()
        stored = store.items("miner_day") if store is not None else dict(self._frozen)
        cutoff = now_ms - WINDOW_H * HOUR_MS
        rows = {k: v for k, v in stored.items() if day_start(k.split("/", 1)[0]) < cutoff}
        rows.update(rollup(self.desk.ledger.outcomes, now_ms))
        return rows

    # -- counters -------------------------------------------------------------------------------

    def _window(self, hotkey: str, since_ms: int, outcomes: list[JobOutcome]) -> dict[str, Any]:
        c = _empty()
        with self.desk._lock:  # noqa: SLF001 -- the desk's own lock, read only
            for j in self.desk.jobs.values():
                last = j.attempts[-1] if j.attempts else None
                for a in j.attempts:
                    if a.hotkey != hotkey or a.assigned_ms < since_ms:
                        continue
                    if j.kind != "client":
                        continue
                    c["assigned"] += 1
                    if a.kind == "reject":
                        c["rejected"] += 1
                    elif a.kind == "gate":
                        c["gate_failed"] += 1
                    elif a.kind == "copy":
                        c["copies"] += 1
                    if (
                        a is last
                        and a.kind in ("gate", "timeout")
                        and j.result.get("label") == "not finished"
                    ):
                        c["not_finished"] += 1
        passed_by_other = {o.job for o in outcomes if o.passed and o.hotkey != hotkey}
        for o in outcomes:
            if o.hotkey != hotkey or o.ended_ms < since_ms:
                continue
            _add(c, outcome_row(o))
            if o.kind == "reject" and o.job in passed_by_other:
                c["overturned"] += 1
        return _finish(c)

    def _lifetime(
        self, hotkeys: Iterable[str], days: Mapping[str, Mapping[str, float]]
    ) -> dict[str, Any]:
        hks = set(hotkeys)
        c = _empty()
        for key, row in days.items():
            if key.split("/", 1)[1] in hks:
                _add(c, {k: row.get(k, 0) for k in ROLLED})
        d = self.desk
        with d._lock:  # noqa: SLF001
            for hk in hks:
                c["assigned"] += d.assigned.get(hk, 0)
                c["rejected"] += d.rejected.get(hk, 0)
                c["overturned"] += d.overturned.get(hk, 0)
                c["gate_failed"] += d.failed.get(hk, 0)
                c["copies"] += d.copied.get(hk, 0)
                c["not_finished"] += getattr(d, "not_finished", {}).get(hk, 0)
        return _finish(c)

    # -- the answer -----------------------------------------------------------------------------

    def formula(self) -> dict[str, Any]:
        return {
            **FORMULA,
            "votes_mode": self.votes_mode,
            "constants": {
                "half_life_h": HALF_LIFE_H,
                "window_h": WINDOW_H,
                "immediate": W_IMMEDIATE,
                "client": W_CLIENT,
                "trust": TRUST,
            },
        }

    def docs(self, hotkeys: Iterable[str]) -> dict[str, Any]:
        """``{at_ms, formula, miners: {hotkey: doc}}`` for up to :data:`MAX_HOTKEYS` hotkeys."""
        wanted = [h for h in dict.fromkeys(hotkeys) if h][:MAX_HOTKEYS]
        now = self.desk.now_ms()
        window, weight, quality = self.desk.ledger.snapshot(now)
        days = self._days(now)
        uids, cold = dict(self.uids() or {}), dict(self.coldkeys() or {})
        cur = dict(self.current() or {})
        vec = {str(k): float(v) for k, v in (cur.get("by_hotkey") or {}).items()}
        total = sum(vec.values())
        ranked = sorted((h for h, w in weight.items() if w > 0), key=lambda h: -weight[h])
        return {
            "at_ms": now,
            "formula": self.formula(),
            "miners": {
                hk: self._doc(
                    hk, now, window, weight, quality, days, uids, cold, vec, total, ranked
                )
                for hk in wanted
            },
        }

    def _doc(
        self,
        hk: str,
        now: int,
        window: list[JobOutcome],
        weight: Mapping[str, float],
        quality: Mapping[str, float],
        days: Mapping[str, Mapping[str, float]],
        uids: Mapping[str, int],
        cold: Mapping[str, str],
        vec: Mapping[str, float],
        total: float,
        ranked: list[str],
    ) -> dict[str, Any]:
        d = self.desk
        m = d.miners.get(hk)
        ck = str(cold.get(hk) or "")
        siblings = {h for h, c in cold.items() if ck and c == ck} | {hk}
        pol = d.abuse
        seen = d.abuse_seen.get(hk, {})

        def since_strike(name: str, count: int) -> int:
            return max(0, count - int(seen.get(name, 0)))

        mine = sorted((o for o in window if o.hotkey == hk), key=lambda o: -o.ended_ms)
        return {
            "hotkey": hk,
            "coldkey": ck,
            "uid": uids.get(hk),
            "registered": hk in uids,
            "admitted": bool(m and m.admitted),
            "last_seen_ms": int(m.last_seen_ms) if m else 0,
            "blocked_until_ms": int(m.blocked_until_ms) if m else 0,
            "standing": {
                "weight": round(float(weight.get(hk, 0.0)), 6),
                "quality": round(float(quality.get(hk, 0.0)), 6),
                "rank": ranked.index(hk) + 1 if hk in ranked else None,
                "miners": len(ranked),
                "share": round(vec.get(hk, 0.0) / total, 6) if total else 0.0,
                "rule": str(self.current().get("rule") or ""),
            },
            "abuse": {
                "copies": {
                    "count": since_strike("copies", d.copied.get(hk, 0)),
                    "limit": pol.copies,
                },
                "suspensions": {
                    "count": since_strike("suspensions", d.suspensions.get(hk, 0)),
                    "limit": pol.suspensions,
                },
                "admission_failures": {
                    "count": since_strike("admission_failures", d.admission_failed.get(hk, 0)),
                    "limit": pol.admission_failures,
                },
                "overturned": {
                    "count": since_strike("overturned", d.overturned.get(hk, 0)),
                    "limit": pol.overturned,
                },
                "strikes": d.strikes.get(hk, 0),
            },
            "windows": {
                "24h": self._window(hk, now - DAY_MS, window),
                "7d": self._window(hk, now - WINDOW_H * HOUR_MS, window),
                "lifetime": self._lifetime([hk], days),
            },
            "coldkey_lifetime": self._lifetime(siblings, days),
            "days": self._daily(hk, days, now),
            "jobs": [self._job(o) for o in mine[:JOBS_SHOWN]],
            "failures": d.failures(hk),
            "imports": {"available": False, "note": "imports: not available yet"},
        }

    @staticmethod
    def _daily(hk: str, days: Mapping[str, Mapping[str, float]], now: int) -> list[dict[str, Any]]:
        out = []
        for i in range(DAYS_SHOWN - 1, -1, -1):
            day = day_of(now - i * DAY_MS)
            row = days.get(f"{day}/{hk}") or {}
            out.append(
                {
                    "day": day,
                    "delivered": int(row.get("delivered", 0)),
                    "score_sum": round(float(row.get("score_sum", 0.0)), 6),
                }
            )
        return out

    def _job(self, o: JobOutcome) -> dict[str, Any]:
        parts = {
            k: o.parts.get(k, -1.0 if k in ("C", "V") else 0.0)
            for k in ("I", "E", "C", "w", "V", "u", "B")
        }
        votes: dict[str, Any] = {
            "V": None,
            "u": 0.0,
            "u_applied": 0.0,
            "shadow": True,
            "count": 0,
            "items": [],
        }
        if self.reviewer is not None and o.passed:
            from .votes import full_votes  # noqa: PLC0415

            votes = {
                **self.reviewer.standing_of(o.job),
                "items": full_votes(self.reviewer.votes_of(o.job)),
            }
        return {
            "job": o.job,
            "ended_ms": o.ended_ms,
            "effort": o.effort,
            "kind": o.kind,
            "on_time": o.on_time,
            "s": o.s,
            "parts": parts,
            "signals": _signals(o),
            "votes": votes,
        }

    def network(self) -> dict[str, Any]:
        """The public leaderboard: every weighted or admitted miner by hotkey -- its share of the
        current vector, weight, quality, 7-day deliveries, mean stars and mean vote."""
        now = self.desk.now_ms()
        window, weight, quality = self.desk.ledger.snapshot(now)
        cur = dict(self.current() or {})
        vec = {str(k): float(v) for k, v in (cur.get("by_hotkey") or {}).items()}
        total = sum(vec.values())
        uids = dict(self.uids() or {})
        hks = {h for h, w in weight.items() if w > 0} | set(self.desk.admitted()) | set(vec)
        rows = []
        for hk in hks:
            mine = [o for o in window if o.hotkey == hk and o.passed]
            stars = [_signals(o)["stars"][-1] for o in mine if _signals(o)["stars"]]
            votes = [float(o.vote["V"]) for o in mine if o.vote and o.vote.get("V") is not None]
            rows.append(
                {
                    "hotkey": hk,
                    "uid": uids.get(hk),
                    "share": round(vec.get(hk, 0.0) / total, 6) if total else 0.0,
                    "weight": round(float(weight.get(hk, 0.0)), 6),
                    "quality": round(float(quality.get(hk, 0.0)), 6),
                    "delivered_7d": len(mine),
                    "stars_mean": round(statistics.fmean(stars), 3) if stars else None,
                    "vote_mean": round(statistics.fmean(votes), 4) if votes else None,
                }
            )
        rows.sort(key=lambda r: (-r["share"], -r["weight"], r["hotkey"]))
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        return {
            "at_ms": now,
            "hour": cur.get("hour"),
            "rule": cur.get("rule", ""),
            "miners": rows,
        }


def by_day(rows: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, float]]:
    """Rollup rows summed per day (every hotkey), for an operator's look."""
    out: dict[str, dict[str, float]] = defaultdict(_empty)
    for key, row in rows.items():
        _add(out[key.split("/", 1)[0]], row)
    return dict(out)


def main(argv: list[str] | None = None) -> int:
    """``cadmind-miner-stats --validator URL --validator-hotkey SS58 --wallet.name W
    --wallet.hotkey H``: your hotkey's stats, the validator's signature on them checked."""
    import argparse  # noqa: PLC0415

    from .cli import add_wallet_args, wallet_kwargs  # noqa: PLC0415
    from .fake_miner import signed  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="cadmind-miner-stats")
    ap.add_argument("--validator", required=True, help="the einstein validator's base URL")
    ap.add_argument("--validator-hotkey", required=True, help="its hotkey (ss58)")
    add_wallet_args(ap)
    ap.add_argument("--uri", default="", help="a development key (//Alice) instead of a wallet")
    ap.add_argument("--json", action="store_true", help="the whole answer as JSON")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if a.uri:
        from bittensor.sp_core import Keypair  # noqa: PLC0415

        key: Any = Keypair.create_from_uri(a.uri)
    else:
        import bittensor as bt  # noqa: PLC0415

        key = bt.Wallet(**wallet_kwargs(a)).hotkey
    code, out = signed(key, a.validator, "/v1/miners/stats", {}, a.validator_hotkey)
    if code != 200:
        why = out.get("error") if isinstance(out, dict) else out
        print(f"error {code}: {why}", file=sys.stderr)  # noqa: T201 -- a CLI
        return 1
    if a.json:
        print(json.dumps(out, indent=1))  # noqa: T201 -- a CLI
        return 0
    s, w = out["standing"], out["windows"]
    print(  # noqa: T201
        f"{out['hotkey']}  rank {s['rank']} of {s['miners']}  share {100 * s['share']:.2f}%  "
        f"weight {s['weight']:.4f}  quality {s['quality']:.3f}\n"
        + "\n".join(
            f"{name:>8}: delivered {c['delivered']}, assigned {c['assigned']}, not finished "
            f"{c['not_finished']}, gate failed {c['gate_failed']}, rejected {c['rejected']}, "
            f"stars {c['stars_mean']}, vote {c['vote_mean']}, refunds {c['refunds']}"
            for name, c in w.items()
        )
        + f"\nimports: {out['imports']['note']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
