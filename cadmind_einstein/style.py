"""A style round's gate: the delivered project kept the settled contract it began from.

A style round (cadmind-watt ADR-020) reshapes a delivered device's printed parts and nothing else.
When the platform opens one, it runs ``watt style begin`` on a copy of the delivered project and
keeps the answer -- the *baseline*: the contract digest and where every bought and world item sits
-- with the job; the claim carries it (``style.baseline``). At the hand-over the validator's own
``watt report`` writes ``build/report.json``, whose ``contract`` block must show:

1. the same contract digest (both computed by the same Watt release);
2. the same layout keys;
3. every item within 0.001 mm and 0.001 degree of where it was delivered (the angle of the relative
   rotation; rounded floats are never compared for equality).

Otherwise the attempt fails the gate with the first differences named. Pure: the caller hands in
the export's bytes and the baseline, so the reading is tested apart from any platform.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

ORIGIN_MM = 0.001
ANGLE_DEG = 0.001
SAID = 3
MAX_REPORT_BYTES = 20_000_000


def _rotation(v: Sequence[float]) -> list[list[float]]:
    x, y, z = (math.radians(float(c)) for c in v)
    angle = math.sqrt(x * x + y * y + z * z)
    if angle < 1e-15:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    kx, ky, kz = x / angle, y / angle, z / angle
    c, s = math.cos(angle), math.sin(angle)
    t = 1 - c
    return [
        [t * kx * kx + c, t * kx * ky - s * kz, t * kx * kz + s * ky],
        [t * kx * ky + s * kz, t * ky * ky + c, t * ky * kz - s * kx],
        [t * kx * kz - s * ky, t * ky * kz + s * kx, t * kz * kz + c],
    ]


def relative_angle_deg(base: Sequence[float], now: Sequence[float]) -> float:
    """The angle of R(base)^T R(now), in degrees (rotation vectors in degrees)."""
    a, b = _rotation(base), _rotation(now)
    trace = sum(a[k][i] * b[k][i] for i in range(3) for k in range(3))
    return math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1) / 2))))


def _row(value: Any) -> list[float] | None:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 6:
        return None
    try:
        return [float(c) for c in value]
    except (TypeError, ValueError):
        return None


def compare(baseline: Mapping[str, Any], contract: Mapping[str, Any] | None) -> list[str]:
    """Every difference between the baseline (``contract``, ``layout``) and a report's
    ``contract`` (``digest``, ``layout``); empty when it was kept."""
    if not isinstance(contract, Mapping) or not contract.get("digest"):
        return ["the delivered report carries no contract (watt report never ran on it)"]
    out: list[str] = []
    if str(contract.get("digest")) != str(baseline.get("contract") or ""):
        out.append(
            f"the settled contract changed: {str(contract.get('digest'))[:19]} now,"
            f" {str(baseline.get('contract') or '')[:19]} delivered"
        )
    was_raw, now_raw = baseline.get("layout"), contract.get("layout")
    was: Mapping[str, Any] = was_raw if isinstance(was_raw, Mapping) else {}
    now: Mapping[str, Any] = now_raw if isinstance(now_raw, Mapping) else {}
    out += [f"{key} is gone" for key in sorted(set(was) - set(now))]
    out += [f"{key} is new" for key in sorted(set(now) - set(was))]
    for key in sorted(set(was) & set(now)):
        a, b = _row(was[key]), _row(now[key])
        if a is None or b is None:
            out.append(f"{key}: its pose cannot be read")
            continue
        moved = math.dist(a[:3], b[:3])
        if moved > ORIGIN_MM:
            out.append(f"{key} sits {moved:.3f} mm from where it was delivered")
            continue
        turned = relative_angle_deg(a[3:], b[3:])
        if turned > ANGLE_DEG:
            out.append(f"{key} is turned {turned:.3f} deg from how it was delivered")
    return out


def report_of(export: bytes) -> dict[str, Any] | None:
    """The export's ``build/report.json`` (under its one top directory or not), or None."""
    with zipfile.ZipFile(io.BytesIO(export)) as z:
        for info in z.infolist():
            name = info.filename
            rel = name if name.startswith("build/") or "/" not in name else name.split("/", 1)[1]
            if rel == "build/report.json" and info.file_size <= MAX_REPORT_BYTES:
                parsed = json.loads(z.read(info))
                return parsed if isinstance(parsed, dict) else None
    return None


def check(export: bytes, baseline: Mapping[str, Any] | None) -> str:
    """Why a style round's delivery did not keep its contract ('' when it did, or when the job
    is not a style round: no baseline)."""
    if not isinstance(baseline, Mapping) or not baseline.get("contract"):
        return ""
    report = report_of(export)
    found = compare(baseline, (report or {}).get("contract"))
    if not found:
        return ""
    more = len(found) - SAID
    return (
        "a style round changes printed shapes only, and this one changed the settled contract: "
        + "; ".join(found[:SAID])
        + (f"; and {more} more" if more > 0 else "")
    )
