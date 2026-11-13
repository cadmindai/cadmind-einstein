"""A reference miner: the signed validator protocol around an agent CLI that builds over MCP.

The protocol side is the same as :mod:`.fake_miner` (the miner's own agent key, taken by the
validator; admission, ``work`` / ``accept`` / ``submit`` / ``reject``); the build is an agent CLI
run in an empty directory, restricted to the hosted CADmind Watt MCP tools. One job at a time; a
heartbeat thread keeps polling ``work`` while the agent runs, so the validator keeps seeing the
miner alive.

The default agent is Claude Code's CLI (``claude -p``), because its flags restrict an agent to one
MCP server; any other agent CLI works through ``--agent-command`` (see below). The miner chooses,
configures and pays for its own model and CLI: this program passes no credential of its own and
assumes no particular plan. **Follow your model provider's terms** -- many consumer plans do not
allow automated or commercial use.

    cadmind-einstein-miner --wallet.name miner --wallet.hotkey m1 \\
        --validator https://VALIDATOR --validator-hotkey 5... \\
        --platform https://cadmind.ai --state ~/.local/share/cadmind-einstein-miner \\
        [--agent-key-file ~/.local/share/cadmind-einstein-miner/agent.key] \\
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

**A host refusal.** A `watt` answer that carries ``host_refused`` means the platform refused the
run's result for a reason of its own (never the design's). The agent is told to stop at once with a
``HOST_REFUSED <why>`` line; the miner does not resume it (only the platform can clear it) and
submits with ``host_refused`` (``{code, why}``). The validator reproduces it on its own guard run
and ends the round blocked by the platform: uncounted, scored for nobody, the project kept.

Every submit carries the miner's ``setup`` label (``--setup``, else ``$CADMIND_MINER_SETUP``),
by which the validator groups practice results. ``cadmind-einstein-miner practice [--status]``
(also ``cadmind-einstein practice``; :func:`practice_main`) asks for a practice job on a public
brief -- this running miner builds it like any job, never scored -- or prints the results.

A submit or a reject the validator did not take (no answer, 429 or 5xx) is sent again with a
backoff (:func:`send`).

**The agent key is the miner's own.** Its operator creates it on the developer platform, on the
account the hotkey is linked to, naming that hotkey, and gives its secret to this program in a
0600 file: ``--agent-key-file``, else ``$CADMIND_AGENT_KEY_FILE``, else ``STATE/agent.key``
(:func:`agent_key_file`). The validator never has the secret. At startup the miner asks the
validator to take the key (``agent_key``): the validator answers the id of the key the platform has
for the hotkey, and the miner checks that it is the key it runs on (:func:`key_mismatch`; the
platform's ``GET /api/agent/usage`` names the calling key) -- it stops when it is not. A request
the subnet team has not approved yet is answered "pending" (202), and a hotkey the platform has no
key for 409 ``miner_not_linked`` with what to do: the miner waits and asks again
(:func:`.fake_miner.ask_key`), with ``--contact`` sent as a note for the team. While idle, the
miner asks again every hour, and for its admission (:func:`keep_admitted`): a validator that
dropped the miner (a block that has since lifted, a deregistration) takes it back that way.

Watt compute is paid in credits. A miner needs none of its own: each job workspace the validator
grants carries its allowance, and that workspace is where the miner's key runs compute.

The agent also gets one local tool, ``fetch_to_workspace`` (:mod:`.fetch_mcp`, a stdio MCP server
named ``cadmind-fetch``): it downloads a public document from the miner's own connection (many sites
refuse the platform's address) and uploads it into the job's workspace, as the hosted ``fetch``
stores one. It is a fenced download, not a shell; ``--no-local-fetch`` leaves it out.

Claude Code's CLI runs **isolated from the operator's own Claude Code configuration**: the agent
learns Watt from the platform's ``watt_guide``, which matches the platform's version and is the same
for every miner, so no operator skill, memory, hook or ``CLAUDE.md`` may change a run or add
instructions to it. The command line carries ``--setting-sources project,local`` (the user settings
stay out; the empty work directory has no project or local ones), ``--settings`` with
:data:`ISOLATION_SETTINGS` (no claude.ai-synced skills or plugins, no auto-memory, no plugin) and
``--disable-slash-commands``; ``Skill`` is denied with the other built-in tools; and the environment
carries :data:`ISOLATION_ENV` (no ``CLAUDE.md``, no auto-memory). ``--agent-user-settings`` lets the
user settings back in (for a provider configured there); ``--agent-arg`` flags come last and win.

**Self-review** (``--self-review on``; off by default; :mod:`.self_review`): after the agent's
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

**Auto-update** (:mod:`.auto_update`, on by default from a git checkout): every 30 minutes the
miner reads the remote's release tags; a newer release is applied only between jobs -- at the top
of the job loop, when the validator's ``work`` names no accepted job of ours -- then the process
re-executes itself on the same command line. A job already accepted is always built and submitted
first; a job offered while an update is pending is accepted after the restart. A failed update
returns to the previous commit and the miner keeps running. ``--no-auto-update`` or
``CADMIND_MINER_AUTO_UPDATE=0`` turns it off.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import __spec_version__, __version__
from . import auto_update as au
from . import self_review as sr
from .answers import check as answers_check
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
from .version import check_latest

MCP_SERVER = "cadmind-watt"
BUILTIN_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebFetch",
    "NotebookEdit",
    "Task",
    "TodoWrite",
    "Skill",
)
"""Claude Code's built-in tools, denied by name: ``--allowedTools`` only pre-approves the MCP tools,
it does not take the built-ins away, and a hostile brief must not steer the agent into reading or
sending anything from the miner's machine. ``Skill`` is denied with them: the agent's knowledge
comes from Watt's ``watt_guide`` (see :data:`ISOLATION_SETTINGS`). ``WebSearch`` is not here: it
runs on the model provider's side and touches nothing on this machine (``WEB_SEARCH``).
``WebFetch`` stays denied, so every page the agent uses is downloaded with ``fetch_to_workspace``
(or the hosted ``fetch``), stored in the project and cited."""
WEB_SEARCH = "WebSearch"
"""Claude Code's web search, pre-approved: the agent needs real drawings and dimensions of every
product the device fits (positions of ports, buttons, cameras), not only a spec sheet's outline."""
SETTING_SOURCES = "project,local"
"""Claude Code's ``--setting-sources``: not ``user``, so the operator's own
``~/.claude/settings.json`` (its hooks, plugins, permission mode, environment) never reaches a
miner session. The agent runs in
the empty ``<state>/empty`` directory, which holds no project or local settings.
``--agent-user-settings`` adds ``user`` back."""
ISOLATION_SETTINGS: dict[str, Any] = {
    "syncClaudeAiSkills": False,
    "syncClaudeAiPlugins": False,
    "autoMemoryEnabled": False,
    "enabledPlugins": {},
}
"""The session's own settings (``--settings``): no skills or plugins synced from a claude.ai
account, no auto-memory, no plugin enabled. With ``--disable-slash-commands``, ``Skill`` denied and
:data:`ISOLATION_ENV`, the agent sees nothing of the operator's own Claude Code configuration:
it learns Watt from ``watt_guide``, served by the platform and matched to its version, so a run is
repeatable and no operator skill, memory or ``CLAUDE.md`` can add instructions to it."""
BASELINE_MODEL = "claude-sonnet-5-5"
BASELINE_EFFORT = "high"
"""CADmind's baseline (docs/MINER.md, "The baseline"): Claude Code running Sonnet 5.5 alone at
effort high, the setup CADmind's own miners run. A miner may change both; the quality this setup
delivers today is the bar a miner is held to."""
ISOLATION_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "ENABLE_TOOL_SEARCH": "false",
}
"""Set in the agent's environment for Claude Code's CLI: no ``CLAUDE.md`` is read (the user's, or
one in any parent directory), auto-memory is neither read nor written, and every tool is loaded
with the session instead of behind a tool search (a builder spent 133 round trips over 91 jobs
searching for its Watt tools, 2026-11-10)."""
KILL_GRACE_S = 10.0

