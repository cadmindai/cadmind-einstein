"""The reference miner's own review bundle (:mod:`.self_review`): the closest a miner can come, with
public code and its own agent key, to the bundle a validator judges.

**Why not the validator's own bundle.** The platform renders one review bundle per DELIVERED
version and serves it to validators only, through their engine key and a signed link
(:meth:`.platform.Platform.review_bundle`). A miner's agent key cannot ask for it, and a version
under self-review is not delivered yet. So the miner draws its own, from the workspace export
(``GET /api/agent/workspaces/<ws>/export`` with its agent key), and states the difference.

**What is the same.** The judge (:func:`.judge.judge`, the validators' version) reads it exactly as
it reads a validator's bundle (:func:`.judge.inputs_from_bundle`): the client's words verbatim
(``brief/brief.md``, ``brief/answer-*``, ``brief/change-*``, ``brief/making.*``) and the client's
photos (``brief/photo-*``); the same picture names and kinds the judge's prompt describes --
``view-*`` (eight outside views of the device: the six axes and two three-quarter views, printed
parts blue, bought parts orange, any other part green), ``world-*`` (the same views with the
world's bodies ghosted in magenta) and ``section-*`` (three cuts, the cut faces filled red) -- drawn
from the same source, the build's view mesh (``build/view/device.stl`` and ``device.json``).

**What differs.**

- The pictures are this module's own drawing (:func:`render_set`, orthographic, pure Python), not
  the platform's renderer: the same views and colours, not the same pixels (edge lines, framing
  and the section plane's choice are close, not identical).
- The fact sheet (:func:`facts_of`) is built from the project's own files (``build/report.json``,
  ``spec/world.toml``, the view mesh's bodies) with a smaller set of fields than the platform's: no
  function evidence from a fresh Watt check, and its requirements, assumptions and counts are
  read from the report as it stands. Its ``format`` says it is the miner's approximation.
- There is no studio render (the judge never sees one anyway).

Every string a builder wrote that enters the fact sheet is capped; the judge withholds
instruction-like text itself (:func:`.judge.scrub`).
"""

from __future__ import annotations

import io
import json
import math
import re
import struct
import tomllib
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SIZE = 1024
"""the longest side of each picture, in pixels (as the validators' pictures)"""
MARGIN = 16
MAX_TRIANGLES = 400_000
MAX_EXPORT_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TEXT = 20_000
MAX_STATEMENT = 300
MAX_ITEMS = 40
FACTS_FORMAT = "miner-self-review-facts/1"
CLIENT_FORMAT = "miner-self-review-client/1"

Vec = tuple[float, float, float]
Tri = tuple[Vec, Vec, Vec]

COLORS: dict[str, tuple[int, int, int]] = {
    "printed": (66, 118, 214),  # blue
    "hardware": (230, 132, 40),  # orange
    "other": (64, 168, 104),  # green
}
GHOST = (204, 48, 188)
"""the world's bodies: magenta"""
CUT = (186, 32, 40)
"""a section's cut face: red"""
EDGE = (28, 28, 34)
WHITE = (255, 255, 255)
GHOST_IN_FRONT, GHOST_BEHIND, GHOST_ALONE = 0.42, 0.14, 0.40
WORLD_GROW = 0.6
"""a world view frames the device grown by this share of its larger extent on each side, so the
device stays large however big the world item is"""
SECTION_SHARES = (0.3, 0.4, 0.5, 0.6, 0.7)
"""where along an axis a section may cut; the one that crosses the most of the device wins"""

_S3 = 1 / math.sqrt(3)
VIEWS: tuple[tuple[str, Vec, Vec, str], ...] = (
    # (name, the direction toward the camera, the world direction up on the image, words)
    ("iso", (_S3, -_S3, _S3), (0.0, 0.0, 1.0), "three-quarter view from front-right, above"),
    ("+x", (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), "from +x (right side)"),
    ("-x", (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0), "from -x (left side)"),
    ("+y", (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), "from +y (back)"),
    ("-y", (0.0, -1.0, 0.0), (0.0, 0.0, 1.0), "from -y (front)"),
    ("+z", (0.0, 0.0, 1.0), (0.0, 1.0, 0.0), "from +z (top)"),
    ("-z", (0.0, 0.0, -1.0), (0.0, 1.0, 0.0), "from -z (bottom)"),
    ("iso-below", (-_S3, _S3, -_S3), (0.0, 0.0, 1.0), "three-quarter view from back-left, below"),
)
SECTION_UP: dict[int, Vec] = {0: (0.0, 0.0, 1.0), 1: (0.0, 0.0, 1.0), 2: (0.0, 1.0, 0.0)}
AXES = "xyz"
PHOTO = re.compile(r"^brief/photo-[^/]+\.(jpe?g|png|webp)$", re.IGNORECASE)


