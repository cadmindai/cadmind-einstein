"""The validator's quality vote (DESIGN.md section 13, docs/VALIDATOR.md): one delivered device,
judged against its brief by CADmind's device judge (:mod:`.judge`, ``cadmind-judge/1.2b`` in
``rate`` mode), from the platform's review bundle.

**What a vote scores.** One delivered version against its brief: the client's words (and, in our
validator's bundle, the client's photos), a structured fact sheet and a standard image set, all in
one **review bundle** the platform renders once per version and serves to every voter, so every
validator judges the same pixels (the bundle's ``image_set`` digest is part of every vote). A vote
only scores the miner: Watt's checks decide what is deliverable; nothing here delivers, charges or
gates anything.

**The judge.** :mod:`.judge` holds the one prompt, the strict reply check, the main-job cap and the
aggregation; this module holds the bundle, the vote and the average. A vote's samples are the
judge's samples (each a 0-3 score with its derived lines and rulings); its value is
``V_MAP[median_low(scores)]`` (:func:`vote_value`); a spread of 2 or more is noisy. Every vote
names the judge's digest (``judge``) and version.

**Votes published before 0.5.0** (protocol ``cadmind-einstein-vote/1``, rubric 3) still read,
verify and recompute (:data:`LEGACY_V1_WEIGHTS`): they stay in the window's reports until they age
out.

The same code runs on our validator and is what a third-party validator runs (``cadmind-review``).
"""

from __future__ import annotations

import hashlib
import io
import json
import statistics
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .judge import (
    JUDGE_VERSION,
    LINE_VERDICTS,
    NOISY_SPREAD,
    STATEMENT_MAX,
    V_MAP,
    WITHHELD,
    instruction_like,
    scrub,
)

BUNDLE_FORMAT = "cadmind-review-bundle/1"
VOTE_PROTOCOL = "cadmind-einstein-vote/2"
VOTE_PROTOCOL_V1 = "cadmind-einstein-vote/1"
"""rubric 3's votes (einstein before 0.5.0): read, verified and recomputed, never made"""
SAMPLES = 3
REQUIREMENTS_MAX = 40
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
LEGACY_V1_WEIGHTS = {"R1": 0.35, "R2": 0.30, "R3": 0.15, "R4": 0.10, "R5": 0.10}
"""rubric 3's weights: a v1 sample's value is ``sum(w * score / 4)``, capped by its published cap"""

__all__ = [
    "BUNDLE_FORMAT",
    "JUDGE_VERSION",
    "STATEMENT_MAX",
    "VOTE_PROTOCOL",
    "WITHHELD",
    "Bundle",
    "BundleError",
    "Vote",
    "Voter",
    "aggregate",
    "instruction_like",
    "read_bundle",
    "scrub_facts",
    "verify_vote",
    "vote_from",
    "vote_value",
]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def scrub_facts(facts: Any) -> tuple[Any, int]:
    """The fact sheet with every string that looks like an instruction replaced by
    :data:`WITHHELD`, and how many were (:func:`.judge.scrub`); at most
    :data:`REQUIREMENTS_MAX` requirements."""
    out, hits = scrub(facts)
    if isinstance(out, dict):
        reqs = out.get("requirements")
        if isinstance(reqs, list) and len(reqs) > REQUIREMENTS_MAX:
            out["requirements"] = reqs[:REQUIREMENTS_MAX]
    return out, hits


# -- the bundle ------------------------------------------------------------------------------------


class BundleError(ValueError):
    """A bundle that is malformed, or whose files do not match its digests."""


