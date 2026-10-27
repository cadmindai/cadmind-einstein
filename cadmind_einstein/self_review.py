"""The reference miner's self-review round (``--self-review on``, the default): after the builder's
``DONE`` and before the submit, the validators' own judge looks at the built device, and the
points it upholds go back to the builder as a correction.

The round, inside the job's deadline:

1. export the built project with the miner's agent key and draw its review bundle
   (:mod:`.miner_bundle`: the validator's picture set and the client's words, drawn by the miner;
   its docstring states how it differs from the bundle a validator is served);
2. the reviewer: ONE sample of the judge's ``nudge`` mode (:func:`.judge.judge`, the validators'
   version), its well-formed nudges, at most :data:`MAX_REVIEW_NUDGES`;
3. the check: the judge's ``check`` mode (``--self-review-samples`` samples; a split is unsure)
   rules each nudge upheld / not upheld;
4. the correction: the builder is given the upheld points and the time left (:data:`CORRECTION`):
   Claude Code's session is resumed; an ``--agent-command`` agent is started again, told the
   workspace holds its earlier work;
5. the final check: the judge's ``final`` mode on the built and the corrected version side by side
   rules each point gone / still there / unsure, and may name NEW points; while a point is still
   there or new and the time holds a correction and its check, the builder is resumed again
   (:data:`CORRECTION_LOOP`), up to ``--self-review-loops`` times;
6. the submit carries the last version the builder delivered (``DONE``).

**Time.** No round starts with less than :data:`ROUND_MIN_LEFT_S` left; every step checks what is
left first and :data:`SUBMIT_RESERVE_S` is always kept for the submit; a judge step that runs
past that is abandoned (:meth:`SelfReview._judge`). The builder is told a deadline
:func:`reserve_s` earlier than the job's, so the round has its time. A final check that does not
fit before the submit runs right after it: it changes nothing delivered, it only records whether
the correction worked.

**Cost.** The judge is the miner's own model, key and bill (``--self-review-model``,
``--self-review-key-file``): this program passes no credential of its own and assumes no plan.
**Follow your model provider's terms.** No key configured: the round is skipped and the miner
works as before. Each step's dollars (the provider's reported cost, e.g. OpenRouter's) and minutes
are recorded, with what was found and fixed (:meth:`SelfReview.summary`), so a miner can see
whether the round pays.

Nothing of the round reaches the platform or the validator: the validator judges the delivered
device on its own bundle, as for any miner.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .judge import VALIDATOR_VERSION, inputs_for_final, inputs_from_bundle

MODES = ("on", "off")
JUDGE_VERSION = VALIDATOR_VERSION
"""the validators' judge: the same prompt, the same rules, the same version"""
DEFAULT_MODEL = "openai:anthropic/claude-opus-5.5"
"""an ``openai:`` model goes to OpenRouter unless ``--self-review-base-url`` says otherwise"""
DEFAULT_EFFORT = "high"
"""the effort the validators' judge is calibrated at"""
DEFAULT_SAMPLES = 3
DEFAULT_LOOPS = 2
MAX_REVIEW_NUDGES = 3

ROUND_MIN_LEFT_S = 15 * 60
"""no round starts with less left: the bundle, the reviewer and the check (about 4 min), a
correction worth having and the submit"""
CHECK_S = 4 * 60
"""what the check needs before a correction can still follow it"""
CORRECT_MIN_S = 5 * 60
"""no correction shorter than this"""
FINAL_S = 6 * 60
"""a final check (export, bundle, judge) runs before the submit only with this much left on top
of the submit's reserve"""
SUBMIT_RESERVE_S = 3 * 60
"""always kept for the submit"""
RESERVE_MARGIN_S = 3 * 60
RESERVE_MAX_SHARE = 0.4
"""the builder's deadline is told earlier by at most this share of the job's time"""
JUDGE_TIMEOUT_S = 600
EXPORT_TIMEOUT_S = 300

