"""A reference miner: the signed validator protocol around an agent CLI that builds over MCP.

The protocol side is the same as :mod:`.fake_miner` (agent key sealed to an ephemeral X25519 key,
admission, ``work`` / ``accept`` / ``submit`` / ``reject``); the build is an agent CLI run in an
empty directory, restricted to the hosted CADmind Watt MCP tools. One job at a time; a heartbeat
thread keeps polling ``work`` while the agent runs, so the validator keeps seeing the miner alive.

The default agent is Claude Code's CLI (``claude -p``), because its flags restrict an agent to one
MCP server; any other agent CLI works through ``--agent-command`` (see below). The miner chooses,
configures and pays for its own model and CLI: this program passes no credential of its own and
assumes no particular plan. **Follow your model provider's terms** -- many consumer plans do not
allow automated or commercial use.

    cadmind-einstein-miner --wallet.name miner --wallet.hotkey m1 \\
        --validator https://VALIDATOR --validator-hotkey 5... \\
        --platform https://cadmind.ai --state ~/.local/share/cadmind-einstein-miner \\
        [--agent-cli claude] [--model MODEL] [--agent-arg ARG ...]

    # any other agent CLI: an argv template; the prompt also arrives on stdin
    cadmind-einstein-miner ... \\
        --agent-command 'my-agent --mcp {mcp_config} --prompt-file {prompt_file}'

Placeholders in ``--agent-command``: ``{prompt_file}``, ``{mcp_config}`` (a standard
``mcpServers`` JSON file naming the ``cadmind-watt`` server and the local ``cadmind-fetch`` server,
mode 0600), ``{model}``,
``{workspace}`` and ``{job}``. A ``--prompt-file`` template takes ``{ws}``, ``{job}``, ``{brief}``,
``{minutes}``, ``{deadline_utc}``, ``{server}``, ``{questions}`` (the rule for the client's
questions and answers: check
``client_answers`` after each ``watt`` step, record each answer in its decision -- or, when the
client asked not to be asked questions, decide every one) and ``{no_questions}`` (``yes``/``no``).
The agent's last line must be ``DONE <digest>``, or it ends with
``REJECT <not-allowed|not-capable>: <message for the client>`` (the message runs to the
end of its output, over several lines if it says more).

There is no hand-over: an agent that ends without either while more than 10 minutes remain is
resumed, up to :data:`MAX_RESUMES` times, and told to take the next most valuable piece. Only
Claude Code's CLI can be resumed (``--session-id`` on the first run, ``--resume`` after:
:class:`ClaudeSession`); a session that ended with no output, or answers ``No response
requested.``, is dead, and the next run starts a new session told that the workspace holds its
earlier work. An ``--agent-command`` agent is submitted as it ended. The submission's ``summary``
is the agent's final text without its ``DONE`` line (or an ``UNFINISHED:`` line, should it write
one), so the client reads the builder's own notes on a delivered project too; a model provider's
usage-limit notice is never sent as the builder's words. Every run's exit code and error output
are kept in the job's log.

A submit or a reject the validator did not take (no answer, 429 or 5xx) is sent again with a
backoff (:func:`send`). While idle, the miner checks every hour that its agent key still works and
that it is admitted: a key the validator revoked (abuse, deregistration) is minted again, and a
dropped admission is requested again (:func:`keep_admitted`). A key request the subnet team has
not approved yet is answered "pending" (202): the miner waits and asks again
(:func:`.fake_miner.ask_key`), with ``--contact`` sent as a note for the team.

The agent also gets one local tool, ``fetch_to_workspace`` (:mod:`.fetch_mcp`, a stdio MCP server
named ``cadmind-fetch``): it downloads a public document from the miner's own connection (many sites
refuse the platform's address) and uploads it into the job's workspace, as the hosted ``fetch``
stores one. It is a fenced download, not a shell; ``--no-local-fetch`` leaves it out.

**Self-review** (``--self-review on``, the default; :mod:`.self_review`): after the agent's
``DONE`` and before the submit, the validators' own judge (:mod:`.judge`, on the miner's own model
key: ``--self-review-model``, ``--self-review-key-file``) reviews the built device on a review
bundle the miner draws itself (:mod:`.miner_bundle`), and the upheld points go back to the agent as
a correction, checked again, up to ``--self-review-loops`` times. The agent is told a deadline
:func:`.self_review.reserve_s` earlier so the round has its time; the submit is never missed.
Without a key file the round is skipped and the miner works as before. What it found, fixed and
cost is logged and written to ``logs/<job>.self-review.json``.

Flags follow Bittensor's names (``--wallet.name``, ``--wallet.hotkey``, ``--wallet.path``,
``--logging.debug``, ``--logging.trace``; the dashed ``--wallet-name``/``--wallet-hotkey`` remain
aliases). With ``--netuid`` (and ``--subtensor.network``/``--subtensor.chain_endpoint``) the miner
reads the metagraph once at startup and warns when its hotkey is not registered there; without
``--netuid`` it makes no chain call. A newer GitHub release is logged as a WARNING at startup
(``--no-version-check`` turns that off).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import __spec_version__, __version__
from . import self_review as sr
from .cli import (
    add_logging_args,
    add_subtensor_args,
    add_version_args,
    add_wallet_args,
    network_of,
    setup_logging,
    wallet_kwargs,
)
from .fake_miner import ask_key, signed
from .fetch_mcp import SERVER_NAME as FETCH_SERVER
from .fetch_mcp import TIME_TOOL
from .fetch_mcp import TOOL as FETCH_TOOL
from .fetch_mcp import server_config as fetch_server_config
from .sealing import new_keypair, open_sealed
from .version import check_latest

MCP_SERVER = "cadmind-watt"
BUILTIN_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Glob",
    "Grep",
    "LS",
    "WebFetch",
    "NotebookEdit",
    "NotebookRead",
    "Task",
    "TodoWrite",
)
"""Claude Code's built-in tools, denied by name: ``--allowedTools`` only pre-approves the MCP tools,
it does not take the built-ins away, and a hostile brief must not steer the agent into reading or
sending anything from the miner's machine. ``WebSearch`` is not here: it runs on the model
provider's side and touches nothing on this machine (``WEB_SEARCH``). ``WebFetch`` stays denied, so
every page the agent uses is downloaded with ``fetch_to_workspace`` (or the hosted ``fetch``),
stored in the project and cited."""
WEB_SEARCH = "WebSearch"
"""Claude Code's web search, pre-approved: the agent needs real drawings and dimensions of every
product the device fits (positions of ports, buttons, cameras), not only a spec sheet's outline."""
KILL_GRACE_S = 10.0