class BundleError(ValueError):
    """The export holds no project that can be drawn."""


@dataclass
class MinerBundle:
    """What :func:`.judge.inputs_from_bundle` reads: ``manifest["images"]`` (``{name, kind,
    view, why?}``), ``images`` (name -> PNG), ``client``, ``facts`` and ``photos``."""

    manifest: dict[str, Any]
    images: dict[str, bytes]
    client: dict[str, Any]
    facts: dict[str, Any]
    photos: dict[str, bytes] = field(default_factory=dict)


# -- vectors ---------------------------------------------------------------------------------------


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def _basis(toward: Vec, up: Vec) -> tuple[Vec, Vec, Vec]:
    """(right, up, toward) of a camera looking along ``-toward``."""
    t = _unit(toward)
    r = _unit(_cross(up, t))
    return r, _cross(t, r), t


# -- the mesh --------------------------------------------------------------------------------------


def triangles_of(stl: bytes) -> list[Tri]:
    """A binary (or ASCII) STL's triangles."""
    if len(stl) >= 84:
        (n,) = struct.unpack_from("<I", stl, 80)
        if 84 + 50 * n == len(stl):
            if n > MAX_TRIANGLES:
                raise BundleError(f"the view mesh has {n} triangles (at most {MAX_TRIANGLES})")
            out: list[Tri] = []
            for i in range(n):
                v = struct.unpack_from("<9f", stl, 84 + 50 * i + 12)
                out.append(((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])))
            return out
    text = stl.decode("ascii", errors="replace")
    pts = [
        tuple(float(x) for x in m.groups())
        for m in re.finditer(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    ]
    if len(pts) // 3 > MAX_TRIANGLES:
        raise BundleError(f"the view mesh has over {MAX_TRIANGLES} triangles")
    return [(pts[i], pts[i + 1], pts[i + 2]) for i in range(0, len(pts) - 2, 3)]  # type: ignore[misc]


def categories_of(count: int, bodies: Sequence[Mapping[str, Any]]) -> list[str]:
    """Each triangle's category from ``device.json``'s bodies (``tri_start``/``tri_count``):
    ``world`` (role world), ``printed``, ``hardware`` or ``other``."""
    cats = ["other"] * count
    for b in bodies:
        if not isinstance(b, Mapping):
            continue
        role, kind = str(b.get("role") or ""), str(b.get("kind") or "")
        cat = (
            "world"
            if role == "world"
            else "hardware"
            if "hardware" in (role, kind)
            else "printed"
            if kind == "printed"
            else "other"
        )
        try:
            start, n = int(b.get("tri_start") or 0), int(b.get("tri_count") or 0)
        except (TypeError, ValueError):
            continue
        for i in range(max(0, start), min(count, start + max(0, n))):
            cats[i] = cat
    return cats


def bounds(tris: Sequence[Tri]) -> tuple[Vec, Vec]:
    xs = [p[0] for t in tris for p in t]
    ys = [p[1] for t in tris for p in t]
    zs = [p[2] for t in tris for p in t]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


# -- drawing ---------------------------------------------------------------------------------------


class _Frame:
    """An orthographic camera fitted to a box in its own image plane, ``SIZE`` on the longer
    side."""

    def __init__(self, basis: tuple[Vec, Vec, Vec], box: tuple[float, float, float, float]):
        self.basis = basis
        x0, x1, y0, y1 = box
        w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        self.scale = (SIZE - 2 * MARGIN) / max(w, h)
        self.width = max(8, min(SIZE, math.ceil(w * self.scale) + 2 * MARGIN))
        self.height = max(8, min(SIZE, math.ceil(h * self.scale) + 2 * MARGIN))
        self.x0, self.y1 = x0, y1

    def project(self, p: Vec) -> tuple[float, float, float]:
        r, u, t = self.basis
        return (
            (_dot(p, r) - self.x0) * self.scale + MARGIN,
            (self.y1 - _dot(p, u)) * self.scale + MARGIN,
            _dot(p, t),
        )


def _box(basis: tuple[Vec, Vec, Vec], points: Sequence[Vec]) -> tuple[float, float, float, float]:
    xs = [_dot(p, basis[0]) for p in points]
    ys = [_dot(p, basis[1]) for p in points]
    return min(xs), max(xs), min(ys), max(ys)


def _world_box(
    basis: tuple[Vec, Vec, Vec], device: Sequence[Vec], every: Sequence[Vec]
) -> tuple[float, float, float, float]:
    dx0, dx1, dy0, dy1 = _box(basis, device)
    ax0, ax1, ay0, ay1 = _box(basis, every)
    grow = max(dx1 - dx0, dy1 - dy0, 1e-6) * WORLD_GROW
    return max(ax0, dx0 - grow), min(ax1, dx1 + grow), max(ay0, dy0 - grow), min(ay1, dy1 + grow)


NEAR = -math.inf


@dataclass
class _Layer:
    depth: list[float]
    shade: list[float]
    cat: list[str]
    back: bytearray


def _raster(
    tris: Sequence[Tri], cats: Sequence[str], frame: _Frame, clip: float | None = None
) -> _Layer:
    """A z-buffer of ``tris``: per pixel the nearest surface's depth (larger is nearer), its
    shade, category and whether it faces away. With ``clip``, everything nearer than ``clip`` is
    cut away (a section)."""
    w, h = frame.width, frame.height
    n = w * h
    layer = _Layer([NEAR] * n, [0.0] * n, [""] * n, bytearray(n))
    depth, shade, cat, back = layer.depth, layer.shade, layer.cat, layer.back
    r, u, toward = frame.basis
    light = _unit(
        (
            toward[0] + 0.55 * u[0] + 0.35 * r[0],
            toward[1] + 0.55 * u[1] + 0.35 * r[1],
            toward[2] + 0.55 * u[2] + 0.35 * r[2],
        )
    )
    for tri, k in zip(tris, cats, strict=True):
        a, b, c = tri
        nrm = _cross(_sub(b, a), _sub(c, a))
        ln = math.sqrt(_dot(nrm, nrm))
        if ln == 0:
            continue
        facing = _dot(nrm, toward) / ln
        s = 0.3 + 0.7 * abs(_dot(nrm, light) / ln)
        is_back = 1 if facing < 0 else 0
        p0, p1, p2 = frame.project(a), frame.project(b), frame.project(c)
        x0, y0, d0 = p0
        x1, y1, d1 = p1
        x2, y2, d2 = p2
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-12:
            continue
        dzdx = ((d1 - d0) * (y2 - y0) - (d2 - d0) * (y1 - y0)) / area
        dzdy = ((d2 - d0) * (x1 - x0) - (d1 - d0) * (x2 - x0)) / area
        ylo = max(0, math.ceil(min(y0, y1, y2) - 0.5))
        yhi = min(h - 1, math.floor(max(y0, y1, y2) - 0.5))
        edges = ((x0, y0, x1, y1), (x1, y1, x2, y2), (x2, y2, x0, y0))
        for py in range(ylo, yhi + 1):
            yc = py + 0.5
            xl, xr = math.inf, -math.inf
            for xa, ya, xb, yb in edges:
                if (ya <= yc < yb) or (yb <= yc < ya):
                    x = xa + (yc - ya) * (xb - xa) / (yb - ya)
                    xl, xr = min(xl, x), max(xr, x)
            if xl > xr:
                continue
            px0, px1 = max(0, math.ceil(xl - 0.5)), min(w - 1, math.floor(xr - 0.5))
            if px0 > px1:
                continue
            row = py * w
            d = d0 + dzdy * (yc - y0) + dzdx * (px0 + 0.5 - x0)
            for i in range(row + px0, row + px1 + 1):
                if d > depth[i] and (clip is None or d <= clip):
                    depth[i], shade[i], cat[i], back[i] = d, s, k, is_back
                d += dzdx
    return layer


def _mix(a: Sequence[int], b: Sequence[int], t: float) -> tuple[int, int, int]:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _edges(layer: _Layer, w: int, h: int, jump: float) -> bytearray:
    """1 where a line is drawn: the outline, a jump in depth (an opening, a step), a turn in the
    shade (a crease) or a change of part kind."""
    depth, shade, cat, back = layer.depth, layer.shade, layer.cat, layer.back
    out = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            i = y * w + x
            if depth[i] == NEAR:
                continue
            for j in (
                i + 1 if x + 1 < w else -1,
                i + w if y + 1 < h else -1,
                i - 1 if x else -1,
                i - w if y else -1,
            ):
                if (
                    j < 0
                    or depth[j] == NEAR
                    or abs(depth[j] - depth[i]) > jump
                    or abs(shade[j] - shade[i]) > 0.08
                    or cat[j] != cat[i]
                    or back[j] != back[i]
                ):
                    out[i] = 1
                    break
    return out


def _paint(frame: _Frame, dev: _Layer, world: _Layer | None, *, cut: bool = False) -> bytes:
    """The picture as PNG bytes: the device in its colours (cut faces red in a section), edges
    where the depth jumps or the shade turns, the world ghosted over it (shaded, its own outline
    drawn)."""
    w, h = frame.width, frame.height
    depth, shade, cat, back = dev.depth, dev.shade, dev.cat, dev.back
    jump = 4.0 / frame.scale
    lines = _edges(dev, w, h, jump)
    wlines = _edges(world, w, h, jump) if world is not None else None
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        if depth[i] == NEAR:
            c: tuple[int, int, int] = WHITE
        elif lines[i]:
            c = EDGE
        elif cut and back[i]:
            c = CUT
        else:
            base = COLORS.get(cat[i], COLORS["other"])
            s = shade[i]
            c = (round(base[0] * s), round(base[1] * s), round(base[2] * s))
        if world is not None and wlines is not None and world.depth[i] != NEAR:
            ws = 0.6 + 0.4 * world.shade[i]
            ghost = (round(GHOST[0] * ws), round(GHOST[1] * ws), round(GHOST[2] * ws))
            if depth[i] == NEAR:
                alpha = GHOST_ALONE
            else:
                alpha = GHOST_IN_FRONT if world.depth[i] > depth[i] else GHOST_BEHIND
            if wlines[i] and alpha != GHOST_BEHIND:
                ghost, alpha = _mix(ghost, EDGE, 0.35), 0.7
            c = _mix(c, ghost, alpha)
        k = i * 3
        rgb[k], rgb[k + 1], rgb[k + 2] = c
    return png(bytes(rgb), w, h)


def png(rgb: bytes, width: int, height: int) -> bytes:
    """An RGB image as PNG bytes (no imaging library)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def section_at(tris: Sequence[Tri], axis: int, low: float, high: float) -> float:
    """The cut along ``axis`` (among :data:`SECTION_SHARES`) that crosses the most triangles of
    the device; the middle on a tie."""
    best, at_best = -1, (low + high) / 2
    for share in SECTION_SHARES:
        at = low + share * (high - low)
        crossed = sum(1 for t in tris if min(p[axis] for p in t) < at < max(p[axis] for p in t))
        if crossed > best:
            best, at_best = crossed, at
    return at_best


def render_set(tris: Sequence[Tri], cats: Sequence[str]) -> list[dict[str, Any]]:
    """The picture set of one built version (its view mesh's triangles and their
    :func:`categories_of`): ``view-*`` (eight), ``world-*`` (eight, when the model has world
    bodies) and ``section-x|y|z``. Each ``{name, kind, view, why?, data}``."""
    dtris = [t for t, c in zip(tris, cats, strict=True) if c != "world"]
    dcats = [c for c in cats if c != "world"]
    wtris = [t for t, c in zip(tris, cats, strict=True) if c == "world"]
    wcats = ["world"] * len(wtris)
    if not dtris:
        raise BundleError("the view mesh has no device bodies")
    dpoints = [p for t in dtris for p in t]
    apoints = dpoints + [p for t in wtris for p in t]
    out: list[dict[str, Any]] = []
    for name, toward, up, words in VIEWS:
        frame = _Frame(_basis(toward, up), _box(_basis(toward, up), dpoints))
        data = _paint(frame, _raster(dtris, dcats, frame), None)
        out.append({"name": f"view-{name}.png", "kind": "analytic", "view": words, "data": data})
    if wtris:
        for name, toward, up, words in VIEWS:
            basis = _basis(toward, up)
            frame = _Frame(basis, _world_box(basis, dpoints, apoints))
            data = _paint(frame, _raster(dtris, dcats, frame), _raster(wtris, wcats, frame))
            view = words + ", the world's bodies ghosted"
            out.append({"name": f"world-{name}.png", "kind": "world", "view": view, "data": data})
    low, high = bounds(dtris)
    for axis in range(3):
        at = section_at(dtris, axis, low[axis], high[axis])
        toward = tuple(1.0 if k == axis else 0.0 for k in range(3))
        basis = _basis(toward, SECTION_UP[axis])  # type: ignore[arg-type]
        frame = _Frame(basis, _box(basis, dpoints))
        layer = _raster(dtris, dcats, frame, clip=at)
        if all(d == NEAR for d in layer.depth):
            continue
        wl = _raster(wtris, wcats, frame, clip=at) if wtris else None
        out.append(
            {
                "name": f"section-{AXES[axis]}.png",
                "kind": "section",
                "view": f"cut at {AXES[axis]} = {at:.1f} mm, seen from +{AXES[axis]}",
                "why": "the plane across this axis that cuts the most of the device",
                "data": _paint(frame, layer, wl, cut=True),
            }
        )
    return out


# -- the client's words and the fact sheet ---------------------------------------------------------


def _text(raw: bytes | None, cap: int = MAX_TEXT) -> str:
    return raw.decode("utf-8", errors="replace")[:cap] if raw else ""


def _cap(value: Any, cap: int = MAX_STATEMENT) -> str:
    return " ".join(str(value or "").split())[:cap]


def _toml(raw: bytes | None) -> dict[str, Any]:
    try:
        return tomllib.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except (tomllib.TOMLDecodeError, ValueError):
        return {}


def client_words(names: Sequence[str], read: Callable[[str], bytes | None]) -> dict[str, Any]:
    """The client's own words, verbatim: the brief, the answers, the change requests and the
    stated making method."""
    out: dict[str, Any] = {"format": CLIENT_FORMAT, "brief": _text(read("brief/brief.md"))}
    for key, prefix in (("answers", "brief/answer-"), ("changes", "brief/change-")):
        out[key] = [
            {"name": n.removeprefix("brief/"), "text": _text(read(n))}
            for n in sorted(names)
            if n.startswith(prefix)
        ][:MAX_ITEMS]
    out["making"] = _text(read("brief/making.md"))
    out["making_toml"] = _text(read("brief/making.toml"))
    return out


def facts_of(
    report: Mapping[str, Any],
    world_toml: bytes | None,
    bodies: Sequence[Mapping[str, Any]],
    size_mm: list[float] | None,
    *,
    questions_allowed: bool = True,
) -> dict[str, Any]:
    """The miner's approximate fact sheet: the project's requirements, world items, parts,
    decisions, assumptions, guard and diagnostics as its own report states them."""
    reqs: list[dict[str, Any]] = []
    for need in report.get("needs") or []:
        if not isinstance(need, Mapping):
            continue
        for r in need.get("requirements") or []:
            if isinstance(r, Mapping):
                reqs.append(
                    {
                        "id": _cap(r.get("id"), 80),
                        "need": _cap(need.get("id"), 80),
                        "level": _cap(r.get("level"), 20),
                        "statement": _cap(r.get("statement")),
                        "weakest_source": _cap(r.get("weakest_source")),
                    }
                )
    world = _toml(world_toml).get("world")
    items = []
    for name, spec in world.items() if isinstance(world, Mapping) else []:
        if isinstance(spec, Mapping):
            items.append(
                {
                    "name": _cap(name, 80),
                    "kind": _cap(spec.get("kind"), 40),
                    "fidelity": _cap(spec.get("fidelity"), 40),
                }
            )
    decisions = [d for d in report.get("decisions") or [] if isinstance(d, Mapping)]
    assumed = report.get("assumed") if isinstance(report.get("assumed"), Mapping) else {}
    chosen = [
        {
            "kind": "choice",
            "question": _cap(d.get("question")),
            "built": _cap(d.get("option") or d.get("choice")),
            "because": _cap(d.get("because")),
        }
        for d in decisions
        if d.get("by") == "agent" and d.get("status") == "decided"
    ]
    accepted = [
        {
            "kind": "accepted_for_client",
            "question": _cap(a.get("question")),
            "built": _cap(a.get("option") or a.get("choice")),
            "because": _cap(a.get("because")),
        }
        for a in (assumed or {}).get("accepted") or []
        if isinstance(a, Mapping)
    ]
    missed = [
        {"kind": "missed_need", "question": _cap(m.get("question") or m.get("need") or m)}
        for m in (assumed or {}).get("misses") or []
    ]
    unanswered = [
        {"kind": "unanswered", "question": _cap(d.get("question"))}
        for d in decisions
        if d.get("by") == "client" and d.get("status") == "open"
    ]
    judgement = report.get("judgement") if isinstance(report.get("judgement"), Mapping) else {}
    to_verify = [
        _cap(i.get("verify"))
        for i in (judgement or {}).get("items") or []
        if isinstance(i, Mapping) and i.get("verify")
    ]
    guard = report.get("guard") if isinstance(report.get("guard"), Mapping) else {}
    mutants = (guard or {}).get("mutants")
    kinds = Counter(
        f"{b.get('role') or '?'}:{b.get('kind') or '?'}"
        for b in bodies
        if isinstance(b, Mapping) and b.get("role") != "world"
    )
    return {
        "format": FACTS_FORMAT,
        "note": "the miner's own approximation of the fact sheet, from the project's files",
        "deliverable": bool(report.get("deliverable")),
        "requirements": reqs[:MAX_ITEMS],
        "requirements_left_out": max(0, len(reqs) - MAX_ITEMS),
        "world": items[:MAX_ITEMS],
        "parts": {
            "bodies": sum(kinds.values()),
            "by_kind": dict(sorted(kinds.items())),
            "size_mm": size_mm,
            "world_bodies": sum(
                1 for b in bodies if isinstance(b, Mapping) and b.get("role") == "world"
            ),
        },
        "decisions": {
            "count": len(decisions),
            "by": dict(sorted(Counter(str(d.get("by") or "?") for d in decisions).items())),
            "status": dict(sorted(Counter(str(d.get("status") or "?") for d in decisions).items())),
        },
        "assumptions": {
            "questions": {"allowed": questions_allowed},
            "items": (chosen + accepted + missed + unanswered)[:MAX_ITEMS],
            "to_verify": to_verify[:MAX_ITEMS],
        },
        "guard": {
            "ran": bool((guard or {}).get("ran")),
            "mutants": mutants if isinstance(mutants, int) else len(mutants or []),
        },
        "diagnostics": dict(
            sorted(
                Counter(
                    str(d.get("severity") or "?")
                    for d in report.get("diagnostics") or []
                    if isinstance(d, Mapping)
                ).items()
            )
        ),
    }


# -- the bundle ------------------------------------------------------------------------------------


def bundle_from(
    names: Sequence[str], read: Callable[[str], bytes | None], *, questions_allowed: bool = True
) -> MinerBundle:
    """The bundle of one project, its files named relative to its root (``brief/brief.md``)."""
    stl = read("build/view/device.stl")
    if not stl:
        raise BundleError("the project has no view mesh (build/view/device.stl): build it first")
    try:
        view = json.loads(read("build/view/device.json") or b"{}")
        report = json.loads(read("build/report.json") or b"{}")
    except ValueError as e:
        raise BundleError(f"the build's JSON does not read: {e}") from None
    bodies = [b for b in (view.get("bodies") or []) if isinstance(b, Mapping)]
    tris = triangles_of(stl)
    cats = categories_of(len(tris), bodies)
    made = render_set(tris, cats)
    low, high = bounds([t for t, c in zip(tris, cats, strict=True) if c != "world"])
    size = [round(high[k] - low[k], 1) for k in range(3)]
    images = {m["name"]: m["data"] for m in made}
    manifest = {
        "format": "miner-self-review-bundle/1",
        "images": [{k: v for k, v in m.items() if k != "data"} for m in made],
    }
    photos = {}
    for n in sorted(names):
        if PHOTO.match(n):
            raw = read(n)
            if raw:
                photos[n.removeprefix("brief/")] = raw
    return MinerBundle(
        manifest=manifest,
        images=images,
        client=client_words(names, read),
        facts=facts_of(
            report if isinstance(report, Mapping) else {},
            read("spec/world.toml"),
            bodies,
            size,
            questions_allowed=questions_allowed,
        ),
        photos=photos,
    )


def bundle_from_export(data: bytes, *, questions_allowed: bool = True) -> MinerBundle:
    """The bundle of a workspace export (a zip whose project sits at its root or under one
    directory), read in memory: nothing is unpacked to disk."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BundleError(f"the export is not a zip: {e}") from None
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > MAX_EXPORT_BYTES:
            raise BundleError("the export is larger than the miner reads")
        marks = sorted(
            i.filename
            for i in infos
            if i.filename == "build/view/device.stl"
            or i.filename.endswith("/build/view/device.stl")
        )
        if not marks:
            raise BundleError("the export has no view mesh (build/view/device.stl): build it first")
        root = marks[0][: -len("build/view/device.stl")]
        files = {
            i.filename[len(root) :]: i
            for i in infos
            if i.filename.startswith(root) and i.file_size <= MAX_FILE_BYTES
        }

        def read(name: str) -> bytes | None:
            info = files.get(name)
            return z.read(info) if info is not None else None

        return bundle_from(sorted(files), read, questions_allowed=questions_allowed)
