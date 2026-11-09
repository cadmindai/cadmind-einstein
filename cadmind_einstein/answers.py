"""The client's answers in a delivered project: the validator's own check (DESIGN.md section 4).

While a job is built, its client may answer the builder's open client decisions (the questions the
client sees in the chat). The platform writes each answer into the job workspace's ``brief/`` -- the
client's words, which the builder cannot write -- as the file Watt's own designer gets:

    brief/answer-<YYYY-MM-DD>-<decision>-<HHMMSS>.md      (UTC; the stamp is when it arrived)

and a change asked mid-job as ``brief/change-<YYYY-MM-DD>-<HHMMSS>.md``. The builder reads them (the
platform's ``client_answers`` tool), records each answer in its decision -- status ``answered`` (or
``decided``), the chosen option, and ``answer = { from = "client: <quote> (cadmind.ai, <date>)" }``
quoting the file -- and re-runs what the answer reaches.

**The check** (:func:`check`), run by the validator on its own fork of the frozen workspace, from
the project's own files (the export), trusting nothing the builder says:

- every answer file that arrived before the hand-over (the miner's submit, or its deadline) is
  recorded: its decision exists, is no longer open, and an ``answer.from`` of it (or of the decision
  that superseded it) quotes the file; a chosen option (with no words of the client's) is the
  recorded choice. When a decision was answered more than once, its latest answer is the one that
  counts. An answer that arrived after the hand-over never counts against the builder: it is the
  next round's.
- in a split project, a parent decision may be only a link to a part's decision (Watt's
  ``same_as = "subs.<key>.decision.<id>"``, ADR-014): the question, its status and its answer are
  the part's, in the part's tree vendored under ``subprojects/<key>/``. The check follows the link
  (and a part's own links, ``subprojects/<key>/subprojects/<inner>/``) and asks the same of the
  part's decision.
- a job whose client asked not to be asked questions (``no_questions``) leaves no client decision
  open: each would-be question is the builder's own decision (``by = "agent"``, ``status =
  "decided"``), a need no option keeps included (``misses``: Watt accepts it on such a job, since
  the
  client can always ask for an update).

Pure: the caller hands in the export's bytes, so the reading is tested apart from any platform.
"""

from __future__ import annotations

import calendar
import io
import json
import re
import time
import tomllib
import zipfile
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ANSWER_NAME = re.compile(r"^answer-(\d{4}-\d{2}-\d{2})-(.+?)(?:-(\d{6}))?\.md$")
"""``answer-<date>-<decision>-<HHMMSS>.md`` (the stamp is optional in older files)."""
RECEIVED = re.compile(r"\(received (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}) UTC")
MAX_SPEC_BYTES = 2_000_000
"""A spec file larger than this is not read (it is not a hand-written project file)."""
MAX_ANSWER_BYTES = 64_000
MAX_CHAIN = 16
"""How far a ``superseded_by`` / ``same_as`` chain is followed."""
MAX_DEPTH = 4
"""How deep vendored parts are read: ``subprojects/<key>/`` nested at most this many times."""
MAX_SPECS_BYTES = 32_000_000
"""All the vendored parts' spec files read from one export, together."""
TREE = "subprojects/"
"""Where a split project's parts are vendored (Watt's ``subprojects/<key>/``)."""
KEY = r"[a-z][a-z0-9_]{0,31}"
"""A part's key, as Watt's sub-project ids are spelled."""
SAME_AS = re.compile(rf"^subs\.({KEY})\.decision\.([^.\s/]+)$")
"""A ``same_as`` link: ``subs.<key>.decision.<id>``."""
MIRRORED = "_m"
"""A mirrored part's twin (``<key>_m``) is built from its original's tree,
``subprojects/<key>/``."""
SPEC_PATH = re.compile(rf"^((?:{TREE}{KEY}/){{0,{MAX_DEPTH}}})spec/[^/]+\.toml$")
"""A spec file of the project or of a vendored part: group 1 is the part's root ('' for the
project)."""
SAID = 3
"""How many problems the gate's reason names (the rest are counted)."""
MAX_REPORT_BYTES = 64_000_000
"""A report larger than this is not read for its digest."""
JOB_STATEMENT = re.compile(r"^job-\d{4}-\d{2}-\d{2}-\d{6}\.toml$")
"""A later round's ``brief/job-<date>-<HHMMSS>.toml``: the platform restating whether the client may
be asked; it asks for no change."""


@dataclass(frozen=True)
class AnswerFile:
    """One client answer under ``brief/``."""

    path: str
    """its path in the project (``brief/answer-...md``)"""
    decision: str
    at_ms: int
    """when it arrived (UTC), from its name; 0 when the name says no time"""
    text: str
    choice: str = ""
    """the option the client picked (``Answer: <id>``), or ''"""
    words: bool = False
    """whether the client also wrote their own words"""