PROMPT = """You are a CADmind einstein miner. Build this client's device in the Watt workspace \
`{ws}` (made from the brief), with the {server} MCP tools, the local `fetch_to_workspace` tool \
and web search. Use web search to find drawings and dimensions of every product the device must \
fit (its outline AND the positions and sizes of its buttons, ports, grilles, camera and other \
protrusions); store each page you use with `fetch_to_workspace` (or with the hosted `fetch` when \
the local tool is not there) and cite it. A spec sheet's overall size is not enough for a part \
that fits around a product. When the maker publishes no drawing, keep looking: to-scale community \
drawings (Wikimedia Commons diagrams), case and accessory makers' templates, published CAD \
measurements; cite one with its stated tolerance. If none exists, ask the client for measurements \
or a photo beside a ruler (a client decision; when the client cannot be asked, design to a bounded \
estimate only if the need is met across its whole range, else it is a missed need, below). Never \
refuse for missing data alone; never guess silently.

Start with `watt_guide` (brief, then primer, then scope): that is the core. Then read, with \
`watt_guide`, each topic your device needs, before you write what it covers: the primer's Topics \
table names them and says when to read each, and a topic's rules bind whether you read it or not. \
The client's photos, when they sent \
any (the brief names them), are in the workspace's brief/ (brief/photo-*): look at each with \
`view_image` before you design; they are the client's evidence, as binding as their words. Work \
fast and carefully:
1. First write DESIGN-NOTES.md: how the device is made, installed, used and serviced, what can go \
wrong at each step and the requirement/test proving it does not; every critical number sourced \
(fetched into the project's assets/), never invented. Prefer the maker's own product page, \
datasheet or manual over retailer listings. Fetch maker pages and datasheets with \
`fetch_to_workspace` when you have it (it downloads from this builder's own connection, which many \
sites that block the hosted `fetch` answer; its source records add fetched_by = "builder"); the \
hosted `fetch` remains available. A site that blocks both: move on. Fetch only what the first \
sketch needs, then build.
2. Iterate with check / build / test; edit files, never rewrite them whole; end with ONE \
`report` with mutate (the full guard and the delivery's files in one run; `watt_wait` on it), \
only after a passing scoped guard (mutate with several `only`, one run) covering every must \
requirement, never as a first check.
3. Give each bought hardware record a `price` (`"4.87 USD"`; a cable stock `"0.80 USD/m"`; a \
material `"25 USD/kg"`): DigiKey's qty-1 USD price from `lookup_part`, cited to its saved source, \
or a listing you fetched; never a guess -- leave it out when unknown. The client sees the bill.
4. Self-review: load paths, retention, clearances, every clause tested; fix the weakest points.
Never weaken a test to pass. A change round (brief/change-*.md on a delivered project) is \
delivered only when the whole project passes `watt status`: failures it inherits are part of the \
job, and your time is only what `time_left` says. {questions}
A `watt` run far longer than usual: stop it with `watt_cancel` and change the design.

Deliver what you can; only the client trades a need away. When part of the request cannot be \
delivered, build everything that can be delivered without giving up a need, and make the missing \
part an open client decision whose option leaves it out (misses = ["<need>"]). When the client can \
be asked, it stays open and you deliver the rest (DONE). When the client asked not to be asked, \
you decide it yourself, once you have checked that no option you can build keeps the need, as the \
questions rule says, and deliver (DONE). A client's \
"you may quit if it is impossible" does not change that.
A device too big for one round (more parts, controls and tests than your time builds and guards): \
decide at the walk-through to split it into sub-projects (`watt describe subproject`). This \
project \
designs its own parts and defines each sub-device's interface ([subproject.<id>] with its required \
contract) and is delivered at that stage (DONE); it never builds a sub-device: the client starts \
each sub-device's job when they want, and a later round assembles them. A workspace with \
brief/contract.toml is a sub-device: meet that contract (W0230) and publish spec/contract.toml; \
brief/parent.md, when there, is the parent's brief, for context.
If the scope guide says we may not or cannot build any of it, end with:
REJECT <not-allowed|not-capable>: <message for the client>
(the last thing you write)
Otherwise work until `watt` status says deliverable and `client_answers` has nothing new (an \
answer that came is recorded first, and the design made deliverable again), then end with exactly \
one line:
DONE <digest>
(the digest `watt` status reports), after a few lines for the client: what you built, and what you \
estimated or left open. You have about {minutes} minutes, until {deadline_utc} \
UTC. You cannot feel time pass: check it with `time_left` (never guess it). There is no \
hand-over: an unfinished project earns nothing. When the remaining work looks too large, start \
its most valuable piece (one control, one hold), build it, test it, then take the next. The only \
endings are `DONE`, `REJECT`, or `time_left` under 5 minutes.

Job: {job}
Brief:
{brief}
"""


PARTS = """A change file may end with a block from CADmind listing the parts the client \
selected in the 3D view, by their view key (build/view/device.json), name and place; the \
request's "this"/"these" mean them. The answer's `text` is only the client's words: read the \
change file itself (`read_file` its `path`) for that block. `watt inspect` with \
`show: [<their keys>]` shows them."""