PROMPT = """You are a CADmind einstein miner. Build this client's device in the Watt workspace \
`{ws}` (made from the brief), with the {server} MCP tools, the local `fetch_to_workspace` tool \
and web search. Use web search to find drawings and dimensions of every product the device must \
fit (its outline AND the positions and sizes of its buttons, ports, grilles, camera and other \
protrusions); store each page you use with `fetch_to_workspace` (or with the hosted `fetch` when \
the local tool is not there) and cite it. A spec sheet's overall size is not enough for a part \
that fits around a product. When a size the fit hangs on is known only to more than a few tenths \
of a mm and fit is what the client stressed: first measure a reference model of the product (the \
`photos` topic, `watt measure model`), then ask the client to measure it (ruler or calipers, naming
each number), and \
only then design from a drawing (to-scale community drawings, case and accessory makers' \
templates, cited with their stated tolerance); never default to the loose option. When the client \
cannot be asked, design to a bounded estimate only if the need is met across its whole range, else \
it is a missed need, below. Never refuse for missing data alone; never guess silently.

Start with `watt_guide` (brief, then primer, then scope): that is the core. Then read, with \
`watt_guide`, each topic your device needs, before you write what it covers: the primer's Topics \
table names them and says when to read each, and a topic's rules bind whether you read it or not. \
The client's photos, when they sent \
any (the brief names them), are in the workspace's brief/ (brief/photo-*): look at each with \
`view_image` before you design; they are the client's evidence, as binding as their words. Work \
fast and carefully:
1. First write DESIGN-NOTES.md: how the device is made, installed, used and serviced, what can go \
wrong at each step and the requirement/test proving it does not (walk the use the client asked for:
\
a step or requirement the brief does not ask for comes only from a need, never at the cost of what \
they did ask for, such as bulk or a looser fit); every critical number sourced \
(fetched into the project's assets/), never invented. Prefer the maker's own product page, \
datasheet or manual over retailer listings. Fetch maker pages and datasheets with \
`fetch_to_workspace` when you have it (it downloads from this builder's own connection, which many \
sites that block the hosted `fetch` answer; its source records add fetched_by = "builder"); the \
hosted `fetch` remains available. A site that blocks both: move on. Fetch only what the first \
sketch needs, then build.
2. Iterate with check / build / test; edit files, never rewrite them whole. Every build, test, \
inspect, render and report builds the whole design again (minutes on a large one): `check` is
cheap, \
so check while editing, then run ONE `test` (it builds) rather than build, inspect and test in a
row, \
and never run a heavy command again on files you have not changed. The final `report` comes \
last (step 5), never as a first check.
3. Give each bought hardware record a `price` (`"4.87 USD"`; a cable stock `"0.80 USD/m"`; a \
material `"25 USD/kg"`): DigiKey's qty-1 USD price from `lookup_part`, cited to its saved source, \
or a listing you fetched; never a guess -- leave it out when unknown. The client sees the bill.
4. Self-review, looking first: `watt` render, then `view_image` each picture of the WHOLE device \
(you have not seen it until you do; one render, before step 5). Write in DESIGN-NOTES.md what its \
buyer would object to on sight -- a square block where a hand, a face or a pocket goes, a part \
left bare or hanging, an edge the brief says to round -- and fix the worst of it while \
`time_left` allows: deliverable is the floor, not the goal. Then: load paths, retention, \
clearances, every clause tested on the whole feature it names \
(every corner and protrusion, not a convenient section). A must you write for a client need \
covers everything the need names (their "no sharp edges" is every outside edge of every part, \
not the one handle you rounded): what you cannot meet stays in the must, failing, until you fix \
the design, or is said to the client in a decision and your final lines; never narrow the must \
to what passes. Each control the device covers still \
worked by a real fingertip, through its opening or a flexible cover; a flexible part grips by \
interference (a press fit), not by play; fix the weakest points. Write the notes and your final \
lines from the model's current values (re-read them), never from an earlier plan.
5. Last, when nothing is left to change (prices, notes, tests and the review's fixes all done: \
any edit after it needs the whole guard again, many minutes), after a passing scoped guard \
(mutate with several `only`, one run) covering every must requirement: ONE `report` with mutate \
(the full guard and the delivery's files in one run; `watt_wait` on it). If it warns of thinly \
guarded musts, follow its one fix once: strengthen all the listed musts in one pass, check them \
with the scoped guard it names, then run the `report` once more; that report's pass stands, \
whatever it warns.
Never weaken a test to pass. A change round (brief/change-*.md on a delivered project) is \
delivered only when the whole project passes `watt status`: failures it inherits are part of the \
job, and your time is only what `time_left` says. {questions}
A `watt` run far longer than usual: stop it with `watt_cancel` and change the design. \
A `watt` answer with `host_refused` is the platform refusing that run's result for a reason of its \
own, never your design: do not run it again, work around it, drop or stand in a part, or weaken a \
test; end at once with exactly one line `HOST_REFUSED <the why it gave>` (the validator checks it \
on its own run, and the round ends blocked by the platform, against nobody).

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
each sub-device's job when they want, and a later round assembles them. \
An ASSEMBLY round (spec/subprojects.toml records with a `digest`, the built parts imported under \
subprojects/<id>/) keeps every imported part: never remove a record's `digest` or `from` to make a \
check pass; the platform refuses such a delivery (parts_dropped) and the job goes to another \
miner. When a part truly breaks one of this project's checks with the part imported, write ONE \
change request on that part's record ([subproject.<id>.change] with `test` = the failing check, \
`measured` = its numbers with the part imported, `proposal` = what the part should change), keep \
every other part imported, and deliver (DONE); the platform re-runs that check with the part \
imported and refuses a request it does not reproduce (change_unfounded). Otherwise, when the parts \
cannot be assembled, REJECT not-capable and say why. When you split a device, every connection \
between sub-devices (a lead, a hose, a signal) is an interface in each contract (a pin or port with
\
its signal and place) and this project wires them in its own src/wiring.py: an extension lead is \
this project's own cable, in its BOM and steps; a sub-device never stands in another sub-device's \
hardware as a world item to wire to, and every brief that says where a shared part sits says the
same. A workspace with \
brief/contract.toml is a sub-device: meet that contract (W0230) and publish spec/contract.toml; \
brief/parent.md, when there, is the parent's brief, for context. \
A workspace with brief/references.toml is an accessory of another device, `@<id>` in the brief: \
read the `references` topic first; you check against references/<id>/, numbers and shapes, which \
you never write. Its parts and files are not yours to deliver.
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
endings are `DONE`, `REJECT`, `HOST_REFUSED`, or `time_left` under 5 minutes.

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
and may answer while you work. Each question says what each option costs in plain numbers ("keep \
the drawing: about 1.5 mm of gap per side round the opening"); a limit that defines the client's \
stated priority (the play "fits perfectly" allows) is theirs: ask it, or build the strictest \
reasonable reading. Keep building on the assumption; never wait for an answer. After \
each `watt` command, call `client_answers` (workspace `{ws}`, `after` = the last `cursor` it \
gave; a `watt` answer also says when the client wrote and you have not read it): each new answer \
is a file under brief/ (the client's words). Record it in its decision (status = "answered", \
choice = the option they picked, answer = { from = "client: <their words, quoted from that file> \
(cadmind.ai, <date>)" }; keep its assumption line: an answer that picks the assumption you built \
on keeps the recorded guard), change what hangs on it, and re-run what `watt impact` names for \
that decision. A change request (brief/change-*.md) is the client's too: \
do what it asks; when it replaces an answer the client gave earlier, supersede the answered \
decision by a new one (status = "decided") whose found_by is { ref = "brief/change-<...>.md", \
reading = "<the client's words, quoted from that file>" }; a photo they send (kind photo, \
brief/photo-*) is theirs: look at it with \
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
        "--setting-sources",
        setting_sources(a),
        "--settings",
        json.dumps(ISOLATION_SETTINGS, separators=(",", ":")),
        "--disable-slash-commands",
        "--output-format",
        "text",
    ]
    if a.model:
        argv += ["--model", a.model]
    effort = getattr(a, "effort", "")
    if effort:
        argv += ["--effort", effort]
    return argv + list(a.agent_arg)


def setting_sources(a: argparse.Namespace) -> str:
    """Claude Code's ``--setting-sources``: :data:`SETTING_SOURCES`, with ``user`` first only when
    the operator asked for it (``--agent-user-settings``)."""
    if getattr(a, "agent_user_settings", False):
        return "user," + SETTING_SOURCES
    return SETTING_SOURCES


def agent_env(a: argparse.Namespace, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The agent's environment: the miner's own (``base``), plus :data:`ISOLATION_ENV` for Claude
    Code's CLI. An ``--agent-command`` agent gets the environment unchanged."""
    env = dict(os.environ if base is None else base)
    if not a.agent_command:
        env.update(ISOLATION_ENV)
    return env


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
        self,
        cmd: list[str],
        *,
        cwd: Path,
        stdin: str | None,
        env: Mapping[str, str] | None = None,
        grace_s: float = KILL_GRACE_S,
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
            env=None if env is None else dict(env),
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
_HOST = re.compile(r"^HOST_REFUSED\b:?[ \t]*(.*)$", re.MULTILINE)
"""The agent's ending when a `watt` answer carried ``host_refused`` (the platform refused the run's
result for a reason of its own): never resumed; submitted with ``host_refused`` and the why."""
HOST_WHY = "the platform refused a Watt run's result"
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
    """Whether the agent's output ends the job itself: a ``DONE``, ``REJECT`` or ``HOST_REFUSED``
    line. A host refusal is never resumed: only the platform can clear it."""
    return bool(_DONE.search(text) or _REJECT.search(text) or _HOST.search(text))