CORRECTION = """A reviewer took a quick look at the device you built, and a judge upheld these \
points (each names the picture it shows in and the client's words it goes against):

{nudges}

What they cost: after your correction a judge looks at the corrected device again. Each upheld \
problem still visible there costs half of this job's score; a problem you fixed costs nothing. \
Answering a point in words, or a cosmetic change that leaves it visible, counts as still there.

You now have about {minutes} minutes, until {deadline_utc} UTC (`time_left` says the rest). Fix \
each point in the design. Only when a point is plainly wrong, answer it in one line in \
DESIGN-NOTES.md under `## Review` (its id and why) and record a decided agent decision. Then make \
the project deliverable again: the scoped guard, then the one final `report` with mutate, and \
`watt status` deliverable. End exactly as before: a few lines for the client, then the one line \
`DONE <digest>` with the digest `watt status` reports. If the time runs out before the fix is \
deliverable, keep the last deliverable version."""
NO_EARLY_END = (
    " Ending early while a point is still in the device is not acceptable when time is left: keep "
    "working on it until it is gone from the device or `time_left` is under 5 minutes."
)
"""added when loops may follow: builders tended to end a correction early with the point still
there"""
CORRECTION_LOOP = """A judge compared your corrected device with the one you first built: these \
points are in it now, either STILL THERE from before or NEW (often brought in by the last change) \
-- each names the picture it shows in and the client's words it goes against:

{nudges}

A point still there means what you changed did not change what the judge sees; a new point means \
the fix moved the problem. Either way a design change is needed -- move, resize, add, remove or \
replace the part in the model (or the bought part it relies on) -- not a new test, a wording \
change, a note or a decision record. Each point still visible after this correction costs half of \
this job's score; a point you fix costs nothing.

You now have about {minutes} minutes, until {deadline_utc} UTC (`time_left` says the rest). Ending \
early while a point is still in the device is not acceptable when time is left: keep working on it \
until it is gone from the device or `time_left` is under 5 minutes. Then make the project \
deliverable again: the scoped guard, then the one final `report` with mutate, and `watt status` \
deliverable. End exactly as before: a few lines for the client, then the one line `DONE <digest>` \
with the digest `watt status` reports. If the time runs out before the fix is deliverable, keep \
the last deliverable version."""
FRESH = """

This workspace already holds your earlier work on this job: you delivered it (`DONE {digest}`). \
Read DESIGN-NOTES.md and `watt status` first; then do what follows.

"""
"""a correction for an ``--agent-command`` agent, which cannot resume: the job's prompt, this, the
correction"""

_DONE = re.compile(r"^DONE\s+(\S+)\s*$", re.MULTILINE)
_REJECT = re.compile(r"^REJECT ", re.MULTILINE)


def reserve_s(deadline_ms: int, now: float) -> float:
    """How much earlier than the job's deadline the builder is told it is, so the round is not
    skipped for time: :data:`ROUND_MIN_LEFT_S` + :data:`RESERVE_MARGIN_S`, at most
    :data:`RESERVE_MAX_SHARE` of the time left."""
    if not deadline_ms:
        return 0.0
    return min(
        float(ROUND_MIN_LEFT_S + RESERVE_MARGIN_S),
        max(0.0, (deadline_ms / 1000 - now) * RESERVE_MAX_SHARE),
    )


def unavailable(mode: str, model: str, key_file: Path | None) -> str:
    """Why the round cannot run for this miner ('' when it can): off, no key file, an empty or
    unreadable one, or the provider's SDK not installed. Reads the key file only to see it is
    not empty; never logs it."""
    if mode != "on":
        return "off (--self-review off)"
    provider = model.partition(":")[0]
    if provider not in ("openai", "anthropic") or not model.partition(":")[2]:
        return f"--self-review-model is openai:<model> or anthropic:<model>, not {model!r}"
    if key_file is None:
        return (
            "no judge key (--self-review-key-file): the round is skipped, the miner works as before"
        )
    try:
        if not Path(key_file).expanduser().read_text(encoding="utf-8").strip():
            return f"the judge key file {key_file} is empty"
    except OSError as e:
        return f"the judge key file cannot be read: {type(e).__name__}"
    if importlib.util.find_spec(provider) is None:
        return f"the {provider} SDK is not installed (pip install 'cadmind-einstein[review]')"
    return ""