QUESTIONS = (
    """What only the client can decide is an open client decision (by = "client", \
status = "open"), designed around your best sourced assumption: the client sees it in their chat \
and may answer while you work. Keep building on the assumption; never wait for an answer. After \
each `watt` command, call `client_answers` (workspace `{ws}`, `after` = the last `cursor` it \
gave; a `watt` answer also says when the client wrote and you have not read it): each new answer \
is a file under brief/ (the client's words). Record it in its decision (status = "answered", \
choice = the option they picked, answer = { from = "client: <their words, quoted from that file> \
(cadmind.ai, <date>)" }; keep its assumption line: an answer that picks the assumption you built \
on keeps the recorded guard), change what hangs on it, and re-run what `watt impact` names for \
that decision. A change request (brief/change-*.md) is the client's too: \
do what it asks; a photo they send (kind photo, brief/photo-*) is theirs: look at it with \
`view_image`. The validator refuses a delivery that leaves an answer unrecorded. Use \
`wait_for_answer` only when no useful work can go on under the assumptions (it waits briefly). \
If `client_answers` says `no_questions: true`, the client asked not to be asked: decide every \
would-be question yourself instead (by = "agent", status = "decided", choice, because), a missed \
need included (misses = ["<need>"]), and say each assumption in your final lines. """
    + PARTS
)

NO_QUESTIONS = (
    """The client asked NOT to be asked questions: this is a feature, not a limit. Leave no \
client decision open: every would-be question is yours to decide. Assume, and record each \
assumption as an agent decision (by = "agent", status = "decided", choice = the option you build \
on, because = why; for a size, count or position you could not source, the range you designed \
for), and build it. The client sees each one on the result and can always ask for a change, so \
your final lines name every assumption plainly. Never write by = "client", an answer or a quote \
the client did not send. An uncertainty stays in the model as the range you assumed, never \
deleted to clear a check. A need that no option you can build keeps is decided the same way: \
build the option closest to it, record the decision with misses = ["<need>"] and its reason, and \
say so first in your final lines; do not REJECT for it. Still call `client_answers` after each \
`watt` command: a change the client sends mid-job arrives there (brief/change-*.md), and so does \
a photo (brief/photo-*: look at it with `view_image`). """
    + PARTS
)


STYLE = """ This is a STYLE ROUND on a delivered device (its watt.toml has [style]): the client's \
words are in brief/style-*.md and the image they picked is brief/photo-*-1 (the device as \
delivered, rendered, is brief/photo-*-2); look at both with `view_image`. Read the primer's \
"Reshaping (a style round)": change printed shapes only (features, sizes, rounds, lofts, where a \
cover parts); the function is settled and Watt refuses any change to it (W0220), and so does the \
validator. Record each shape choice as a decided agent decision found_by the style brief. \
Then list every visible difference between the picked image and the delivered render, and \
answer each: build it, or say in such a decision why not. Sizes are free: a feature too thin to \
round can be made thicker."""


ONE_PROJECT = """ Splitting into sub-projects is NOT allowed for this job (brief/job.toml: \
subprojects = false): deliver the device as one project. Watt refuses any [subproject.<id>] \
(W0233), and so does the validator's gate; what cannot fit is an open client decision that \
misses the need it leaves out."""


CONTINUATION = """ This is a CONTINUATION: the workspace holds an unfinished project an earlier \
builder could not finish; read its REVIEW/DESIGN-NOTES and `watt status`, then continue it, or \
start over from the base if the work is on a wrong path ({base})."""

BASE_DELIVERED = """the base is the last delivered version: the read-only workspace `{ref}`, read \
with `list_files` and `read_file`"""

BASE_BRIEF = """the base is the brief: clear spec/, src/ and tests/ and build from brief/"""


def continuation_rule(work: dict[str, Any]) -> str:
    """What the prompt says of a continuation (``continuation``): continue the unfinished project,
    or start over from its base: the last delivered version (read-only) or the brief."""
    if not work.get("continuation"):
        return ""
    base = work.get("base") if isinstance(work.get("base"), dict) else {}
    ref = str(base.get("workspace") or "") if base.get("kind") == "delivered" else ""
    said = BASE_DELIVERED.replace("{ref}", ref) if ref else BASE_BRIEF
    return CONTINUATION.replace("{base}", said)


def questions_rule(work: dict[str, Any], ws: str) -> str:
    """What the prompt says of the client's questions and answers: ask and keep building (and check
    for answers after each step), or, when the client asked not to be asked (``no_questions``),
    decide. A job that does not allow sub-projects (``subprojects`` false) says to deliver one
    project; a style round (``style``) adds what it may change; a continuation (``continuation``)
    what it starts from."""
    rule = (NO_QUESTIONS if work.get("no_questions") else QUESTIONS).replace("{ws}", ws)
    one = ONE_PROJECT if work.get("subprojects") is False else ""
    return rule + one + (STYLE if work.get("style") else "") + continuation_rule(work)


def _write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)


def agent_argv(
    a: argparse.Namespace, *, prompt: str, prompt_file: Path, mcp_file: Path, ws: str, job: str
) -> list[str]:
    """The agent's command line: the ``--agent-command`` template, or Claude Code's CLI
    restricted to the Watt MCP server."""
    if a.agent_command:
        fields = {
            "prompt_file": str(prompt_file),
            "mcp_config": str(mcp_file),
            "model": a.model or "",
            "workspace": ws,
            "job": job,
        }
        return [part.format(**fields) for part in shlex.split(a.agent_command)]
    argv = [
        a.agent_cli,
        "-p",
        prompt,
        "--mcp-config",
        str(mcp_file),
        "--strict-mcp-config",
        "--allowedTools",
        allowed_tools(a),
        "--disallowedTools",
        ",".join(BUILTIN_TOOLS),
        "--output-format",
        "text",
    ]
    if a.model:
        argv += ["--model", a.model]
    return argv + list(a.agent_arg)


def allowed_tools(a: argparse.Namespace) -> str:
    """Claude Code's ``--allowedTools``: the Watt MCP server's tools, the local fetch tool, and web
    search."""
    tools = [f"mcp__{MCP_SERVER}"]
    if getattr(a, "local_fetch", True):
        tools.append(f"mcp__{FETCH_SERVER}__{FETCH_TOOL}")
        tools.append(f"mcp__{FETCH_SERVER}__{TIME_TOOL}")
    tools.append(WEB_SEARCH)
    return ",".join(tools)


def dropped(work: Any, job: str) -> bool:
    """Whether the validator's ``work`` answer says ``job`` is no longer this miner's: the client
    cancelled it (``cancelled``), or the answer names another job or none. A failed poll (not a
    mapping) says nothing."""
    if not isinstance(work, Mapping) or "job" not in work:
        return False
    return work.get("cancelled") == job or work.get("job") != job