def host_refused(text: str) -> str:
    """The why of the agent's ``HOST_REFUSED`` line ('' when it wrote none)."""
    got = _HOST.search(text)
    if not got:
        return ""
    return " ".join(got.group(1).split())[:500] or HOST_WHY


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


class AgentLaunchError(Exception):
    """The agent program could not be started at all (not installed, not executable, its working
    directory missing): the job is handed back at once, never held to its deadline (v0.6.6)."""


LAUNCH_FAILURES_MAX = 3
"""Launch failures in a row after which the miner stops taking jobs (a broken setup)."""
LAUNCH_BACKOFF_S = 60.0
"""The wait after a launch failure, doubled at each one in a row."""
REJECT_LAUNCH = (
    "The builder's agent program could not start on the miner's machine, so this job goes to "
    "another builder."
)


def agent_launchable(argv: list[str]) -> str:
    """Why ``argv``'s program cannot be started ('' when it can): checked before any work is asked,
    so a broken setup never takes a client's job (v0.6.6)."""
    if not argv or not argv[0]:
        return "no agent command"
    exe = argv[0]
    if os.sep in exe or (os.altsep and os.altsep in exe):
        path = Path(exe).expanduser()
        if not path.is_file():
            return f"the agent program {exe} does not exist"
        if not os.access(path, os.X_OK):
            return f"the agent program {exe} is not executable"
        return ""
    if shutil.which(exe) is None:
        return f"the agent program {exe!r} is not on this miner's PATH"
    return ""


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
    env: Mapping[str, str] | None = None,
) -> Build:
    """Run the agent; while it ends without ``DONE`` or ``REJECT`` with more than
    :data:`RESUME_MIN_LEFT_S` before the deadline, run it again, up to :data:`MAX_RESUMES` times
    (there is no hand-over: an unfinished project earns nothing): the same session, or a new one
    told of its earlier work when the session is dead (:func:`dead`). ``watch(run)`` is told the
    running :class:`AgentRun` (``None`` once it ends), so a cancelled job can stop it. Each run's
    exit code and error output are logged and kept in the record. ``env``: the agent's environment
    (:func:`agent_env`; None: the miner's own)."""

    def one(argv: list[str], stdin_: str | None, timeout: float) -> tuple[str, str, bool, bool]:
        nonlocal record
        try:
            run = AgentRun(argv, cwd=cwd, stdin=stdin_, env=env)
        except OSError as e:  # the program, or its working directory, is not there
            raise AgentLaunchError(f"{type(e).__name__}: {e}") from None
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