def _stamp_ms(date: str, hhmmss: str | None) -> int:
    try:
        t = time.strptime(f"{date} {hhmmss or '000000'}", "%Y-%m-%d %H%M%S")
    except ValueError:
        return 0
    return calendar.timegm(t) * 1000


def parse_answer(path: str, text: str) -> AnswerFile | None:
    """An answer file by its path in the project and its text; None for any other file."""
    name = path.rsplit("/", 1)[-1]
    m = ANSWER_NAME.match(name)
    if not m or not path.startswith("brief/") or path.count("/") != 1:
        return None
    date, decision, hhmmss = m.group(1), m.group(2), m.group(3)
    at = _stamp_ms(date, hhmmss)
    if not hhmmss:
        got = RECEIVED.search(text)
        if got:
            at = _stamp_ms(got.group(1), f"{got.group(2)}{got.group(3)}59")
    choice = ""
    for line in text.splitlines():
        if line.startswith("Answer: "):
            choice = line.removeprefix("Answer: ").split(" (", 1)[0].strip()
            break
    return AnswerFile(
        path=path,
        decision=decision,
        at_ms=at,
        text=text,
        choice=choice,
        words="In the client's words:" in text,
    )


def _norm(text: str) -> str:
    return " ".join(text.replace("“", '"').replace("”", '"').split()).casefold()


def quote_of(answer_from: str) -> str:
    """The quote of an ``answer.from`` (``client: <quote> (<channel>, <date>)``), unquoted; '' when
    it is none."""
    s = str(answer_from or "").strip()
    if not s.lower().startswith("client:"):
        return ""
    s = s[len("client:") :].strip()
    s = re.sub(r"\s*\([^()]*\)\s*$", "", s).strip()
    if len(s) >= 2 and s[0] in "\"'“" and s[-1] in "\"'”":
        s = s[1:-1].strip()
    return s


def quotes(answer_from: str, file: AnswerFile) -> bool:
    """Whether ``answer_from`` quotes ``file``: its quote, whitespace and case aside, is in the
    file's text."""
    q = _norm(quote_of(answer_from))
    return bool(q) and q in _norm(file.text)


def _from_of(decision: Mapping[str, Any]) -> str:
    answer = decision.get("answer")
    if isinstance(answer, Mapping):
        return str(answer.get("from") or "")
    return ""


def decisions_of(spec_files: Mapping[str, str], root: str = "") -> dict[str, dict[str, Any]]:
    """Every ``[decision.<id>]`` of the project's ``spec/*.toml`` (path -> text), by id; with
    ``root`` (``subprojects/<key>/``), of that vendored part's instead. A file that does not parse
    is skipped (Watt's own checks refuse it long before)."""
    out: dict[str, dict[str, Any]] = {}
    for path, text in sorted(spec_files.items()):
        got = SPEC_PATH.match(path)
        if got is None or got.group(1) != root:
            continue
        try:
            table = tomllib.loads(text).get("decision")
        except tomllib.TOMLDecodeError:
            continue
        if isinstance(table, Mapping):
            for ident, d in table.items():
                if isinstance(d, Mapping):
                    out[str(ident)] = dict(d)
    return out