class AgentRun:
    """The agent CLI in its own process group, so a cancelled job stops it and everything it
    started: SIGTERM to the group, SIGKILL after ``grace_s``."""

    def __init__(
        self, cmd: list[str], *, cwd: Path, stdin: str | None, grace_s: float = KILL_GRACE_S
    ) -> None:
        self.grace_s, self.stopped = grace_s, threading.Event()
        self._stdin = stdin
        self.proc = subprocess.Popen(  # noqa: S603 -- the operator's own configured agent CLI
            cmd,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )

    def _signal(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, sig)

    def stop(self) -> None:
        """Stop the agent (from another thread): its output so far is still read by ``wait``."""
        self.stopped.set()
        self._signal(signal.SIGTERM)

        def hard() -> None:
            try:
                self.proc.wait(self.grace_s)
            except subprocess.TimeoutExpired:
                self._signal(signal.SIGKILL)

        threading.Thread(target=hard, daemon=True).start()

    def wait(self, timeout: float) -> tuple[str, str, bool]:
        """(stdout, stderr, timed_out): the agent's output once it ends, or at ``timeout`` (it is
        then stopped). Its exit code is ``proc.returncode`` after."""
        try:
            out, err = self.proc.communicate(self._stdin, timeout=timeout)
            return out or "", err or "", False
        except subprocess.TimeoutExpired:
            self.stop()
            out, err = self.proc.communicate()
            return out or "", err or "", True


RESUME = """You stopped with {minutes} minutes left. There is no hand-over: an unfinished project \
earns nothing. Continue with the next most valuable piece, build it, test it, and keep going until \
`DONE`, `REJECT`, or `time_left` under 5 minutes."""
HANDOFF = """ This workspace already holds the work of your earlier run on this job, which stopped \
({why}); you have about {minutes} minutes left. Read DESIGN-NOTES.md and `watt status` first, \
then continue it."""
"""Added to the prompt of a new session that replaces a dead one (see :func:`dead`)."""
MAX_RESUMES = 3
"""the most runs after the first: an agent that keeps ending early is resumed again while time is
left (2026-10-22 audit: builders quit with 21-105 minutes left)"""
RESUME_MIN_LEFT_S = 600.0
"""an agent that ends without ``DONE``/``REJECT`` with more than this left is resumed"""
RESUME_MARGIN_S = 120.0
"""the resumed run ends this long before the deadline, leaving time to submit"""
SUMMARY_CHARS = 2000
"""the most of the agent's final text sent as ``summary`` (the validator keeps as many)"""

_DONE = re.compile(r"^DONE\s+(\S+)\s*$", re.MULTILINE)
_REJECT = re.compile(r"^REJECT (not-allowed|not-capable):[ \t]*(\S.*)?$", re.MULTILINE)
_UNFINISHED = re.compile(r"^UNFINISHED:\s*(.+)$", re.MULTILINE)
_NO_RESPONSE = "No response requested."
"""What Claude Code's CLI answers when a resumed session does not call the model at all (2026-10-21:
a job lost 157 minutes after a run that ended with no output)."""
LIMIT = re.compile(r"you've hit your [^\n]*limit", re.IGNORECASE)
"""A model provider's usage-limit notice ("You've hit your weekly limit · resets ..."): the CLI's
words, never the builder's, so never the client's ``summary``."""


class ClaudeSession:
    """Claude Code's session flags: the first run names a new session (``--session-id``), a resume
    continues it (``--resume``) with a new ``-p`` message and the same other flags."""

    def __init__(self, session_id: str | None = None) -> None:
        self.id = session_id or str(uuid.uuid4())

    def first(self, argv: list[str]) -> list[str]:
        return [argv[0], "--session-id", self.id, *argv[1:]]

    def resume(self, argv: list[str], message: str) -> list[str]:
        """``argv``: the first run's command, no session (``[cli, "-p", prompt, ...]``)."""
        if argv[1:2] != ["-p"]:
            raise ValueError("not a Claude Code command line: [cli, '-p', prompt, ...]")
        return [argv[0], "--resume", self.id, "-p", message, *argv[3:]]


def session_for(a: argparse.Namespace) -> ClaudeSession | None:
    """The session adapter for the configured agent: Claude Code's CLI (``--agent-cli``, whose
    flags the miner writes) can be resumed; an ``--agent-command`` template cannot (None)."""
    return None if a.agent_command else ClaudeSession()


def ended(text: str) -> bool:
    """Whether the agent's output ends the job itself: a ``DONE`` or ``REJECT`` line."""
    return bool(_DONE.search(text) or _REJECT.search(text))


def dead(text: str) -> str:
    """Why a run's output says its session is dead ('' when it is not): no output at all, or the
    CLI's ``No response requested.`` A dead session is not resumed: a new one is started."""
    said = text.strip()
    if not said:
        return "it ended with no output"
    if said == _NO_RESPONSE:
        return "its session answered no more"
    return ""


class Build:
    """One job's agent runs: the final output (the last run's that said something), the log."""

    def __init__(self, text: str, record: str, *, timed_out: bool, cancelled: bool) -> None:
        self.text, self.record, self.timed_out, self.cancelled = text, record, timed_out, cancelled
        self.resumed = False
        self.runs = 1
        self.session: ClaudeSession | None = None
        """the session the last run used (a dead one is replaced): the self-review resumes it"""


def _record(out: str, err: str, code: int | None, timed_out: bool) -> str:
    end = "TIMEOUT" if timed_out else f"exit {code}"
    return f"{out}\n--- {end} ---\n--- stderr ---\n{err}"


