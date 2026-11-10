"""A scripted miner for local end-to-end runs (DESIGN.md): no model, the miner's side of the
protocol for real.

It runs on its own agent key (``--agent-key-file``: made on the developer platform, naming its
hotkey), asks the validator to take that key (signed; the validator answers the key's id), polls
``work``, accepts, and builds a reference Watt project in the job workspace **through the platform's
MCP server** -- each file written with ``write_file``, its assets fetched with ``fetch``, then
``watt check``, ``build``, ``test`` and ``test --mutate`` -- reads the digest with ``watt status``
and submits.

    python -m cadmind_einstein.fake_miner --uri //Alice --validator http://127.0.0.1:8793 \
        --validator-hotkey 5V... --platform http://127.0.0.1:PORT --agent-key-file K \
        --project wall-hook [--jobs 1]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .desk import host_refusal


def ask_key(
    call: Any,
    *,
    contact: str = "",
    log: Any = print,
    sleep: Any = time.sleep,
    max_wait_s: float = 0.0,
    clock: Any = time.monotonic,
) -> tuple[int, Any]:
    """Ask the validator to take this miner's agent key (``call(path, body) -> (status,
    answer)``) and wait while the request is pending the operator's approval (202 ``{"pending":
    true}``, asked again every ``retry_after_s`` the validator names) or while the platform knows
    no key for the hotkey (409 ``miner_not_linked``: the platform's instruction is logged, and the
    miner asks again every :data:`NOT_LINKED_RETRY_S`); at most ``max_wait_s`` in all (0: no
    limit). Answers the last (status, answer): 200 with ``key_id``, the id of the key the
    platform has for the hotkey, once approved."""
    body: dict[str, Any] = {"contact": contact} if contact else {}
    start = clock()
    asks = 0
    said = False
    while True:
        code, got = call("/v1/miners/agent_key", body)
        asks += 1
        if code == 403 and _not_registered_yet(got):
            # just registered: the validator reads the chain every few minutes; wait, never exit
            if asks == 1:
                log(
                    "agent key: the validator does not see this hotkey registered yet "
                    "(it reads the "
                    "chain every few minutes); asking again every minute"
                )
            wait = REGISTRATION_RETRY_S
        elif code == 409 and isinstance(got, dict) and got.get("code") == NOT_LINKED:
            wait = NOT_LINKED_RETRY_S
            log(
                f"agent key: the platform has no agent key for this hotkey -- {got.get('error')}; "
                f"asking again in {wait / 60:.0f} min"
            )
        elif code != 202 or not isinstance(got, dict) or not got.get("pending"):
            return code, got
        else:
            wait = max(60.0, float(got.get("retry_after_s") or 900))
            if not said:
                log(f"agent key: pending approval -- {got.get('message') or 'ask again later'}")
                said = True
            log(
                f"agent key: still pending at {time.strftime('%H:%M', time.gmtime())} UTC; "
                f"asking again in {wait / 60:.0f} min"
            )
        if max_wait_s and clock() - start + wait > max_wait_s:
            return code, got
        sleep(wait)


NOT_LINKED = "miner_not_linked"
NOT_LINKED_RETRY_S = 900.0
"""how often a miner asks again while the platform has no agent key for its hotkey"""
REGISTRATION_RETRY_S = 60.0
"""how often a miner asks again while the validator does not see its registration yet"""


def _not_registered_yet(answer: Any) -> bool:
    """A 403 that only means the validator has not read the miner's registration yet."""
    text = answer.get("error") if isinstance(answer, dict) else answer
    return "not registered" in str(text or "").lower()

    return False