class CostMeter:
    """A judge model, its spend counted: the provider's reported dollars per request (OpenRouter's
    ``usage.cost``), and the requests that reported none."""

    def __init__(self, model: Any) -> None:
        self.model = model
        self.name = getattr(model, "name", "?")
        self.usd = 0.0
        self.unpriced = 0
        self._lock = threading.Lock()

    def _count(self, replies: Sequence[Any]) -> None:
        with self._lock:
            for r in replies:
                cost = getattr(r, "cost_usd", None)
                if isinstance(cost, (int, float)):
                    self.usd += float(cost)
                else:
                    self.unpriced += 1

    def converse(self, system: str, turns: Any, tools: Any = None) -> Any:
        conv = self.model.converse(system, turns, tools)
        if getattr(conv, "usd", None) is not None:
            with self._lock:
                self.usd += float(conv.usd)
        else:
            self._count(getattr(conv, "replies", []) or [])
        return conv

    def ask(self, system: str, text: str, images: Any) -> Any:
        reply = self.model.ask(system, text, images)
        self._count([reply])
        return reply


def judge_model(model: str, key_file: Path, *, effort: str, base_url: str = "") -> CostMeter:
    """The miner's judge model (an ``openai:`` model goes to OpenRouter by default), metered."""
    from .judge import validator_model  # noqa: PLC0415

    key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
    return CostMeter(validator_model("openrouter", model, key, base_url=base_url, effort=effort))


# -- the agent API, with the miner's own agent key -------------------------------------------------


def _agent_api(
    platform: str,
    key: str,
    method: str,
    path: str,
    body: Any = None,
    *,
    raw: bool = False,
    timeout: float = 120,
    budget_s: float = 240,
) -> tuple[int, Any]:
    """One agent-API request, again on no answer, 409 (the key's one lane busy), 429 and 5xx,
    for at most ``budget_s``. The key is never logged."""
    t0 = time.time()
    while True:
        req = urllib.request.Request(  # noqa: S310 -- the operator's own platform URL
            platform.rstrip("/") + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                data = r.read()
                return int(r.status), data if raw else json.loads(data or b"{}")
        except urllib.error.HTTPError as e:
            code, data = e.code, e.read()
        except (OSError, ValueError) as e:
            code, data = 0, str(e).encode()
        if not (code in (0, 409, 429) or code >= 500) or time.time() - t0 > budget_s:
            try:
                return code, json.loads(data or b"{}")
            except ValueError:
                return code, {"error": data[:300].decode(errors="replace")}
        time.sleep(10)


def workspace_export(platform: str, key: str, ws: str) -> bytes | None:
    """The workspace's export (a zip), or None."""
    code, out = _agent_api(
        platform,
        key,
        "GET",
        f"/api/agent/workspaces/{ws}/export",
        raw=True,
        timeout=EXPORT_TIMEOUT_S,
    )
    return out if code == 200 and isinstance(out, bytes) else None


def workspace_status(platform: str, key: str, ws: str) -> dict[str, Any] | None:
    """``watt status`` of the workspace (deliverable, stage, digest), or None."""
    code, out = _agent_api(
        platform,
        key,
        "POST",
        f"/api/agent/workspaces/{ws}/run",
        {"command": "status", "wait_s": 90},
    )
    t0 = time.time()
    while (
        code == 200
        and isinstance(out, dict)
        and out.get("state") in ("running", "queued", "waiting")
        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(out.get("run") or ""))
        and time.time() - t0 < 180
    ):
        code, out = _agent_api(platform, key, "GET", f"/api/agent/runs/{out['run']}?wait_s=60")
    st = (out.get("data") or {}).get("status") if isinstance(out, dict) else None
    return dict(st) if isinstance(st, dict) else None