@dataclass
class Bundle:
    manifest: dict[str, Any]
    images: dict[str, bytes]
    """name (as the manifest and images.sha256 name it) -> bytes, in the manifest's order"""
    facts: dict[str, Any]
    client: dict[str, Any]
    image_set: str
    bundle_sha256: str
    photos: dict[str, bytes] = field(default_factory=dict)
    """the client's photos (our validator's bundle only; checked against ``photos.sha256``):
    name -> bytes, sorted by name. They are not part of the image set."""

    def views(self) -> list[str]:
        return list(self.images)

    def client_text(self) -> str:
        """Every client string, joined: what an R1 quote must be found in."""
        out: list[str] = []

        def walk(v: Any) -> None:
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, Mapping):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        walk(self.client)
        return "\n".join(out)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bundle(data: bytes) -> Bundle:
    """Open a bundle zip and check it: every image's SHA-256 matches ``images.sha256``, the image
    set's digest is that file's, and the fact sheet and the client's words match the manifest."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError("the bundle is larger than a bundle may be")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BundleError("the bundle is not a zip") from e
    with z:
        names = set(z.namelist())
        for need in ("bundle.json", "images.sha256", "facts.json", "client.json"):
            if need not in names:
                raise BundleError(f"the bundle has no {need}")
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise BundleError("the bundle names a file outside it")
        manifest = json.loads(z.read("bundle.json"))
        if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
            raise BundleError(f"the bundle is not {BUNDLE_FORMAT}")
        listing = z.read("images.sha256")
        image_set = "sha256:" + _sha(listing)
        if str(manifest.get("image_set") or "") != image_set:
            raise BundleError("the manifest's image_set is not the digest of images.sha256")
        sums: dict[str, str] = {}
        for line in listing.decode().splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            sums[name.strip()] = digest.strip()
        images: dict[str, bytes] = {}
        seen: set[str] = set()
        for item in manifest.get("images") or []:
            name = str(item.get("name") or "")
            path = str(item.get("path") or f"images/{name}")
            # images.sha256 names each file by its path in the bundle (or by its bare name)
            listed = path if path in sums else name
            if listed not in sums or path not in names:
                raise BundleError(f"image {name!r} is not in the bundle")
            raw = z.read(path)
            want = sums[listed]
            if _sha(raw) != want or str(item.get("sha256") or want) != want:
                raise BundleError(f"image {name!r} does not match its digest")
            images[name] = raw
            seen.add(listed)
        if seen != set(sums):
            raise BundleError("images.sha256 and the manifest list different images")
        facts_raw, client_raw = z.read("facts.json"), z.read("client.json")
        if manifest.get("facts_sha256") and manifest["facts_sha256"] != _sha(facts_raw):
            raise BundleError("facts.json does not match the manifest")
        if manifest.get("client_sha256") and manifest["client_sha256"] != _sha(client_raw):
            raise BundleError("client.json does not match the manifest")
        facts, client = json.loads(facts_raw), json.loads(client_raw)
        photos = _photos(z, names, manifest)
    if not isinstance(facts, dict) or not isinstance(client, dict):
        raise BundleError("facts.json and client.json are JSON objects")
    return Bundle(manifest, images, facts, client, image_set, _sha(data), photos)


def _photos(z: zipfile.ZipFile, names: set[str], manifest: Mapping[str, Any]) -> dict[str, bytes]:
    """The client's photos of our validator's bundle, each checked against ``photos.sha256``
    (whose own digest the manifest names); none when the bundle carries none."""
    listed = manifest.get("photos") or []
    if not listed and "photos.sha256" not in names:
        return {}
    if "photos.sha256" not in names or not isinstance(listed, list):
        raise BundleError("the bundle's photos are not listed with their digests")
    listing = z.read("photos.sha256")
    if str(manifest.get("photos_sha256") or "") != _sha(listing):
        raise BundleError("the manifest's photos_sha256 is not the digest of photos.sha256")
    sums: dict[str, str] = {}
    for line in listing.decode().splitlines():
        if line.strip():
            digest, _, path = line.partition("  ")
            sums[path.strip()] = digest.strip()
    out: dict[str, bytes] = {}
    for item in listed:
        name = str((item or {}).get("name") or "")
        path = f"photos/{name}"
        if not name or "/" in name or path not in sums or path not in names:
            raise BundleError(f"photo {name!r} is not in the bundle")
        raw = z.read(path)
        if _sha(raw) != sums[path] or str(item.get("sha256") or sums[path]) != sums[path]:
            raise BundleError(f"photo {name!r} does not match its digest")
        out[name] = raw
    if set(sums) != {f"photos/{n}" for n in out}:
        raise BundleError("photos.sha256 and the manifest list different photos")
    return dict(sorted(out.items()))


def image_media(name: str, raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/png" if name.endswith(".png") else "image/jpeg"


# -- the vote --------------------------------------------------------------------------------------


def vote_value(scores: Sequence[int]) -> float | None:
    """A vote's value from its samples' 0-3 scores: ``V_MAP[median_low(scores)]``."""
    if not scores:
        return None
    return V_MAP[int(statistics.median_low(int(s) for s in scores))]


def legacy_v1_value(scores: Mapping[str, Any], cap: float | None) -> float:
    """A rubric 3 sample's value (``{R1..R5: score}``), capped: what a v1 vote published."""
    raw = sum(
        w * int(scores[c]["score"] if isinstance(scores[c], Mapping) else scores[c]) / 4
        for c, w in LEGACY_V1_WEIGHTS.items()
    )
    return round(raw if cap is None else min(raw, cap), 6)