def signed(
    keypair: Any,
    base: str,
    path: str,
    body: Mapping[str, Any],
    receiver: str,
    *,
    require_signed: bool = True,
    method: str = "POST",
) -> tuple[int, Any]:
    """POST (or, ``method="GET"``, GET with no body) a ``btauth/1``-signed request to the
    validator ``receiver``; its answer must carry the validator's signature over this request
    (:func:`.service.verify_response`), or it is taken as no answer (status 0) -- unless
    ``require_signed`` is off."""
    import bittensor.http_auth as btauth  # noqa: PLC0415

    from .service import verify_response  # noqa: PLC0415

    get = method.upper() == "GET"
    raw = b"" if get else json.dumps(dict(body)).encode()
    auth = btauth.sign(keypair, method=method.upper(), path=path, body=raw, receiver_ss58=receiver)
    headers = {**({} if get else {"Content-Type": "application/json"}), **auth}
    req = urllib.request.Request(  # noqa: S310 -- configured
        base.rstrip("/") + path, data=None if get else raw, headers=headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            code, answer, got = r.status, r.read(), dict(r.headers.items())
    except urllib.error.HTTPError as e:
        code, answer, got = e.code, e.read(), dict(e.headers.items())
    except OSError as e:  # the validator restarting: said as no answer, and the loop polls again
        return 0, {"error": f"validator unreachable: {type(e).__name__}"}
    if require_signed and not verify_response(
        got,
        answer,
        status=code,
        method=method.upper(),
        path=path,
        nonce=auth["X-Bittensor-Nonce"],
        hotkey=auth["X-Bittensor-Hotkey"],
        validator=receiver,
    ):
        return 0, {"error": f"the validator's answer ({code}) is not signed by {receiver}"}
    try:
        return code, json.loads(answer or b"{}")
    except ValueError:
        return code, {"error": "the validator's answer is not JSON"}


class Mcp:
    """The platform's MCP server, as any MCP client reaches it: JSON-RPC over HTTP with the agent
    key."""

    def __init__(self, platform: str, key: str) -> None:
        self.url, self.key, self.n = platform.rstrip("/") + "/mcp", key, 0

    def _rpc(self, method: str, params: Mapping[str, Any]) -> Any:
        self.n += 1
        raw = json.dumps(
            {"jsonrpc": "2.0", "id": self.n, "method": method, "params": dict(params)}
        ).encode()
        req = urllib.request.Request(  # noqa: S310 -- a configured http(s) URL
            self.url,
            data=raw,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        # the platform's per-key request rate, and a restart or a busy moment behind its proxy (502,
        # 503, 504, as the agent miner's own send): wait it out
        for wait in (2, 5, 10, 20, 30, 0):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
                    out = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code not in (429, 502, 503, 504) or not wait:
                    raise
                time.sleep(min(60.0, float(e.headers.get("Retry-After") or wait)))
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out["result"]

    def init(self) -> None:
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fake-miner", "version": "1"},
            },
        )

    def call(self, tool: str, **args: Any) -> Any:
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        text = "".join(
            c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"
        )
        try:
            out = json.loads(text)
        except ValueError:
            out = {"text": text}
        if res.get("isError"):
            raise RuntimeError(f"{tool}: {text[:300]}")
        return out

    def watt(self, ws: str, command: str, **args: Any) -> Any:
        """A run's full JSON (`detail`: the MCP tools answer an agent short text by default). A run
        whose result the platform refused for a reason of its own (``host_refused``) raises
        :class:`HostRefusedError`: the miner stops and reports it."""
        out = self.call("watt", workspace=ws, command=command, wait_s=100, detail=True, **args)
        while out.get("state") == "running":
            out = self.call("watt_wait", run=out["run"], wait_s=100, detail=True)
        why = host_refusal(out)
        if why:
            raise HostRefusedError(why)
        return out


class HostRefusedError(Exception):
    """A `watt` answer carried ``host_refused``: not the design's failure, only the platform can
    clear it; the miner submits with ``host_refused`` and the why."""


def answers(mcp: Mcp, ws: str, after: int, log: Any) -> int:
    """The client's new answers (the platform's ``client_answers``), logged; the next cursor. This
    scripted miner rebuilds a fixed project and records none: a job its client answered fails the
    gate, which is what an end-to-end test of the answers check wants. A platform without the tool
    answers nothing new."""
    try:
        out = mcp.call("client_answers", workspace=ws, after=after)
    except RuntimeError as e:
        log(f"client_answers: {str(e)[:120]}")
        return after
    for a in out.get("answers") or []:
        log(f"client {a.get('kind')}: {a.get('path')}")
    return int(out.get("cursor") or after)


def decide_open(decisions: str) -> str:
    """A ``decisions.toml`` with every open client decision made the builder's own, on the option it
    was built on, as a job that may not ask has it (the agent primer's rule). The reference project
    is free to keep a question open: Watt's worked example does, to show one; an entry test, which
    has no client to answer it, would fail the gate on it."""
    client, opened = r'(?m)^by\s*=\s*"client"', r'(?m)^status\s*=\s*"open"'

    def decided(block: str) -> str:
        if not (re.search(client, block) and re.search(opened, block)):
            return block
        mine = re.sub(opened, 'status = "decided"', re.sub(client, 'by = "agent"', block))
        return re.sub(r"(?m)^assumption\s*=", "choice =", mine)

    return "".join(decided(block) for block in re.split(r"(?m)^(?=\[)", decisions))