def build(
    cmd: list[str],
    *,
    session: ClaudeSession | None,
    cwd: Path,
    stdin: str | None,
    timeout_s: float,
    deadline_ms: int,
    watch: Any,
    log: Any,
    job: str,
) -> Build:
    """Run the agent; while it ends without ``DONE`` or ``REJECT`` with more than
    :data:`RESUME_MIN_LEFT_S` before the deadline, run it again, up to :data:`MAX_RESUMES` times
    (there is no hand-over: an unfinished project earns nothing): the same session, or a new one
    told of its earlier work when the session is dead (:func:`dead`). ``watch(run)`` is told the
    running :class:`AgentRun` (``None`` once it ends), so a cancelled job can stop it. Each run's
    exit code and error output are logged and kept in the record."""

    def one(argv: list[str], stdin_: str | None, timeout: float) -> tuple[str, str, bool, bool]:
        nonlocal record
        run = AgentRun(argv, cwd=cwd, stdin=stdin_)
        watch(run)
        try:
            out, err, timed_out = run.wait(timeout)
        finally:
            watch(None)
        code = getattr(getattr(run, "proc", None), "returncode", None)
        code = code if isinstance(code, int) else None
        if (code is not None and code != 0) or dead(out):
            tail = " ".join(err.split())[-300:]
            why = f"; {dead(out)}" if dead(out) else ""
            log(f"{job}: agent run exit {code}{why}; stderr: {tail or '(none)'}")
        record += _record(out, err, code, timed_out)
        return out, err, timed_out, run.stopped.is_set() and not timed_out

    record = ""
    text, _, timed_out, cancelled = one(session.first(cmd) if session else cmd, stdin, timeout_s)
    got = Build(text, "", timed_out=timed_out, cancelled=cancelled)
    last = text
    for _ in range(MAX_RESUMES):
        if got.cancelled or got.timed_out or ended(last) or not deadline_ms:
            break
        left_s = deadline_ms / 1000 - time.time()
        if left_s <= RESUME_MIN_LEFT_S:
            break
        left_min = int(left_s // 60)
        if LIMIT.search(last):
            log(f"{job}: the model provider's usage limit stopped the agent: not resumed")
            break
        if session is None:
            log(
                f"{job}: agent stopped with {left_min} min left and no DONE; this agent CLI "
                "(--agent-command) cannot resume a session: submitting as it is"
            )
            break
        why = dead(last)
        if why:
            session = ClaudeSession()
            log(f"{job}: agent stopped with {left_min} min left ({why}); a new session takes over")
            argv = session.first(
                [cmd[0], "-p", cmd[2] + HANDOFF.format(why=why, minutes=left_min), *cmd[3:]]
            )
            record += f"\n--- NEW SESSION with {left_min} min left ({why}) ---\n"
        else:
            log(f"{job}: agent stopped with {left_min} min left and no DONE; resuming it")
            argv = session.resume(cmd, RESUME.format(minutes=left_min))
            record += f"\n--- RESUMED with {left_min} min left ---\n"
        last, _, got.timed_out, got.cancelled = one(argv, None, max(60.0, left_s - RESUME_MARGIN_S))
        got.resumed, got.runs = True, got.runs + 1
        if not dead(last):
            got.text = last  # an empty or dead answer keeps the earlier run's words
    got.record = record
    got.session = session
    return got


def outcome(job: str, ws: str, text: str) -> tuple[str, dict[str, Any]]:
    """What to send for the agent's final output: ``("reject", body)`` for a ``REJECT`` line (not
    on an admission job; its message is everything from the line's message to the end, over
    several lines when it runs on), else ``("submit", body)``. The body carries a ``summary`` for
    the client: an ``UNFINISHED:`` line when the agent wrote one anyway, else its final text
    without the ``DONE`` line (whitespace collapsed, the last :data:`SUMMARY_CHARS` characters):
    on a delivered project too, the builder's notes (what it estimated, what is open) reach the
    client. A usage-limit notice (:data:`LIMIT`) is never sent as the builder's words."""
    rej, fin = _REJECT.search(text), _DONE.search(text)
    if rej and not job.startswith("admission-"):
        message = " ".join(text[rej.start(2) if rej.group(2) else rej.end() :].split())
        return "reject", {"job": job, "category": rej.group(1), "message": message}
    digest = fin.group(1) if fin else ""
    body: dict[str, Any] = {"job": job, "workspace": ws, "digest": digest}
    left = _UNFINISHED.search(text)
    said = " ".join((left.group(1) if left else _DONE.sub("", text)).split())
    if said and not LIMIT.search(said):
        body["summary"] = said[:SUMMARY_CHARS] if left else said[-SUMMARY_CHARS:]
    return "submit", body


SEND_DELAYS_S = (5.0, 15.0, 30.0, 60.0, 60.0)
"""The waits between tries of a submit or a reject the validator did not take."""


def send(
    call: Any, path: str, body: dict[str, Any], *, log: Any, sleep: Any = time.sleep
) -> tuple[int, Any]:
    """``call(path, body)`` until the validator takes it or refuses it for good: a lost answer (0),
    a busy one (429) or a server error (5xx) is tried again after :data:`SEND_DELAYS_S`; any other
    code (200, 404, 410, a 4xx refusal) is final. A lost submit used to end a delivered project as
    a timeout (2026-10-22 audit)."""
    code, out = call(path, body)
    for wait in SEND_DELAYS_S:
        if not (code == 0 or code == 429 or code >= 500):
            break
        log(f"{path} {body.get('job')}: {code}; trying again in {wait:g} s")
        sleep(wait)
        code, out = call(path, body)
    return code, out


KEY_CHECK_S = 3600.0
"""How often an idle miner checks its agent key and its admission (:func:`keep_admitted`)."""


def key_works(platform: str, key: str, *, timeout: float = 20.0) -> bool | None:
    """Whether the platform still takes the agent key (``GET /api/platform/key``): True on 200,
    False on 401 (disabled or deleted: the validator revoked it), None when it cannot tell."""
    req = urllib.request.Request(  # noqa: S310 -- the operator's own platform URL
        platform.rstrip("/") + "/api/platform/key",
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": f"cadmind-einstein-miner/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310
            return True
    except urllib.error.HTTPError as e:
        return False if e.code == 401 else None
    except (OSError, ValueError):
        return None


def keep_admitted(call: Any, mint: Any, works: Any, *, log: Any) -> None:
    """An idle miner's hourly check: a key the platform no longer takes (``works()`` False: the
    validator revoked it for abuse or deregistration) is minted again (``mint()``), and the
    admission is asked for again (one admission test a day at most; an admitted miner is told so).
    Before 0.4.5 both happened only at startup, so a disabled miner never came back."""
    if works() is False:
        log("the agent key is no longer taken by the platform: minting a new one")
        if not mint():
            return
    code, adm = call("/v1/miners/admission", {})
    if not (code == 200 and isinstance(adm, dict) and adm.get("admitted")):
        log(
            f"admission: {code} "
            f"{adm.get('job') or adm.get('error') if isinstance(adm, dict) else adm}"
        )


def deadline_file(a: argparse.Namespace) -> Path | None:
    """Where the miner writes each job's deadline (epoch ms) for the local `time_left` tool."""
    state = getattr(a, "state", None)
    return Path(state) / "deadline" if state else None


def mcp_config(a: argparse.Namespace, key: str, key_file: Path) -> dict[str, Any]:
    """The agent's ``mcpServers``: the platform's Watt server, and the local fetch server (unless
    ``--no-local-fetch``), which reads the agent key from ``key_file``."""
    servers: dict[str, Any] = {
        MCP_SERVER: {
            "type": "http",
            "url": a.platform.rstrip("/") + "/mcp",
            "headers": {"Authorization": f"Bearer {key}"},
        }
    }
    if getattr(a, "local_fetch", True):
        # absolute: the agent (and so the server) runs in the empty work directory
        deadline = deadline_file(a)
        servers[FETCH_SERVER] = fetch_server_config(
            a.platform.rstrip("/"), key_file.resolve(), deadline.resolve() if deadline else None
        )
    return {"mcpServers": servers}


def registered_on(network: str, netuid: int, hotkey: str) -> bool:
    """Whether ``hotkey`` is in the subnet's metagraph (one read)."""
    import bittensor as bt  # noqa: PLC0415

    from .chain import SubtensorReader  # noqa: PLC0415

    state = SubtensorReader(bt.Subtensor(network=network), netuid).read()
    return state.by_hotkey(hotkey) is not None


def start_self_review(
    a: argparse.Namespace,
    got: Build,
    *,
    review_off: str,
    job: str,
    ws: str,
    deadline_ms: int,
    key: str,
    questions_allowed: bool,
    correct: sr.Correct,
    cancelled: Any,
    log: Any,
    model: Any = None,
) -> sr.SelfReview | None:
    """The self-review round after the builder's run (:mod:`.self_review`), when it is on and
    the builder delivered: run, its final check before the submit when it fits, its loops.
    ``got.text`` becomes the corrected run's text when the corrected version is to be submitted.
    None when the round does not start (the reason is logged). Never raises."""
    if a.self_review != "on" or got.cancelled:
        return None
    kind, body = outcome(job, ws, got.text)
    why = (
        review_off
        or ("" if deadline_ms else "the job has no deadline")
        or ("" if kind == "submit" and body.get("digest") else "the builder did not deliver")
    )
    if why:
        log(f"{job}: self-review: skipped: {why}")
        return None
    try:
        if model is None:
            model = sr.judge_model(
                a.self_review_model,
                a.self_review_key_file,
                effort=a.self_review_effort,
                base_url=a.self_review_base_url,
            )
    except Exception as e:  # noqa: BLE001 -- no judge: the job goes on without the round
        log(f"{job}: self-review: skipped: the judge model: {type(e).__name__}: {str(e)[:200]}")
        return None
    rnd = sr.SelfReview(
        job=job,
        built_text=got.text,
        deadline_ms=deadline_ms,
        export=lambda: sr.workspace_export(a.platform, key, ws),
        status=lambda: sr.workspace_status(a.platform, key, ws),
        correct=correct,
        log=log,
        cancelled=cancelled,
        model=model,
        samples=a.self_review_samples,
        loops=a.self_review_loops,
        questions_allowed=questions_allowed,
    )
    rnd.run()
    if rnd.final_pending and rnd.final_fits() and not cancelled():
        rnd.final(after_submit=False)
    if not cancelled():
        rnd.loops()
    if rnd.text:
        got.text = rnd.text
    return rnd


def corrector(
    a: argparse.Namespace,
    got: Build,
    *,
    cmd: list[str],
    prompt: str,
    prompt_file: Path,
    mcp_file: Path,
    work_dir: Path,
    ws: str,
    job: str,
    watch: Any,
) -> sr.Correct:
    """The self-review's ``correct(message, budget_s)``: the builder once more with the message.
    Claude Code's session is resumed; an ``--agent-command`` agent runs again on the job's prompt
    told that the workspace holds its delivered work (:data:`.self_review.FRESH`)."""

    def correct(message: str, budget_s: float) -> tuple[str, bool]:
        stdin: str | None = None
        if got.session is not None:
            argv = got.session.resume(cmd, message)
        else:
            done = _DONE.search(got.text)
            text = prompt + sr.FRESH.format(digest=done.group(1) if done else "") + message
            _write_private(prompt_file, text)
            argv = agent_argv(
                a, prompt=text, prompt_file=prompt_file, mcp_file=mcp_file, ws=ws, job=job
            )
            stdin = text
        run = AgentRun(argv, cwd=work_dir, stdin=stdin)
        watch(run)
        try:
            out, err, timed_out = run.wait(budget_s)
        finally:
            watch(None)
        code = getattr(getattr(run, "proc", None), "returncode", None)
        got.record += (
            f"\n--- SELF-REVIEW ({int(budget_s // 60)} min) ---\n{message}\n--- answer ---\n"
        )
        got.record += _record(out, err, code if isinstance(code, int) else None, timed_out)
        return out, run.stopped.is_set() and not timed_out

    return correct


def parser() -> argparse.ArgumentParser:
    """The miner's flags (Bittensor's names, with the earlier dashed names as aliases)."""
    ap = argparse.ArgumentParser(prog="cadmind-einstein-miner")
    add_version_args(ap)
    add_wallet_args(ap, help_name="the bittensor wallet holding the miner's hotkey")
    add_subtensor_args(ap, network_default="finney", netuid_default=0)
    add_logging_args(ap)
    ap.add_argument("--dev-uri", help="a development key (//Alice): local stacks only")
    ap.add_argument("--validator", required=True, help="the validator's base URL")
    ap.add_argument("--validator-hotkey", required=True, help="the validator's SS58 hotkey")
    ap.add_argument("--platform", required=True, help="the CADmind platform's base URL")
    ap.add_argument("--state", type=Path, required=True, help="a private directory for the key")
    ap.add_argument("--agent-cli", default="claude", help="Claude Code's CLI (default: claude)")
    ap.add_argument("--model", default="", help="passed to the agent; empty: the CLI's default")
    ap.add_argument("--agent-arg", action="append", default=[], help="extra CLI argument")
    ap.add_argument("--agent-command", default="", help="any agent CLI: an argv template")
    ap.add_argument("--prompt-file", type=Path, help="a prompt template replacing the built-in one")
    ap.add_argument("--max-jobs", type=int, default=0, help="0: run forever")
    ap.add_argument(
        "--contact",
        default="",
        help="a short note for the subnet team with your key request (how to reach you)",
    )
    ap.add_argument(
        "--self-review",
        choices=sr.MODES,
        default="on",
        help="after the builder's DONE, the validators' judge reviews the built device on the "
        "miner's own key and the builder corrects the upheld points (default on; skipped when no "
        "--self-review-key-file is given)",
    )
    ap.add_argument(
        "--self-review-model",
        default=sr.DEFAULT_MODEL,
        help="the judge's model, provider:model (an openai: model goes to OpenRouter unless "
        f"--self-review-base-url says otherwise; default {sr.DEFAULT_MODEL})",
    )
    ap.add_argument(
        "--self-review-key-file",
        type=Path,
        default=None,
        help="a file holding the judge model's API key (your own account; never logged)",
    )
    ap.add_argument(
        "--self-review-base-url",
        default="",
        help="the judge model's API base URL, if not the default",
    )
    ap.add_argument(
        "--self-review-effort",
        default=sr.DEFAULT_EFFORT,
        help=f"the judge's reasoning effort (default {sr.DEFAULT_EFFORT}, as the validators' "
        "judge is calibrated)",
    )
    ap.add_argument(
        "--self-review-samples",
        type=int,
        default=sr.DEFAULT_SAMPLES,
        help=f"samples per check and final check (default {sr.DEFAULT_SAMPLES}, as a "
        "validator's vote)",
    )
    ap.add_argument(
        "--self-review-loops",
        type=int,
        default=sr.DEFAULT_LOOPS,
        help="further corrections while the final check finds a point still there or a new one "
        f"(default {sr.DEFAULT_LOOPS})",
    )
    ap.add_argument(
        "--no-local-fetch",
        dest="local_fetch",
        action="store_false",
        help="no local fetch_to_workspace tool: the agent fetches through the platform only",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    a = ap.parse_args(argv)

    if a.dev_uri:
        from bittensor.sp_core import Keypair  # noqa: PLC0415

        kp: Any = Keypair.create_from_uri(a.dev_uri)
    else:
        if not (a.wallet_name and a.wallet_hotkey):
            ap.error("--wallet.name and --wallet.hotkey (or --dev-uri) are required")
        import bittensor as bt  # noqa: PLC0415

        kp = bt.Wallet(**wallet_kwargs(a)).hotkey
    me = kp.ss58_address
    setup_logging(a, prefix=f"[einstein-miner {me[:8]}]", stream=sys.stdout)
    logger = logging.getLogger("cadmind_einstein.miner")
    template = a.prompt_file.read_text(encoding="utf-8") if a.prompt_file else PROMPT
    a.state.mkdir(parents=True, exist_ok=True)
    a.state.chmod(0o700)
    logs = a.state / "logs"
    logs.mkdir(exist_ok=True)
    work_dir = a.state / "empty"
    work_dir.mkdir(exist_ok=True)

    def log(msg: str) -> None:
        logger.info("%s %s", time.strftime("%H:%M:%S"), msg)

    log(f"cadmind-einstein-miner {__version__} (spec {__spec_version__})")
    if not a.no_version_check:
        check_latest(say=log)
    if a.netuid and not a.dev_uri:
        try:
            if not registered_on(network_of(a), a.netuid, me):
                log(
                    f"WARNING: hotkey {me} is not registered on netuid {a.netuid} "
                    f"({network_of(a)}): the validator will refuse it"
                )
        except Exception as e:  # noqa: BLE001 -- a startup hint only, never fatal
            log(f"registration check skipped: {type(e).__name__}: {e}")

    def call(path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return signed(kp, a.validator, path, body, a.validator_hotkey)

    review_off = sr.unavailable(a.self_review, a.self_review_model, a.self_review_key_file)
    if not review_off:
        log(
            f"self-review: on (judge {sr.JUDGE_VERSION} on {a.self_review_model} at effort "
            f"{a.self_review_effort}, {a.self_review_samples} samples, up to "
            f"{a.self_review_loops} loops; your key, your cost)"
        )
    elif a.self_review == "on":
        log(f"self-review: skipped on every job: {review_off}")

    key_file, mcp_file = a.state / "agent.key", a.state / "watt-mcp.json"

    def mint() -> bool:
        """A new agent key from the validator, stored with the MCP config that carries it."""
        priv, pub = new_keypair()
        # waits while the request is pending the subnet team's approval
        code, got = ask_key(call, pub, contact=a.contact, log=log)
        if code != 200:
            log(f"agent_key refused: {code} {got}")
            return False
        key = open_sealed(got["sealed"], priv, me)
        _write_private(key_file, key + "\n")
        _write_private(mcp_file, json.dumps(mcp_config(a, key, key_file)))
        log(f"agent key {got['key_id']} opened and stored")
        return True

    if key_file.exists():
        _write_private(mcp_file, json.dumps(mcp_config(a, key_file.read_text().strip(), key_file)))
        log("agent key loaded from state")
    elif not mint():
        return 1

    code, adm = call("/v1/miners/admission", {})
    log(f"admission requested: {code} {adm.get('job') if isinstance(adm, dict) else adm}")
    checked = time.monotonic()

    busy, stop, cancel = threading.Event(), threading.Event(), threading.Event()
    running: dict[str, Any] = {"job": "", "agent": None}

    def heartbeat() -> None:
        while not stop.is_set():
            if busy.is_set():
                # keep alive while the agent runs -- and stop it when its job is gone
                code, w = call("/v1/jobs/work", {})
                agent, job = running["agent"], running["job"]
                if code == 200 and job and dropped(w, job) and not cancel.is_set():
                    cancel.set()
                    log(f"{job}: cancelled or no longer ours: stopping the agent")
                stopped = getattr(agent, "stopped", None)
                if cancel.is_set() and agent is not None and not (stopped and stopped.is_set()):
                    agent.stop()
            stop.wait(15)

    threading.Thread(target=heartbeat, daemon=True).start()
    done = 0
    while not a.max_jobs or done < a.max_jobs:
        code, work = call("/v1/jobs/work", {})
        if code != 200 or not work.get("job"):
            if code not in (0, 200):
                log(f"work: {code} {work}")
            if (
                time.monotonic() - checked > KEY_CHECK_S
            ):  # idle: is the key still ours, are we admitted?
                checked = time.monotonic()
                keep_admitted(
                    call,
                    mint,
                    lambda: key_works(a.platform, key_file.read_text().strip()),
                    log=log,
                )
            time.sleep(5)
            continue
        job, ws, brief = work["job"], work["workspace"], work.get("brief") or ""
        if not work.get("accepted"):
            code, _ = call("/v1/jobs/accept", {"job": job})
            log(f"accepted {job} in {ws}: {code}")
            if code != 200:  # cancelled (410) or gone meanwhile: nothing to build
                time.sleep(5)
                continue
            code, work = call("/v1/jobs/work", {})  # the deadline starts at accept
        deadline_ms = int(work.get("deadline_ms") or 0) if isinstance(work, dict) else 0
        minutes = max(10, int((deadline_ms / 1000 - time.time()) / 60) - 3) if deadline_ms else 25
        # with the self-review, the builder is told an earlier deadline, so the round has its time
        # (a builder still working then is told the real one and goes on)
        reserve = sr.reserve_s(deadline_ms, time.time()) if not review_off else 0.0
        told_ms = deadline_ms - int(reserve * 1000) if deadline_ms else 0
        told_minutes = (
            max(10, int((told_ms / 1000 - time.time()) / 60) - 3) if deadline_ms else minutes
        )
        if reserve:
            log(
                f"{job}: the builder is told {told_minutes} min: {reserve / 60:.0f} min kept "
                "for the self-review"
            )
        # the job's own deadline for `time_left` (none written: it says no deadline is set)
        deadline = deadline_file(a)
        if deadline is not None:
            _write_private(deadline, str(told_ms) if deadline_ms else "")
        deadline_utc = (
            time.strftime("%H:%M", time.gmtime(told_ms / 1000))
            if deadline_ms
            else "the job's deadline"
        )
        prompt = template.format(
            ws=ws,
            job=job,
            brief=brief,
            minutes=told_minutes,
            deadline_utc=deadline_utc,
            server=MCP_SERVER,
            questions=questions_rule(work if isinstance(work, dict) else {}, ws),
            no_questions="yes" if isinstance(work, dict) and work.get("no_questions") else "no",
        )
        prompt_file = a.state / "prompt.txt"
        _write_private(prompt_file, prompt)
        cmd = agent_argv(
            a, prompt=prompt, prompt_file=prompt_file, mcp_file=mcp_file, ws=ws, job=job
        )
        t0 = time.time()
        out_file = logs / f"{job}.txt"

        def watch(agent: AgentRun | None) -> None:
            running.update(agent=agent)

        def real_deadline(path: Path | None = deadline, real: int = deadline_ms) -> None:
            if path is not None:
                _write_private(path, str(real))

        overrun = None
        if reserve and deadline is not None:
            overrun = threading.Timer(max(0.0, told_ms / 1000 - time.time()), real_deadline)
            overrun.daemon = True
            overrun.start()
        cancel.clear()
        running.update(job=job, agent=None)
        busy.set()
        rnd: sr.SelfReview | None = None
        try:
            got = build(
                cmd,
                session=session_for(a),
                cwd=work_dir,
                stdin=prompt if a.agent_command else None,
                timeout_s=minutes * 60,
                deadline_ms=told_ms or deadline_ms,
                watch=watch,
                log=log,
                job=job,
            )
            if overrun is not None:
                overrun.cancel()
                real_deadline()  # the self-review's corrections see the job's real deadline
            if got.timed_out:
                log(f"{job}: agent timed out")
            log(f"{job}: agent finished in {(time.time() - t0) / 60:.1f} min")
            got.cancelled = got.cancelled or cancel.is_set()
            rnd = start_self_review(
                a,
                got,
                review_off=review_off,
                job=job,
                ws=ws,
                deadline_ms=deadline_ms,
                key=key_file.read_text().strip() if key_file.exists() else "",
                questions_allowed=not (isinstance(work, dict) and work.get("no_questions")),
                correct=corrector(
                    a,
                    got,
                    cmd=cmd,
                    prompt=prompt,
                    prompt_file=prompt_file,
                    mcp_file=mcp_file,
                    work_dir=work_dir,
                    ws=ws,
                    job=job,
                    watch=watch,
                ),
                cancelled=cancel.is_set,
                log=log,
            )
            got.cancelled = got.cancelled or cancel.is_set()
        finally:
            if overrun is not None:
                overrun.cancel()
            busy.clear()
            running.update(job="", agent=None)
        _write_private(out_file, got.record)
        review_file = logs / f"{job}.self-review.json"
        if rnd is not None:
            _write_private(review_file, json.dumps(rnd.summary(), indent=1, default=str))
            log(f"{job}: {rnd.line()}")
        if got.cancelled:
            log(f"{job}: the job was cancelled; nothing submitted")
            done += 1
            continue
        kind, body = outcome(job, ws, got.text)
        if kind == "reject":
            code, out = send(call, "/v1/jobs/reject", body, log=log)
            log(f"rejected {job}: {code} {out}")
        else:
            code, out = send(call, "/v1/jobs/submit", body, log=log)
            log(f"submitted {job} digest={body['digest'][:16] or '(none)'}: {code} {out}")
        if rnd is not None and rnd.final_pending:
            # no time before the submit: the final check (a record only) runs after it
            rnd.final(after_submit=True)
            _write_private(review_file, json.dumps(rnd.summary(), indent=1, default=str))
            log(f"{job}: {rnd.line()}")
        done += 1
    stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