SETUP_ENV = "CADMIND_MINER_SETUP"
"""The environment variable naming this miner's setup (its model, its prompt), sent as ``setup``
with every submit: the validator groups practice results by it (``--setup`` overrides it)."""


def outcome(job: str, ws: str, text: str, *, setup: str = "") -> tuple[str, dict[str, Any]]:
    """What to send for the agent's final output: ``("reject", body)`` for a ``REJECT`` line (not
    on an admission job; its message is everything from the line's message to the end, over
    several lines when it runs on), else ``("submit", body)``. The body carries a ``summary`` for
    the client: an ``UNFINISHED:`` line when the agent wrote one anyway, else its final text
    without the ``DONE`` line (whitespace collapsed, the last :data:`SUMMARY_CHARS` characters):
    on a delivered project too, the builder's notes (what it estimated, what is open) reach the
    client. A usage-limit notice (:data:`LIMIT`) is never sent as the builder's words. A
    ``HOST_REFUSED`` line adds ``host_refused`` (``{code, why}``) to the submit: the validator's own
    run reproduces it and ends the round blocked by the platform, against nobody."""
    rej, fin = _REJECT.search(text), _DONE.search(text)
    if rej and not job.startswith("admission-"):
        message = " ".join(text[rej.start(2) if rej.group(2) else rej.end() :].split())
        return "reject", {"job": job, "category": rej.group(1), "message": message}
    digest = fin.group(1) if fin else ""
    body: dict[str, Any] = {"job": job, "workspace": ws, "digest": digest}
    if setup:
        body["setup"] = setup
    host = host_refused(text)
    if host:
        body["host_refused"] = {"code": "host_refused", "why": host}
    left = _UNFINISHED.search(text)
    said = " ".join((left.group(1) if left else _HOST.sub("", _DONE.sub("", text))).split())
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
"""How often an idle miner asks the validator again for its key and its admission
(:func:`keep_admitted`)."""
AGENT_KEY_ENV = "CADMIND_AGENT_KEY_FILE"
"""The environment variable naming the file that holds the miner's agent key
(``--agent-key-file`` overrides it)."""
NO_AGENT_KEY = (
    "no agent key in {path}. Create a developer platform account, link this hotkey to it "
    "(a signature), create an agent key naming the hotkey, and save its secret in that file "
    "(mode 0600), or name another file with --agent-key-file (docs/MINER.md, section 2)"
)