def _line_counts(sample: Mapping[str, Any]) -> dict[str, int]:
    lines = sample.get("lines") or []
    return {v: sum(1 for r in lines if r.get("verdict") == v) for v in LINE_VERDICTS}


@dataclass
class Vote:
    review: str
    """what was reviewed: the job id on our validator (an assignment id for a third party)"""
    image_set: str
    judge: str
    """the judge's digest (:meth:`.judge.Judge.digest`); a v1 vote's rubric digest"""
    model: str
    validator: str
    samples: list[dict[str, Any]]
    at_ms: int
    abstain: bool = False
    sig: str = ""
    judge_version: str = JUDGE_VERSION
    nudges: list[dict[str, Any]] = field(default_factory=list)
    """the kept nudges (2 of 3 samples raised them): shown to the miner, never published, not
    signed (the samples are)"""
    protocol: str = VOTE_PROTOCOL

    @property
    def legacy(self) -> bool:
        return self.protocol == VOTE_PROTOCOL_V1

    @property
    def scores(self) -> list[int]:
        return [int(s["score"]) for s in self.samples if not self.legacy]

    @property
    def values(self) -> list[float]:
        if self.legacy:
            return [float(s["value"]) for s in self.samples]
        return [V_MAP[x] for x in self.scores]

    @property
    def value(self) -> float | None:
        if not self.samples:
            return None
        if self.legacy:
            return round(statistics.fmean(self.values), 6)
        return vote_value(self.scores)

    @property
    def spread(self) -> float:
        if self.legacy:
            v = self.values
            return round(max(v) - min(v), 6) if v else 0.0
        s = self.scores
        return float(max(s) - min(s)) if s else 0.0

    @property
    def noisy(self) -> bool:
        return self.spread > 0.4 if self.legacy else self.spread >= NOISY_SPREAD

    def samples_sha256(self) -> str:
        return _sha(canonical(self.samples))

    def payload(self) -> bytes:
        """The exact bytes the validator's hotkey signs (anyone can verify a published vote)."""
        return _payload(
            self.protocol,
            self.review,
            self.image_set,
            self.judge,
            self.samples_sha256(),
            self.abstain,
            self.at_ms,
            self.validator,
        )

    def as_json(self) -> dict[str, Any]:
        out = {
            **asdict(self),
            "value": self.value,
            "spread": self.spread,
            "noisy": self.noisy,
            "samples_sha256": self.samples_sha256(),
        }
        if self.legacy:
            out["rubric"] = out.pop("judge")
        return out

    def public(self) -> dict[str, Any]:
        """The vote as the public hourly report carries it: the numbers and the signature, never
        the words (the derived lines, ``seen`` notes, nudges and quotes are the client's project:
        miners read them on their own jobs only)."""
        out: dict[str, Any] = {
            "protocol": self.protocol,
            "review": self.review,
            "image_set": self.image_set,
            "judge": self.judge,
            "judge_version": self.judge_version,
            "model": self.model,
            "validator": self.validator,
            "at_ms": self.at_ms,
            "abstain": self.abstain,
            "value": self.value,
            "spread": self.spread,
            "noisy": self.noisy,
            "samples_sha256": self.samples_sha256(),
            "sig": self.sig,
        }
        if self.legacy:
            out.pop("judge")
            out.pop("judge_version")
            out["rubric"] = self.judge
            out["scores"] = [
                {c: int(s[c]["score"]) for c in LEGACY_V1_WEIGHTS} for s in self.samples
            ]
            out["caps"] = [s.get("cap") for s in self.samples]
            return out
        out["scores"] = self.scores
        out["capped"] = [s.get("capped_from") is not None for s in self.samples]
        out["lines"] = [_line_counts(s) for s in self.samples]
        out["nudges_filed"] = [
            sum(1 for n in s.get("nudges") or [] if not n.get("void")) for s in self.samples
        ]
        out["nudges_kept"] = len(self.nudges)
        return out