def open_client(decisions: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The ids of the open client decisions: the questions still put to the client."""
    return sorted(
        i for i, d in decisions.items() if d.get("by") == "client" and d.get("status") == "open"
    )


def _links(decision: Mapping[str, Any]) -> list[str]:
    """A decision's ``same_as`` links, as Watt reads them: one string, or a list of them."""
    got = decision.get("same_as")
    if isinstance(got, str):
        return [got]
    if isinstance(got, list):
        return [str(r) for r in got]
    return []


@dataclass(frozen=True)
class _Step:
    """One decision on the way from the answered one to where it is recorded."""

    ident: str
    decision: Mapping[str, Any]
    where: str
    """how the gate names it: the answered id, then each hop (same as ..., superseded by ...)"""
    link: bool
    """whether it is only a ``same_as`` link (its part's decision states the rest)"""


class _Parts:
    """The decisions of the project and of each vendored part, read once per root."""

    def __init__(
        self, decisions: Mapping[str, Mapping[str, Any]], specs: Mapping[str, str] | None
    ) -> None:
        self._specs = specs or {}
        self._roots = {
            g.group(1) for g in (SPEC_PATH.match(p) for p in self._specs) if g is not None
        }
        self._by_root: dict[str, Mapping[str, Mapping[str, Any]]] = {"": decisions}

    def at(self, root: str) -> Mapping[str, Mapping[str, Any]]:
        if root not in self._by_root:
            self._by_root[root] = decisions_of(self._specs, root)
        return self._by_root[root]

    def follow(self, root: str, ref: str) -> tuple[str, str] | None:
        """``(part root, decision id)`` a ``same_as`` link stated in ``root`` names; None when it
        names nothing the export has."""
        got = SAME_AS.match(ref.strip())
        if got is None or root.count(TREE) >= MAX_DEPTH:
            return None
        key, ident = got.group(1), got.group(2)
        for k in (key, key.removesuffix(MIRRORED)) if key.endswith(MIRRORED) else (key,):
            part = f"{root}{TREE}{k}/"
            if part in self._roots:
                return (part, ident) if ident in self.at(part) else None
        return None


def _chain(parts: _Parts, ident: str) -> tuple[list[_Step], str]:
    """The decisions an answer to ``ident`` goes through: ``superseded_by`` within a project or
    part, ``same_as`` into a part. Second: the link whose part's decision is missing, or ''."""
    out: list[_Step] = []
    seen: set[tuple[str, str]] = set()
    root, hops = "", []
    while len(out) < MAX_CHAIN and (root, ident) not in seen:
        decisions = parts.at(root)
        if ident not in decisions:
            break
        seen.add((root, ident))
        d = decisions[ident]
        where = f"{out[0].ident} ({', '.join(hops)})" if out else ident
        links = _links(d)
        out.append(_Step(ident, d, where, bool(links)))
        if links:
            followed = next(((r, t) for r in links if (t := parts.follow(root, r))), None)
            if followed is None:
                return out, links[0]
            ref, (root, ident) = followed
            hops.append(f"same as {ref}")
            continue
        if d.get("status") != "superseded":
            break
        ident = str(d.get("superseded_by") or "")
        hops.append(f"superseded by {ident}")
    return out, ""


def unrecorded(
    answers: Iterable[AnswerFile],
    decisions: Mapping[str, Mapping[str, Any]],
    *,
    before_ms: int = 0,
    specs: Mapping[str, str] | None = None,
) -> list[str]:
    """What is wrong with how the project records its client's answers, one line each; [] when every
    answer that arrived before ``before_ms`` (0: every one) is recorded. ``specs`` (path -> text,
    as :func:`project_files` reads them) are where a ``same_as`` link finds its part's decision."""
    latest: dict[str, AnswerFile] = {}
    for a in answers:
        if before_ms and a.at_ms > before_ms:
            continue  # arrived after the hand-over: the next round's, never this builder's
        was = latest.get(a.decision)
        if was is None or (a.at_ms, a.path) > (was.at_ms, was.path):
            latest[a.decision] = a
    parts = _Parts(decisions, specs)
    problems: list[str] = []
    for ident, a in sorted(latest.items()):
        chain, missing = _chain(parts, ident)
        if not chain:
            problems.append(
                f"{a.path} answers decision {ident}, which the project no longer has (never"
                " delete a decision: answer it, or supersede it)"
            )
            continue
        if missing:
            problems.append(
                f"{a.path} answers decision {chain[-1].where}, which is same_as {missing}: the"
                " linked part's decision is missing from the project (vendor the part's tree under"
                " subprojects/<key>/ as it was delivered, or link a decision the part has)"
            )
            continue
        stated = [s for s in chain if not s.link]
        first = stated[0] if stated else chain[0]
        if str(first.decision.get("status") or "") == "open":
            problems.append(
                f"{a.path} answers decision {first.where}, which is still open: record it (status"
                ' "answered", its choice, answer = { from = "client: <quote> (cadmind.ai,'
                ' <date>)" }) and re-run what it reaches'
            )
            continue
        recorded = [
            s
            for s in stated
            if s.decision.get("status") in ("answered", "decided")
            and quotes(_from_of(s.decision), a)
        ]
        if not recorded:
            problems.append(
                f"decision {chain[-1].where} does not quote the client's answer {a.path}: its"
                ' answer.from must be "client: <words from that file> (cadmind.ai, <date>)"'
            )
            continue
        rec = recorded[-1].decision
        picked = a.choice and not a.words and a.choice in (rec.get("options") or {})
        if picked and str(rec.get("choice") or "") != a.choice:
            problems.append(
                f"decision {recorded[-1].where} records choice {rec.get('choice')!r}; the client"
                f" chose {a.choice!r} ({a.path})"
            )
    return problems


def project_files(export: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """``(brief answer files, spec files)`` of a project export (a zip): path in the project ->
    text. The spec files are the project's ``spec/*.toml`` and those of its vendored parts
    (``subprojects/<key>/spec/*.toml``, nested parts too, :data:`MAX_DEPTH`). The export's entries
    may sit under one top directory (the workspace's name); either way paths are the project's
    own."""
    briefs: dict[str, str] = {}
    specs: dict[str, str] = {}
    budget = MAX_SPECS_BYTES
    with zipfile.ZipFile(io.BytesIO(export)) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        names = [i.filename for i in infos]
        top = ""
        if names and all("/" in n for n in names):
            first = {n.split("/", 1)[0] for n in names}
            if len(first) == 1 and not any(n.startswith(("brief/", "spec/")) for n in names):
                top = first.pop() + "/"
        for info in infos:
            rel = (
                info.filename[len(top) :]
                if top and info.filename.startswith(top)
                else info.filename
            )
            if ".." in rel.split("/") or rel.startswith("/"):
                continue
            top_level = rel.count("/") == 1
            if top_level and rel.startswith("brief/") and ANSWER_NAME.match(rel[6:]):
                if info.file_size <= MAX_ANSWER_BYTES:
                    briefs[rel] = z.read(info).decode("utf-8", errors="replace")
            elif SPEC_PATH.match(rel) and info.file_size <= MAX_SPEC_BYTES:
                if rel.startswith(TREE):  # a part's: within the parts' budget
                    if info.file_size > budget:
                        continue
                    budget -= info.file_size
                specs[rel] = z.read(info).decode("utf-8", errors="replace")
    return briefs, specs


def _project_entries(z: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """An open export's files by their path in the project (one top directory taken off)."""
    infos = [i for i in z.infolist() if not i.is_dir()]
    tops = {i.filename.split("/", 1)[0] for i in infos if "/" in i.filename}
    top = ""
    if len(tops) == 1 and all("/" in i.filename for i in infos):
        only = next(iter(tops))
        if only not in ("brief", "spec", "build"):
            top = only + "/"
    return {i.filename.removeprefix(top): i for i in infos}


def report_digest(export: bytes) -> str:
    """The digest a project export's own ``build/report.json`` states; '' when it says none or
    cannot be read."""
    try:
        with zipfile.ZipFile(io.BytesIO(export)) as z:
            info = _project_entries(z).get("build/report.json")
            if info is None or info.file_size > MAX_REPORT_BYTES:
                return ""
            report = json.loads(z.read(info))
    except (zipfile.BadZipFile, OSError, ValueError, RecursionError):
        return ""
    return str(report.get("digest") or "") if isinstance(report, dict) else ""


def answers_only(seed: bytes, export: bytes) -> bool:
    """Whether what the client sent for this round, read from the ``brief/`` files the platform
    wrote and the builder cannot write, is nothing but answers picking an offered option: at least
    one new ``answer-*`` file, each a chosen option with no words of the client's, and no other new
    file (no ``change-*``, no photo). The brief's text is not read for this: a client may type
    anything there. Unreadable: False."""
    try:
        with zipfile.ZipFile(io.BytesIO(seed)) as z:
            before = set(_project_entries(z))
        with zipfile.ZipFile(io.BytesIO(export)) as z:
            entries = _project_entries(z)
            new = {
                rel: info
                for rel, info in entries.items()
                if rel not in before and rel.startswith("brief/") and rel.count("/") == 1
            }
            answers = []
            for rel, info in new.items():
                name = rel.removeprefix("brief/")
                if JOB_STATEMENT.match(name):
                    continue
                if not ANSWER_NAME.match(name) or info.file_size > MAX_ANSWER_BYTES:
                    return False
                answers.append(parse_answer(rel, z.read(info).decode("utf-8", errors="replace")))
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    return bool(answers) and all(a is not None and a.choice and not a.words for a in answers)


def check(
    export: bytes,
    *,
    before_ms: int = 0,
    no_questions: bool = False,
    kept_open: Collection[str] = (),
) -> str:
    """The gate's answers check on a project export: '' when it passes, else the reason (for the
    miner). ``kept_open``: client decisions that may stay open under ``no_questions`` -- a style
    round's settled ones, open when the version was delivered (deciding one would change the
    contract the round must keep)."""
    try:
        briefs, specs = project_files(export)
    except (zipfile.BadZipFile, OSError, ValueError):
        return "the delivered project could not be read to check the client's answers"
    decisions = decisions_of(specs)
    answers = [a for a in (parse_answer(p, t) for p, t in briefs.items()) if a is not None]
    problems = unrecorded(answers, decisions, before_ms=before_ms, specs=specs)
    said: list[str] = []
    if problems:
        more = f" (and {len(problems) - SAID} more)" if len(problems) > SAID else ""
        said.append(
            "the client's answers are not all recorded: " + "; ".join(problems[:SAID]) + more
        )
    if no_questions:
        still = [i for i in open_client(decisions) if i not in set(kept_open)]
        if still:
            said.append(
                "the client asked not to be asked questions, and client decision(s) "
                f'{", ".join(still[:8])} are open: decide each yourself (by = "agent", status ='
                ' "decided", its choice and because)'
            )
    return "; and ".join(said)