def agent_key_file(a: argparse.Namespace) -> Path:
    """Where the miner's own agent key is: ``--agent-key-file``, else :data:`AGENT_KEY_ENV`,
    else ``agent.key`` in the state directory."""
    named = getattr(a, "agent_key_file", None) or os.environ.get(AGENT_KEY_ENV, "")
    return Path(named).expanduser() if named else Path(a.state) / "agent.key"


def read_agent_key(path: Path) -> str:
    """The agent key in ``path``; '' when the file is missing, unreadable or empty."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def own_key_id(platform: str, key: str, *, timeout: float = 20.0) -> str | None:
    """The id of the agent key ``key``, as the platform names the calling key (``GET
    /api/agent/usage`` -> ``key_id``): '' when the platform does not take the key (401: wrong,
    disabled or deleted), None when it cannot tell."""
    req = urllib.request.Request(  # noqa: S310 -- the operator's own platform URL
        platform.rstrip("/") + "/api/agent/usage",
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": f"cadmind-einstein-miner/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            got = json.loads(r.read(1 << 20) or b"{}")
        return str(got["key_id"]) if isinstance(got, dict) and got.get("key_id") else None
    except urllib.error.HTTPError as e:
        return "" if e.code == 401 else None
    except (OSError, ValueError):
        return None


def key_mismatch(named: str, mine: str | None) -> str:
    """Why this miner cannot work on its key: the validator answered ``named``, the id of the key
    the platform has for the hotkey, and ``mine`` is the id of the key this miner runs on
    (:func:`own_key_id`). '' when they are the same key, or when the platform could not tell."""
    if mine is None or mine == named:
        return ""
    have = f"is key {mine}" if mine else "is not taken by the platform (wrong, disabled or deleted)"
    return (
        f"the platform has agent key {named} for this hotkey, but the key this miner was given "
        f"{have}. The newest live agent key naming the hotkey is the one used: give this miner "
        "that key's secret, or create a new agent key naming the hotkey and give it that one"
    )


def keep_admitted(call: Any, take: Any, *, log: Any) -> None:
    """An idle miner's hourly check: the validator is asked again to take the miner's key
    (``take()``: a validator that dropped it -- a block since lifted, a deregistration -- looks it
    up again), and the admission is asked for again (one admission test a day at most; an admitted
    miner is told so)."""
    if not take():
        return
    code, adm = call("/v1/miners/admission", {})
    if not (code == 200 and isinstance(adm, dict) and adm.get("admitted")):
        log(
            f"admission: {code} "
            f"{adm.get('job') or adm.get('error') if isinstance(adm, dict) else adm}"
        )


def report_entry_test(
    stats: Any,
    *,
    log: Any,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
    max_wait_s: float = 1800.0,
) -> bool | None:
    """After the entry test is submitted: read this miner's signed stats (``stats() -> (status,
    answer)``: the validator's ``/v1/miners/stats``, which requests nothing) every minute until it
    says admitted or counts one more failed entry test, and say which. True admitted, False failed,
    None not decided within ``max_wait_s``."""

    def read() -> tuple[bool, int] | None:
        code, out = stats()
        if code != 200 or not isinstance(out, dict):
            return None
        fails = ((out.get("abuse") or {}).get("admission_failures") or {}).get("count", 0)
        return bool(out.get("admitted")), int(fails or 0)

    first = read()
    before = first[1] if first else 0
    start = clock()
    while True:
        now = read()
        if now and now[0]:
            log("entry test passed: this miner is ADMITTED -- client jobs and weights from now on")
            return True
        if now and now[1] > before:
            log(
                "entry test NOT passed: the validator counted a failed entry test; "
                "see cadmind-miner-stats"
            )
            return False
        if clock() - start >= max_wait_s:
            log("entry test: no verdict yet after 30 min; check cadmind-miner-stats")
            return None
        sleep(60.0)


def setup_label(a: argparse.Namespace) -> str:
    """This miner's setup label: ``--setup``, else :data:`SETUP_ENV` (one line, 80 characters)."""
    from .practice import clean_setup  # noqa: PLC0415

    raw = a.setup if getattr(a, "setup", None) is not None else os.environ.get(SETUP_ENV, "")
    return clean_setup(raw)