def _payload(
    protocol: str,
    review: str,
    image_set: str,
    judge: str,
    samples_sha256: str,
    abstain: bool,
    at_ms: int,
    validator: str,
) -> bytes:
    return "\n".join(
        (
            protocol,
            review,
            image_set,
            judge,
            samples_sha256,
            "1" if abstain else "0",
            str(int(at_ms)),
            validator,
        )
    ).encode()


def vote_from(row: Mapping[str, Any]) -> Vote:
    """A stored or published vote as a :class:`Vote` (a v1 row names ``rubric``, not ``judge``)."""
    legacy = "judge" not in row and "rubric" in row
    return Vote(
        review=str(row["review"]),
        image_set=str(row["image_set"]),
        judge=str(row["rubric"] if legacy else row["judge"]),
        model=str(row.get("model") or ""),
        validator=str(row.get("validator") or ""),
        samples=list(row.get("samples") or []),
        at_ms=int(row["at_ms"]),
        abstain=bool(row.get("abstain")),
        sig=str(row.get("sig") or ""),
        judge_version=str(row.get("judge_version") or ("" if legacy else JUDGE_VERSION)),
        nudges=list(row.get("nudges") or []),
        protocol=VOTE_PROTOCOL_V1 if legacy else str(row.get("protocol") or VOTE_PROTOCOL),
    )


def verify_vote(vote: Mapping[str, Any] | Vote, samples_sha256: str = "") -> bool:
    """Whether a vote's signature is its validator's, over :meth:`Vote.payload`; a public vote (no
    samples) is checked against its published ``samples_sha256``. A v1 vote is checked over v1's
    payload."""
    try:
        from bittensor.sp_core import Keypair  # noqa: PLC0415

        if isinstance(vote, Vote):
            payload, validator, sig = vote.payload(), vote.validator, vote.sig
        else:
            legacy = "judge" not in vote and "rubric" in vote
            validator, sig = str(vote["validator"]), str(vote.get("sig") or "")
            payload = _payload(
                VOTE_PROTOCOL_V1 if legacy else str(vote.get("protocol") or VOTE_PROTOCOL),
                str(vote["review"]),
                str(vote["image_set"]),
                str(vote["rubric"] if legacy else vote["judge"]),
                samples_sha256 or str(vote["samples_sha256"]),
                bool(vote.get("abstain")),
                int(vote["at_ms"]),
                validator,
            )
        raw = bytes.fromhex(sig.removeprefix("0x"))
        return bool(raw) and bool(Keypair(ss58_address=validator).verify(payload, raw))
    except Exception:  # noqa: BLE001 -- anything malformed is not a valid signature
        return False


def published_value_holds(item: Mapping[str, Any]) -> bool:
    """Whether a published vote's value is what its published scores give: ``V_MAP`` of the
    lower median (v2), or rubric 3's capped weighted mean (v1)."""
    scores = item.get("scores") or []
    if not scores or item.get("value") is None:
        return True
    if "judge" not in item and "rubric" in item:
        caps = item.get("caps") or [None] * len(scores)
        if len(caps) != len(scores):
            caps = [None] * len(scores)
        values = [
            legacy_v1_value(s, None if c is None else float(c))
            for s, c in zip(scores, caps, strict=True)
        ]
        return abs(statistics.fmean(values) - float(item["value"])) <= 1e-6
    try:
        want = vote_value([int(s) for s in scores])
    except (TypeError, ValueError, KeyError):
        return False
    return want is not None and abs(want - float(item["value"])) <= 1e-6


# -- the average across validators (DESIGN.md section 13.5) ----------------------------------------

CLAMP = 0.2
U_MAX = 0.4
"""The most the vote counts in the baseline: with our vote alone, and with third parties too (a
client's stars outweigh it: DESIGN.md section 5)."""
THIRD_PARTY_FOR_MAX = 3
THIRD_PARTY_CAP = 1 / 3