# -- the round -------------------------------------------------------------------------------------


def nudge_list(nudges: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(
        f"- {n['id']}: {n.get('text')} (view: {n.get('view')}; part: {n.get('part') or '-'}; "
        f'the client\'s words: "{n.get("quote")}")'
        for n in nudges
    )


def _bounded(fn: Callable[[], Any], timeout: float) -> Any:
    """``fn()`` in a daemon thread, waited for at most ``timeout`` seconds: its result, or
    TimeoutError (the call is abandoned: a late answer is dropped). Its exception is raised."""
    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["out"] = fn()
        except BaseException as e:  # noqa: BLE001 -- handed to the caller
            box["err"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(max(0.0, timeout))
    if t.is_alive():
        raise TimeoutError(f"no answer within {timeout / 60:.1f} min")
    if "err" in box:
        raise box["err"]
    return box.get("out")


Correct = Callable[[str, float], tuple[str, bool]]
"""``correct(message, budget_s) -> (the agent's final text, cancelled)``: the builder run once more
with ``message``, stopped at ``budget_s``"""


class SelfReview:
    """One job's round (see the module's docstring). ``run`` then, when ``final_pending``,
    ``final(after_submit=False)`` if :meth:`final_fits`, then ``loops``; after the submit,
    ``final(after_submit=True)`` when still pending. ``text`` is the corrected run's final text
    when the corrected version is the one to submit. No method raises."""

    def __init__(
        self,
        *,
        job: str,
        built_text: str,
        deadline_ms: int,
        export: Callable[[], bytes | None],
        status: Callable[[], Mapping[str, Any] | None],
        correct: Correct,
        log: Callable[[str], None],
        cancelled: Callable[[], bool],
        model: Any,
        samples: int = DEFAULT_SAMPLES,
        loops: int = DEFAULT_LOOPS,
        questions_allowed: bool = True,
        judge_fn: Callable[..., dict[str, Any]] | None = None,
        bundle_fn: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.job, self.built_text, self.deadline_ms = job, built_text, deadline_ms
        self.export, self.status, self.correct, self.log = export, status, correct, log
        self.cancelled, self.model, self.clock = cancelled, model, clock
        self.samples, self.loops_max = max(1, int(samples)), max(0, int(loops))
        self.questions_allowed = questions_allowed
        self.judge_fn, self.bundle_fn = judge_fn, bundle_fn
        self.built: Any = None
        self.upheld: list[dict[str, Any]] = []
        self.text = ""
        self.final_pending = False
        self.final_targets: list[dict[str, Any]] = []
        self.final_loop = 0
        self.rec: dict[str, Any] = {
            "judge_version": JUDGE_VERSION,
            "judge_model": str(getattr(model, "name", "") or "?"),
            "samples": self.samples,
            "loops_max": self.loops_max,
            "skipped": None,
            "started_left_min": None,
            "nudges": [],
            "check": {},
            "upheld": [],
            "correction": None,
            "final": None,
            "loops": [],
            "delivered": "built",
            "usd": {},
            "minutes": {},
        }

    # -- plumbing ------------------------------------------------------------------------------

    def left_s(self) -> float:
        return self.deadline_ms / 1000 - self.clock() if self.deadline_ms else 0.0

    def skip(self, why: str) -> None:
        self.rec["skipped"] = why
        self.log(f"{self.job}: self-review: skipped: {why}")

    def _usd(self) -> float:
        return float(getattr(self.model, "usd", 0.0) or 0.0)

    def _judge(
        self,
        mode: str,
        inputs: Any,
        step: str,
        *,
        samples: int | None = None,
        after_submit: bool = False,
    ) -> dict[str, Any] | None:
        """One judge step, timed, its dollars kept; None (the reason recorded) when the judge
        fails, abstains or would run into the submit's reserve."""
        t0, usd0 = self.clock(), self._usd()
        if self.judge_fn is None:
            from .judge import judge as judge_fn  # noqa: PLC0415
        else:
            judge_fn = self.judge_fn
        limit = (
            JUDGE_TIMEOUT_S
            if after_submit
            else min(JUDGE_TIMEOUT_S, self.left_s() - SUBMIT_RESERVE_S)
        )
        kw = {"samples": samples} if samples is not None else {}
        try:
            if limit <= 0:
                raise TimeoutError("no time left before the submit's reserve")
            out = _bounded(
                lambda: judge_fn(mode, inputs, self.model, version=JUDGE_VERSION, **kw), limit
            )
        except Exception as e:  # noqa: BLE001 -- the judge unavailable: the job goes on
            self._spent(step, t0, usd0)
            self.skip(f"the judge failed at the {step}: {type(e).__name__}: {str(e)[:200]}")
            return None
        self._spent(step, t0, usd0)
        if not isinstance(out, dict) or out.get("abstain"):
            why = "; ".join((out or {}).get("unavailable") or []) if isinstance(out, dict) else ""
            self.skip(f"the judge abstained at the {step}: {why[:300]}")
            return None
        self.rec.setdefault("judge", out.get("judge"))
        return out

    def _spent(self, step: str, t0: float, usd0: float) -> None:
        self.rec["usd"][step] = round(self._usd() - usd0, 4)
        self.rec["minutes"][step] = round((self.clock() - t0) / 60, 2)

    def _bundle(self, step: str) -> Any:
        t0 = self.clock()
        data = self.export()
        if not data:
            raise ValueError("the workspace export failed")
        if self.bundle_fn is not None:
            bundle = self.bundle_fn(data)
        else:
            from .miner_bundle import bundle_from_export  # noqa: PLC0415

            bundle = bundle_from_export(data, questions_allowed=self.questions_allowed)
        self.rec["minutes"][f"bundle_{step}"] = round((self.clock() - t0) / 60, 2)
        return bundle

    # -- the round -----------------------------------------------------------------------------

    def run(self) -> None:
        """bundle -> reviewer -> check -> correction (the final check and loops are separate)."""
        t0 = self.clock()
        self.rec["started_left_min"] = round(self.left_s() / 60, 1)
        try:
            self._run()
        except Exception as e:  # noqa: BLE001 -- the round never costs the job its submit
            self.final_pending = False
            self.skip(f"the round failed: {type(e).__name__}: {str(e)[:200]}")
        finally:
            self.rec["minutes"]["round"] = round((self.clock() - t0) / 60, 2)

    def _run(self) -> None:
        if self.left_s() < ROUND_MIN_LEFT_S:
            return self.skip(
                f"only {self.left_s() / 60:.1f} min left (the round needs {ROUND_MIN_LEFT_S // 60})"
            )
        try:
            self.built = self._bundle("built")
        except Exception as e:  # noqa: BLE001
            return self.skip(f"the built version's bundle failed: {type(e).__name__}: {e!s:.200}")
        if self.cancelled():
            return self.skip("the job was cancelled")
        if not self.review(self.built):
            return None
        if self.cancelled():
            return self.skip("the job was cancelled")
        return self._correct()

    def review(self, bundle: Any) -> bool:
        """The reviewer and the check: True when a nudge is upheld (``self.upheld``)."""
        out = self._judge("nudge", inputs_from_bundle(bundle), "reviewer", samples=1)
        if out is None:
            return False
        sample = (out.get("samples") or [{}])[0]
        filed = [dict(n) for n in sample.get("nudges") or []]
        live = [n for n in filed if not n.get("void")][:MAX_REVIEW_NUDGES]
        self.rec["nudges"] = filed
        self.log(
            f"{self.job}: self-review: the reviewer filed {len(filed)} nudges "
            f"({len(live)} well-formed)"
        )
        if not live:
            return False
        if self.cancelled():
            self.skip("the job was cancelled")
            return False
        need = CHECK_S + CORRECT_MIN_S + SUBMIT_RESERVE_S
        if self.left_s() < need:
            self.skip(
                f"only {self.left_s() / 60:.1f} min left before the check (a check and a "
                f"correction need {need // 60})"
            )
            return False
        out = self._judge(
            "check", inputs_from_bundle(bundle, nudges=live), "check", samples=self.samples
        )
        if out is None:
            return False
        self.rec["check"] = {
            nid: {
                **v,
                "why": [
                    next((r.get("seen") for r in s.get("verdicts") or [] if r.get("id") == nid), "")
                    for s in out.get("samples") or []
                ],
            }
            for nid, v in (out.get("verdicts") or {}).items()
        }
        self.upheld = [
            n for n in live if (self.rec["check"].get(n["id"]) or {}).get("verdict") == "upheld"
        ]
        self.rec["upheld"] = [n["id"] for n in self.upheld]
        self.log(
            f"{self.job}: self-review: the check upheld {len(self.upheld)} of {len(live)}"
            + "".join(f"\n  {n['id']}: {str(n.get('text'))[:200]}" for n in self.upheld)
        )
        return bool(self.upheld)

    def _current_digest(self) -> str:
        m = _DONE.search(self.text or self.built_text)
        return m.group(1) if m else ""

    def _correct(self, loop: int = 0, nudges: list[dict[str, Any]] | None = None) -> None:
        """The builder once more, with the points: loop 0 the upheld ones (:data:`CORRECTION`),
        loop k those the last final check found still there or new (:data:`CORRECTION_LOOP`; its
        budget keeps that loop's final check before the submit)."""
        nudges = self.upheld if nudges is None else nudges
        say = "correction" if not loop else f"loop {loop}"
        held = FINAL_S if loop else 0
        budget = self.left_s() - SUBMIT_RESERVE_S - held
        if budget < CORRECT_MIN_S:
            why = (
                f"only {self.left_s() / 60:.1f} min left for the {say} (it needs "
                f"{(CORRECT_MIN_S + SUBMIT_RESERVE_S + held) // 60})"
            )
            if loop:
                self.rec["loops"].append({"loop": loop, "skipped": why})
                self.log(f"{self.job}: self-review: {say}: skipped: {why}")
                return None
            return self.skip(why)
        minutes = int(budget // 60)
        deadline_utc = time.strftime("%H:%M", time.gmtime(self.deadline_ms / 1000 - held))
        template = CORRECTION_LOOP if loop else CORRECTION
        msg = (
            template.replace("{nudges}", nudge_list(nudges))
            .replace("{minutes}", str(minutes))
            .replace("{deadline_utc}", deadline_utc)
        )
        if not loop and self.loops_max:
            msg += NO_EARLY_END
        self.log(
            f"{self.job}: self-review: {say}: the builder gets {len(nudges)} point(s), "
            f"{minutes} min"
        )
        t0 = self.clock()
        said, cancelled = self.correct(msg, budget)
        corr: dict[str, Any] = {
            "minutes": round((self.clock() - t0) / 60, 2),
            "given_min": minutes,
            "points": [n["id"] for n in nudges],
            "cancelled": cancelled,
        }
        if loop:
            corr["loop"] = loop
            self.rec["loops"].append(corr)
        else:
            self.rec["correction"] = corr
        if cancelled or self.cancelled():
            self.final_pending = False
            return self.skip("the job was cancelled during the correction")
        before = self._current_digest()
        m = _DONE.search(said)
        digest = m.group(1) if m and not _REJECT.search(said) else ""  # never a REJECT now
        corr["ended"] = "done" if digest else "no DONE"
        if not digest:
            # no DONE: whatever the workspace holds is what the validator gates; when it is
            # deliverable its digest is submitted, else the last delivered text
            st = None
            with contextlib.suppress(Exception):  # unread: the last delivered text stays
                st = self.status()
            corr["status"] = {k: (st or {}).get(k) for k in ("deliverable", "stage", "digest")}
            if st and st.get("deliverable") and st.get("digest"):
                digest = str(st["digest"])
                # the last delivered words for the client, with the workspace's digest
                said = _DONE.sub("", self.text or self.built_text).rstrip() + f"\nDONE {digest}\n"
                corr["ended"] = "no DONE; the workspace is deliverable"
            else:
                corr["note"] = "the correction ended undeliverable: the last delivered text stays"
        if digest and digest != before:
            self.text = said
            self.rec["delivered"] = "corrected"
            self.final_pending = True
            self.final_targets, self.final_loop = list(nudges), loop
        else:
            # the device did not change: every point is still there, by the rule
            self._found(
                {
                    "by_rule": "the device did not change" if digest else "no deliverable version",
                    "verdicts": {
                        n["id"]: {"samples": [], "verdict": "still_there"} for n in nudges
                    },
                },
                loop,
            )
        self.log(
            f"{self.job}: self-review: {say} ended ({corr['ended']}) in {corr['minutes']} "
            f"min; delivers the {self.rec['delivered']} version"
        )
        return None

    def _found(self, fin: dict[str, Any], loop: int) -> None:
        """A final check's record: loop 0's is ``final``; a loop's is kept in its own record and
        its verdicts update ``final`` (which holds each point's last verdict)."""
        if not loop:
            self.rec["final"] = fin
            return
        rec = next((r for r in reversed(self.rec["loops"]) if r.get("loop") == loop), None)
        if rec is not None:
            rec["final"] = fin
        if fin.get("verdicts"):
            base = self.rec.get("final") if isinstance(self.rec.get("final"), dict) else {}
            self.rec["final"] = {
                **base,
                "verdicts": {**(base.get("verdicts") or {}), **fin["verdicts"]},
                "nudges": fin.get("nudges", base.get("nudges")),
                "score": fin.get("score", base.get("score")),
                "last_loop": loop,
            }

    def still_there(self) -> list[dict[str, Any]]:
        fin = (self.rec.get("final") or {}).get("verdicts") or {}
        return [n for n in self.upheld if (fin.get(n["id"]) or {}).get("verdict") == "still_there"]

    def new_in_final(self, loop: int) -> list[dict[str, Any]]:
        """The last final check's own NEW points (not void), named ``F<loop>N<k>`` and added to
        the points the next final check rules on: a final check often names the main failure as
        a new point, and a loop must act on it."""
        if loop == 1:
            fin = self.rec.get("final") or {}
        else:
            fin = next(
                (
                    r["final"]
                    for r in reversed(self.rec["loops"])
                    if r.get("loop") == loop - 1 and isinstance(r.get("final"), dict)
                ),
                {},
            )
        out = [
            {**n, "id": f"F{loop}N{k}"}
            for k, n in enumerate(
                (m for m in fin.get("nudges") or [] if isinstance(m, dict) and not m.get("void")),
                1,
            )
        ][:MAX_REVIEW_NUDGES]
        self.upheld = [*self.upheld, *out]
        return out

    def final_fits(self) -> bool:
        return self.left_s() >= FINAL_S + SUBMIT_RESERVE_S

    def final(self, after_submit: bool) -> None:
        """The final check on the pending correction: the built and corrected versions side by
        side, each point gone / still there / unsure, the final score and any new points."""
        if not self.final_pending:
            return
        self.final_pending = False
        loop = self.final_loop
        step = "final" if not loop else f"final{loop}"
        try:
            corrected = self._bundle(step)
            out = self._judge(
                "final",
                inputs_for_final(self.built, corrected, self.final_targets or self.upheld),
                step,
                samples=self.samples,
                after_submit=after_submit,
            )
        except Exception as e:  # noqa: BLE001
            self._found(
                {"skipped": f"{type(e).__name__}: {str(e)[:200]}", "after_submit": after_submit},
                loop,
            )
            return
        if out is None:
            fin = {"skipped": self.rec["skipped"], "after_submit": after_submit}
            if loop:
                self.rec["skipped"] = None  # a loop's lost final check does not undo the round
            self._found(fin, loop)
            return
        fin = {
            "after_submit": after_submit,
            "verdicts": {
                nid: {
                    **v,
                    "why": [
                        next(
                            (r.get("seen") for r in s.get("verdicts") or [] if r.get("id") == nid),
                            "",
                        )
                        for s in out.get("samples") or []
                    ],
                }
                for nid, v in (out.get("verdicts") or {}).items()
            },
            "score": out.get("score"),
            "scores": out.get("scores"),
            "nudges": out.get("nudges"),
        }
        self._found(fin, loop)
        said = ", ".join(f"{k} {v['verdict']}" for k, v in fin["verdicts"].items())
        new = [n for n in out.get("nudges") or [] if isinstance(n, dict) and not n.get("void")]
        self.log(
            f"{self.job}: self-review: final check{f' (loop {loop})' if loop else ''}: {said}; "
            f"score {out.get('score')}; {len(new)} new point(s)"
            + (" (after the submit)" if after_submit else "")
        )

    def loops(self) -> None:
        """While the last final check says a point is still there, or names a new one, and the
        time holds a correction and its final check, the builder goes again, up to
        ``loops_max`` times."""
        for k in range(1, self.loops_max + 1):
            if self.cancelled() or self.final_pending or self.rec.get("skipped"):
                return
            if not isinstance(self.rec.get("final"), dict) or "verdicts" not in self.rec["final"]:
                return  # no final check to act on
            todo = self.still_there() + self.new_in_final(k)
            if not todo:
                return
            try:
                self._correct(loop=k, nudges=todo)
                if self.final_pending and self.final_fits() and not self.cancelled():
                    self.final(after_submit=False)
            except Exception as e:  # noqa: BLE001 -- a loop never costs the job its submit
                self.rec["loops"].append({"loop": k, "skipped": f"{type(e).__name__}: {e!s:.200}"})
                self.log(f"{self.job}: self-review: loop {k} failed: {type(e).__name__}")
                return
            last = self.rec["loops"][-1] if self.rec["loops"] else {}
            if last.get("skipped") or self.final_pending:
                return

    def summary(self) -> dict[str, Any]:
        """The record: counts, dollars and minutes per step, what was delivered."""
        live = [n for n in self.rec["nudges"] if not n.get("void")]
        check = self.rec["check"]
        fin = (self.rec.get("final") or {}).get("verdicts") or {}
        counts = {
            "nudges": len(self.rec["nudges"]),
            "well_formed": len(live),
            "upheld": sum(1 for v in check.values() if v.get("verdict") == "upheld"),
            "not_upheld": sum(1 for v in check.values() if v.get("verdict") == "not_upheld"),
            "check_unsure": sum(1 for v in check.values() if v.get("verdict") == "unsure"),
            "gone": sum(1 for v in fin.values() if v.get("verdict") == "gone"),
            "still_there": sum(1 for v in fin.values() if v.get("verdict") == "still_there"),
            "final_unsure": sum(1 for v in fin.values() if v.get("verdict") == "unsure"),
            "loops_run": sum(1 for r in self.rec["loops"] if not r.get("skipped")),
        }
        return {
            **self.rec,
            "counts": counts,
            "final_score": (self.rec.get("final") or {}).get("score"),
            "usd_total": round(sum(float(v or 0) for v in self.rec["usd"].values()), 4),
            "unpriced_requests": int(getattr(self.model, "unpriced", 0) or 0),
        }

    def line(self) -> str:
        """One log line: what it found and fixed, and what it cost."""
        s = self.summary()
        c = s["counts"]
        if s["skipped"] and not c["nudges"]:
            return f"self-review: skipped ({s['skipped']}); ${s['usd_total']:.2f}"
        return (
            f"self-review: {c['well_formed']} nudges, {c['upheld']} upheld, {c['gone']} gone, "
            f"{c['still_there']} still there, {c['loops_run']} loop(s); final score "
            f"{s['final_score']}; delivered the {s['delivered']} version; "
            f"${s['usd_total']:.2f} on the judge"
            + (f"; skipped: {s['skipped']}" if s["skipped"] else "")
        )