def deadline_file(a: argparse.Namespace) -> Path | None:
    """Where the miner writes each job's deadline (epoch ms) for the local `time_left` tool."""
    state = getattr(a, "state", None)
    return Path(state) / "deadline" if state else None


def mcp_config(
    a: argparse.Namespace, key: str, key_file: Path, deadline_ms: int = 0
) -> dict[str, Any]:
    """The agent's ``mcpServers``: the platform's Watt server, and the local fetch server (unless
    ``--no-local-fetch``), which reads the agent key from ``key_file``. ``deadline_ms``, the
    deadline the builder is told: sent with each request (``X-Watt-Deadline``), so every ``watt``
    answer says the minutes left."""
    headers = {"Authorization": f"Bearer {key}"}
    if deadline_ms:
        headers["X-Watt-Deadline"] = str(int(deadline_ms))
    servers: dict[str, Any] = {
        MCP_SERVER: {
            "type": "http",
            "url": a.platform.rstrip("/") + "/mcp",
            "headers": headers,
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
        or ("the platform refused a run's result" if body.get("host_refused") else "")
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


ANSWER_CHECKS = 2
"""How many times the builder is sent back to record the client's answers before the hand-over."""
ANSWERS_MIN_LEFT_S = 5 * 60
"""No such correction starts with less than this left before the submit's reserve."""
ANSWERS_NOTE = """Before the hand-over, the check the validator runs on the client's answers was \
run on your project. It would refuse the delivery:

{why}

Correct exactly that in the project's decisions (spec/), from what the client's files under brief/ \
say. Then run `watt status` again; if the digest changed, run what you ran before your first DONE \
line again. End with your final lines for the client and the DONE line of the digest now \
delivered. You have about {minutes} minutes."""


def answers_round(
    got: Build,
    *,
    job: str,
    ws: str,
    deadline_ms: int,
    no_questions: bool,
    export: Callable[[], bytes | None],
    correct: sr.Correct,
    cancelled: Callable[[], bool],
    log: Callable[[str], None],
    clock: Callable[[], float] = time.time,
) -> int:
    """Before the submit: the validator's own check on the client's answers (:mod:`.answers`), run
    by the builder on its workspace's export. While it would refuse the delivery and there is time,
    the builder runs once more with the reason (at most :data:`ANSWER_CHECKS` times), so that a
    decision recorded wrongly costs minutes, not the round. ``got.text`` becomes the corrected run's
    text when it ends with a ``DONE`` line. ``no_questions``: also refuse an open client decision
    (False on a style round, whose settled decisions stay open). Returns the corrections made.
    Never raises."""
    made = 0
    try:
        while not (got.cancelled or cancelled()):
            kind, body = outcome(job, ws, got.text)
            if kind != "submit" or not body.get("digest") or body.get("host_refused"):
                break
            data = export()
            if data is None:
                log(f"{job}: answers check: skipped: the workspace's export did not come")
                break
            why = answers_check(data, no_questions=no_questions)
            if not why:
                if made:
                    log(f"{job}: answers check: passes after {made} correction(s)")
                break
            left = deadline_ms / 1000 - clock() - sr.SUBMIT_RESERVE_S if deadline_ms else 0.0
            if made >= ANSWER_CHECKS or left < ANSWERS_MIN_LEFT_S:
                log(f"{job}: answers check: handed over as it is ({why[:300]})")
                break
            log(f"{job}: answers check: the builder runs again: {why[:300]}")
            made += 1
            text, stopped = correct(
                ANSWERS_NOTE.format(why=why, minutes=max(1, int(left // 60))), left
            )
            if stopped:
                got.cancelled = True
                break
            if _DONE.search(text):
                got.text = text
    except Exception as e:  # noqa: BLE001 -- the check is the builder's own help, never its end
        log(f"{job}: answers check: skipped: {type(e).__name__}: {str(e)[:200]}")
    return made


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
        run = AgentRun(argv, cwd=work_dir, stdin=stdin, env=agent_env(a))
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
    ap.add_argument("--state", type=Path, required=True, help="this miner's private directory")
    ap.add_argument(
        "--agent-key-file",
        type=Path,
        help="this miner's own agent key (0600): made on the developer platform, on the account "
        f"its hotkey is linked to, naming the hotkey (default: ${AGENT_KEY_ENV}, else "
        "STATE/agent.key)",
    )
    ap.add_argument("--agent-cli", default="claude", help="Claude Code's CLI (default: claude)")
    ap.add_argument(
        "--model",
        default=BASELINE_MODEL,
        help=f"passed to the agent (default {BASELINE_MODEL}, the baseline: docs/MINER.md); "
        "empty: the CLI's default",
    )
    ap.add_argument(
        "--effort",
        default=BASELINE_EFFORT,
        help=f"the agent's reasoning effort (default {BASELINE_EFFORT}, the baseline); empty: the "
        "CLI's default",
    )
    ap.add_argument("--agent-arg", action="append", default=[], help="extra CLI argument")
    ap.add_argument(
        "--agent-user-settings",
        action="store_true",
        help="Claude Code's CLI also reads your user settings (~/.claude/settings.json: its hooks, "
        "plugins, environment); off by default, so no operator configuration reaches the agent. "
        "CLAUDE.md, auto-memory, skills and slash commands stay off either way",
    )
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
        default="off",
        help="on: after the builder's DONE, the validators' judge reviews the built device on the "
        "miner's own key and the builder corrects the upheld points (default off; skipped when no "
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
        "--setup",
        default=None,
        help=f"a short label for this setup (model, prompt), sent with each submit; practice "
        f"results are grouped by it (default: ${SETUP_ENV})",
    )
    ap.add_argument(
        "--no-local-fetch",
        dest="local_fetch",
        action="store_false",
        help="no local fetch_to_workspace tool: the agent fetches through the platform only",
    )
    au.add_args(ap)
    return ap


def practice_parser() -> argparse.ArgumentParser:
    """``practice [--status]``: the miner's own practice (docs/MINER.md "Trying a new setup")."""
    ap = argparse.ArgumentParser(
        prog="cadmind-einstein practice",
        description="Ask the validator for one practice job on a public brief (never scored; "
        "the running miner builds it), or show your practice results with --status.",
    )
    add_wallet_args(ap, help_name="the bittensor wallet holding the miner's hotkey")
    ap.add_argument("--dev-uri", help="a development key (//Alice): local stacks only")
    ap.add_argument("--validator", required=True, help="the validator's base URL")
    ap.add_argument("--validator-hotkey", required=True, help="the validator's SS58 hotkey")
    ap.add_argument("--status", action="store_true", help="your practice results so far")
    ap.add_argument("--json", action="store_true", help="print the validator's answer as JSON")
    return ap


def _score(x: Any) -> str:
    return "-" if x is None else f"{float(x):g}"


def _q(x: Any) -> str:
    return '"' + str(x or "") + '"'


def practice_lines(got: Mapping[str, Any]) -> list[str]:
    """The ``--status`` answer, a few lines per result and one per setup."""
    out: list[str] = []
    for o in got.get("open") or []:
        out.append(f"{o.get('practice')}  {_q(o.get('title'))}  {o.get('state')}")
    for r in got.get("results") or []:
        head = f"{r.get('practice')}  {_q(r.get('title'))}"
        if r.get("deliverable"):
            head += f"  yours {_score(r.get('score'))}"
            head += f"  delivered {_score(r.get('reference_score'))}"
            head += f"  (judge {r.get('judge')})" if r.get("judge") else ""
        else:
            head += f"  not deliverable: {str(r.get('why') or '')[:160]}"
        tail = f"{r.get('minutes')} min" if r.get("minutes") is not None else ""
        if r.get("setup"):
            tail += f"  setup {_q(r.get('setup'))}"
        if r.get("unscored"):
            tail += f"  no vote: {str(r.get('unscored'))[:80]}"
        out.append(head)
        out.append(f"    {r.get('state')}  {tail.strip()}")
    for name, g in (got.get("by_setup") or {}).items():
        out.append(
            f"setup {_q(name or '(none)')}: {g.get('runs')} run(s), {g.get('deliverable')} "
            f"deliverable, mean {_score(g.get('mean_score'))} vs delivered "
            f"{_score(g.get('mean_reference_score'))} over {g.get('voted')} voted"
        )
    today = got.get("today") or {}
    if today:
        out.append(f"today: {today.get('started')} of {today.get('cap')} practice job(s) started")
    if got.get("note"):
        out.append(str(got["note"]))
    return out or ["no practice yet"]


def practice_main(argv: list[str] | None = None) -> int:
    """``cadmind-einstein practice [--status]`` (also ``cadmind-einstein-miner practice``): ask
    for one practice job -- the running miner picks it up on its next ``work`` like any job -- or
    print the results so far."""
    ap = practice_parser()
    a = ap.parse_args(argv)
    if a.dev_uri:
        from bittensor.sp_core import Keypair  # noqa: PLC0415

        kp: Any = Keypair.create_from_uri(a.dev_uri)
    else:
        if not (a.wallet_name and a.wallet_hotkey):
            ap.error("--wallet.name and --wallet.hotkey (or --dev-uri) are required")
        import bittensor as bt  # noqa: PLC0415

        kp = bt.Wallet(**wallet_kwargs(a)).hotkey
    if a.status:
        code, got = signed(kp, a.validator, "/v1/practice", {}, a.validator_hotkey, method="GET")
    else:
        code, got = signed(kp, a.validator, "/v1/practice", {}, a.validator_hotkey)
    if a.json or code != 200 or not isinstance(got, dict):
        sys.stdout.write(json.dumps({"status": code, "answer": got}, indent=1) + "\n")
        return 0 if code == 200 else 1
    if a.status:
        sys.stdout.write("\n".join(practice_lines(got)) + "\n")
    else:
        sys.stdout.write(
            f"practice {got.get('practice')} {_q(got.get('title'))}: {got.get('state')} -- the "
            "running miner builds it like any job; nothing is scored\n"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["practice"]:
        return practice_main(args[1:])
    ap = parser()
    a = ap.parse_args(args)
    setup = setup_label(a)

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
    if setup:
        log(f"setup label: {setup!r} (sent with each submit; practice results are grouped by it)")
    if not a.no_version_check:
        check_latest(say=log)
    updater = au.from_args(a, log=log)
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

    key_file, mcp_file = agent_key_file(a), a.state / "watt-mcp.json"
    if not read_agent_key(key_file):
        log(NO_AGENT_KEY.format(path=key_file))
        return 1
    _write_private(mcp_file, json.dumps(mcp_config(a, read_agent_key(key_file), key_file)))
    log(f"agent key read from {key_file}")

    def take() -> bool:
        """Ask the validator to take this miner's key; True once it names the key in the file.
        Stops the miner when the platform has another key for the hotkey."""
        # waits while the request is pending the subnet team's approval, or the hotkey unlinked
        code, got = ask_key(call, contact=a.contact, log=log)
        if code != 200:
            log(f"agent_key refused: {code} {got}")
            return False
        named = str(got.get("key_id") or "")
        why = key_mismatch(named, own_key_id(a.platform, read_agent_key(key_file)))
        if why:
            log(why)
            raise SystemExit(1)
        log(f"agent key {named} taken by the validator")
        return True

    why_not = agent_launchable(
        agent_argv(
            a, prompt="", prompt_file=a.state / "prompt.txt", mcp_file=mcp_file, ws="", job=""
        )
    )
    if why_not:
        log(
            f"cannot start: {why_not}. Fix the miner's agent setup (--agent-cli or "
            "--agent-command) and start it again"
        )
        return 2

    if not take():
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
    if updater is not None:
        updater.watch(busy.is_set, stop)  # checks during a job; applied only once it has ended
    done = 0
    launch_failures = 0
    while not a.max_jobs or done < a.max_jobs:
        code, work = call("/v1/jobs/work", {})
        # the idle point: no job of ours is accepted (a job ended above, before this poll). A
        # pending update is applied here and the miner restarts, before it accepts a job offered
        # meanwhile; an accepted job (one resumed after a restart) is always built first
        if (
            updater is not None
            and code == 200
            and isinstance(work, dict)
            and not (work.get("job") and work.get("accepted"))
            and updater.between_jobs(job_waiting=bool(work.get("job")))
        ):
            stop.set()
            return 0  # only an injected restart returns: the real one replaces this process
        if code != 200 or not work.get("job"):
            if code not in (0, 200):
                log(f"work: {code} {work}")
            if (
                time.monotonic() - checked > KEY_CHECK_S
            ):  # idle: does the validator still take our key, are we admitted?
                checked = time.monotonic()
                keep_admitted(call, take, log=log)
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
        # and on each Watt request, so every `watt` answer says the minutes left
        _write_private(
            mcp_file,
            json.dumps(mcp_config(a, key_file.read_text().strip(), key_file, told_ms)),
        )
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
        launch_error = ""
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
                env=agent_env(a),
            )
            if overrun is not None:
                overrun.cancel()
                real_deadline()  # the self-review's corrections see the job's real deadline
            if got.timed_out:
                log(f"{job}: agent timed out")
            log(f"{job}: agent finished in {(time.time() - t0) / 60:.1f} min")
            got.cancelled = got.cancelled or cancel.is_set()
            agent_key = key_file.read_text().strip() if key_file.exists() else ""
            asked = not (isinstance(work, dict) and work.get("no_questions"))
            again = corrector(
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
            )
            rnd = start_self_review(
                a,
                got,
                review_off=review_off,
                job=job,
                ws=ws,
                deadline_ms=deadline_ms,
                key=agent_key,
                questions_allowed=asked,
                correct=again,
                cancelled=cancel.is_set,
                log=log,
            )
            got.cancelled = got.cancelled or cancel.is_set()
            answers_round(
                got,
                job=job,
                ws=ws,
                deadline_ms=deadline_ms,
                no_questions=not asked and not (isinstance(work, dict) and work.get("style")),
                export=functools.partial(sr.workspace_export, a.platform, agent_key, ws),
                correct=again,
                cancelled=cancel.is_set,
                log=log,
            )
            got.cancelled = got.cancelled or cancel.is_set()
        except AgentLaunchError as e:
            launch_error = str(e)
        finally:
            if overrun is not None:
                overrun.cancel()
            busy.clear()
            running.update(job="", agent=None)
        if launch_error:
            # hand the job back at once (another miner takes it), wait; stop after a few in a row
            launch_failures += 1
            log(f"{job}: the agent could not start ({launch_error}): handing the job back")
            reject = {"job": job, "category": "not-capable", "message": REJECT_LAUNCH}
            code, out = send(call, "/v1/jobs/reject", reject, log=log)
            log(f"rejected {job}: {code} {out}")
            if launch_failures >= LAUNCH_FAILURES_MAX:
                log(
                    f"the agent could not start {launch_failures} times in a row: stopping. "
                    "Fix the miner's agent setup and start it again"
                )
                stop.set()
                return 2
            time.sleep(LAUNCH_BACKOFF_S * 2 ** (launch_failures - 1))
            continue
        launch_failures = 0
        _write_private(out_file, got.record)
        review_file = logs / f"{job}.self-review.json"
        if rnd is not None:
            _write_private(review_file, json.dumps(rnd.summary(), indent=1, default=str))
            log(f"{job}: {rnd.line()}")
        if got.cancelled:
            log(f"{job}: the job was cancelled; nothing submitted")
            done += 1
            continue
        kind, body = outcome(job, ws, got.text, setup=setup)
        if kind == "reject":
            code, out = send(call, "/v1/jobs/reject", body, log=log)
            log(f"rejected {job}: {code} {out}")
        else:
            if body.get("host_refused"):
                log(
                    f"{job}: the platform refused a Watt run's result "
                    f"({body['host_refused']['why'][:200]}): reported, not resumed"
                )
            code, out = send(call, "/v1/jobs/submit", body, log=log)
            log(f"submitted {job} digest={body['digest'][:16] or '(none)'}: {code} {out}")
            if code == 200 and str(job).startswith("admission-"):
                report_entry_test(
                    lambda: signed(kp, a.validator, "/v1/miners/stats", {}, a.validator_hotkey),
                    log=log,
                )
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