@dataclass(frozen=True)
class Voter:
    """One counted vote, already checked (digests, not the builder's coldkey, reliability floor)."""

    coldkey: str
    hotkey: str
    value: float
    stake: float = 0.0
    reliability: float = 1.0
    ours: bool = False
    noisy: bool = False


def aggregate(voters: Sequence[Voter], *, s_ref: float = 1.0) -> tuple[float | None, float]:
    """``(V, u)`` of one job's votes: merged per coldkey (stake-weighted mean of its hotkeys),
    each coldkey's value clamped to the median +- ``CLAMP`` when there are three or more, weighted
    ``a = 1`` for ours and ``r * min(1, stake / s_ref)`` for a third party (no third-party coldkey
    over a third of the third parties' weight; a noisy vote at half weight). ``u`` is
    :data:`U_MAX` (0.4) with our vote, alone or with third parties; third parties without ours
    reach it at :data:`THIRD_PARTY_FOR_MAX` coldkeys; (None, 0) without votes."""
    if not voters:
        return None, 0.0
    by_ck: dict[str, list[Voter]] = {}
    for v in voters:
        by_ck.setdefault(v.coldkey, []).append(v)
    merged: list[tuple[str, float, float, bool]] = []  # coldkey, value, weight, ours
    for ck, vs in by_ck.items():
        ours = any(v.ours for v in vs)
        stake = sum(max(0.0, v.stake) for v in vs)
        if stake > 0:
            value = sum(v.value * max(0.0, v.stake) for v in vs) / stake
        else:
            value = statistics.fmean(v.value for v in vs)
        half = 0.5 if all(v.noisy for v in vs) else 1.0
        if ours:
            weight = 1.0 * half
        else:
            r = statistics.fmean(v.reliability for v in vs)
            weight = r * min(1.0, stake / s_ref if s_ref > 0 else 1.0) * half
        merged.append((ck, value, weight, ours))
    if len(merged) >= 3:
        med = statistics.median(m[1] for m in merged)
        merged = [(ck, min(med + CLAMP, max(med - CLAMP, v)), w, o) for ck, v, w, o in merged]
    third = [m for m in merged if not m[3]]
    total_third = sum(m[2] for m in third)
    if len(third) > 1 and total_third > 0:
        cap = THIRD_PARTY_CAP * total_third
        merged = [(ck, v, (min(w, cap) if not o else w), o) for ck, v, w, o in merged]
    total = sum(m[2] for m in merged)
    if total <= 0:
        return None, 0.0
    value = sum(m[1] * m[2] for m in merged) / total
    counted = sum(1 for m in third if m[2] > 0)
    has_ours = any(m[3] for m in merged)
    u = U_MAX if has_ours else U_MAX * min(1.0, counted / THIRD_PARTY_FOR_MAX)
    return round(value, 6), round(u, 6)


# -- one review ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``cadmind-review BUNDLE.zip --model PROVIDER:MODEL --key-file K``: judge one bundle with your
    own model and key (``cadmind-judge/1.2b``, ``rate``), and print the vote (unsigned). Posting a
    vote to the einstein validator comes with third-party voting."""
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import time  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from .judge import Judge, inputs_from_bundle  # noqa: PLC0415
    from .models import base_url_for, model_from  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="cadmind-review")
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--model", required=True, help="openai:<model> or anthropic:<model>")
    ap.add_argument("--key-file", type=Path, required=True)
    ap.add_argument("--base-url", default="", help="default for openai:<model>: OpenRouter's")
    ap.add_argument("--effort", default="medium")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    bundle = read_bundle(a.bundle.read_bytes())
    model = model_from(
        a.model,
        a.key_file.read_text().strip(),
        base_url=base_url_for(a.model, a.base_url),
        effort=a.effort,
    )
    j = Judge()
    got = j.run("rate", inputs_from_bundle(bundle), model, parallel=False)
    vote = Vote(
        review=bundle.bundle_sha256,
        image_set=bundle.image_set,
        judge=j.digest(),
        model=a.model,
        validator="",
        samples=got["samples"],
        at_ms=int(time.time() * 1000),
        nudges=got.get("nudges") or [],
    )
    out = {**vote.as_json(), "unavailable": got["unavailable"], "abstain": got["abstain"]}
    print(json.dumps(out, indent=1))  # noqa: T201 -- a CLI
    return 0 if not got["abstain"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