def build(mcp: Mcp, ws: str, project: Path, log: Any, *, no_questions: bool = False) -> str:
    """Rebuild the reference project in the workspace; answer the digest Watt reports."""
    sources = tomllib.loads((project / "spec" / "sources.toml").read_text()).get("source", {})
    for s in sources.values():
        if s.get("asset"):
            mcp.call("fetch", workspace=ws, url=s["url"], save_as=Path(s["asset"]).name)
    files = (
        sorted(project.glob("spec/*.toml"))
        + sorted(project.rglob("src/**/*.py"))
        + sorted(project.glob("tests/test_*.py"))
    )
    for f in files:
        path = f.relative_to(project).as_posix()
        content = f.read_text()
        if no_questions and path == "spec/decisions.toml":
            content = decide_open(content)
        mcp.call("write_file", workspace=ws, path=path, content=content)
    log(f"wrote {len(files)} files")
    cursor = 0
    for command, args in (("check", {}), ("build", {}), ("test", {}), ("test", {"mutate": True})):
        out = mcp.watt(ws, command, **args)
        log(f"watt {command}{' --mutate' if args else ''}: {'ok' if out.get('ok') else 'errors'}")
        cursor = answers(mcp, ws, cursor, log)
    status = mcp.watt(ws, "status")
    st = ((status.get("data") or {}).get("status")) or {}
    log(f"status: stage {st.get('stage')}, deliverable {st.get('deliverable')}")
    return str(st.get("digest") or "")


def main(argv: list[str] | None = None) -> int:
    from bittensor.sp_core import Keypair  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="fake-miner")
    ap.add_argument("--uri", required=True)
    ap.add_argument("--validator", required=True)
    ap.add_argument("--validator-hotkey", required=True)
    ap.add_argument("--platform", required=True)
    ap.add_argument(
        "--agent-key-file",
        type=Path,
        required=True,
        help="this miner's own agent key (made on the developer platform, naming its hotkey)",
    )
    ap.add_argument("--project", type=Path, required=True, help="the reference project to rebuild")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--contact", default="", help="a note for the operator with the key request")
    ap.add_argument("--idle-s", type=float, default=900.0)
    ap.add_argument("--fail", action="store_true", help="accept, then submit without building")
    ap.add_argument("--reject", default="", help="reject every job with this message (not-allowed)")
    a = ap.parse_args(argv)
    kp = Keypair.create_from_uri(a.uri)
    me = kp.ss58_address

    def log(msg: str) -> None:
        print(f"[fake-miner {me[:8]}] {msg}", file=sys.stderr, flush=True)  # noqa: T201

    key = a.agent_key_file.read_text(encoding="utf-8").strip()
    code, got = ask_key(
        lambda path, body: signed(kp, a.validator, path, body, a.validator_hotkey),
        contact=a.contact,
        log=log,
        max_wait_s=a.idle_s,
    )
    if code != 200:
        log(f"agent_key refused: {code} {got}")
        return 1
    log(f"agent key {got['key_id']} taken by the validator")
    mcp = Mcp(a.platform, key)
    mcp.init()
    code, adm = signed(kp, a.validator, "/v1/miners/admission", {}, a.validator_hotkey)
    log(f"admission requested: {code} {adm.get('job') or adm}")
    done, idle_until = 0, time.monotonic() + a.idle_s
    target = a.jobs + 1  # the admission test, then the client jobs
    while done < target and time.monotonic() < idle_until:
        code, work = signed(kp, a.validator, "/v1/jobs/work", {}, a.validator_hotkey)
        if code != 200 or not work.get("job"):
            time.sleep(2)
            continue
        job, ws = work["job"], work["workspace"]
        if a.reject and not job.startswith("admission-"):
            code, out = signed(
                kp,
                a.validator,
                "/v1/jobs/reject",
                {"job": job, "category": "not-allowed", "message": a.reject},
                a.validator_hotkey,
            )
            log(f"rejected {job}: {code} {out}")
            done += 1
            continue
        signed(kp, a.validator, "/v1/jobs/accept", {"job": job}, a.validator_hotkey)
        log(f"accepted {job} in {ws}")
        body: dict[str, Any] = {"job": job, "workspace": ws, "digest": ""}
        try:
            if not (a.fail and not job.startswith("admission-")):
                nq = work.get("no_questions") is True
                body["digest"] = build(mcp, ws, a.project, log, no_questions=nq)
        except HostRefusedError as e:
            log(f"{job}: the platform refused a run's result ({str(e)[:120]}): reported")
            body["host_refused"] = {"code": "host_refused", "why": str(e)}
        code, out = signed(kp, a.validator, "/v1/jobs/submit", body, a.validator_hotkey)
        log(f"submitted {job}: {code} {out}")
        done += 1
        idle_until = time.monotonic() + a.idle_s
    return 0


if __name__ == "__main__":
    sys.exit(main())
