"""``cadmind-einstein-operator``: the operator's side of manual miner onboarding (DESIGN.md section
2), over the validator's Bearer routes.

    cadmind-einstein-operator --validator http://127.0.0.1:8793 --token-file T pending [--state all]
    cadmind-einstein-operator ... approve HOTKEY
    cadmind-einstein-operator ... refuse HOTKEY --reason "WORDS THE MINER READS"
    cadmind-einstein-operator ... revoke HOTKEY --reason "WORDS THE MINER READS"
    cadmind-einstein-operator ... launch-equal on|off|status

``pending`` lists the key requests (hotkey, coldkey, first and last ask, client address, the
miner's note); ``approve`` lets the hotkey's next ``agent_key`` call mint its key; ``refuse``
answers its asks with the reason; ``revoke`` deletes its key, drops its admission and sends the job
it holds to another miner unscored; ``launch-equal`` turns the launch rule (equal weight per
admitted hotkey) on or off at runtime, saved across restarts (unless ``--launch-equal`` sets it). An
approved miner still passes the entry test once before any job or weight. The token file (the
validator's ``--token-file``, mode 0600) is read, never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__


def read_token(path: Path) -> str:
    """The Bearer token from ``path``, which must be readable by its owner only."""
    mode = path.stat().st_mode & 0o077
    if mode:
        raise SystemExit(f"{path} is readable by others (mode {oct(mode)}): chmod 600 it")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"{path} is empty")
    return token


def call(
    base: str, token: str, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """One request to the validator: (status, decoded JSON answer)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 -- the operator's own validator URL
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"cadmind-einstein-operator/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": f"HTTP {e.code}"}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cadmind-einstein-operator",
        description="manual miner onboarding: list, approve, refuse and revoke agent keys; the"
        " launch rule",
    )
    ap.add_argument("--validator", default="http://127.0.0.1:8793", help="the validator's URL")
    ap.add_argument("--token-file", type=Path, required=True, help="the validator's Bearer token")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pending", help="list key requests")
    p.add_argument(
        "--state",
        default="pending",
        choices=("pending", "approved", "refused", "revoked", "all"),
    )
    p = sub.add_parser("approve", help="approve a hotkey's key request")
    p.add_argument("hotkey")
    for name in ("refuse", "revoke"):
        p = sub.add_parser(name, help=f"{name} a hotkey (the miner reads the reason)")
        p.add_argument("hotkey")
        p.add_argument("--reason", required=True)
    p = sub.add_parser("launch-equal", help="the launch rule: on, off, or its status")
    p.add_argument("switch", choices=("on", "off", "status"))
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    token = read_token(a.token_file)
    if a.command == "pending":
        q = urllib.parse.urlencode({"state": a.state})
        code, out = call(a.validator, token, "GET", f"/v1/operator/key_requests?{q}")
    elif a.command == "launch-equal":
        if a.switch == "status":
            code, out = call(a.validator, token, "GET", "/v1/operator/launch_equal")
        else:
            code, out = call(
                a.validator, token, "POST", "/v1/operator/launch_equal", {"on": a.switch == "on"}
            )
    elif a.command == "approve":
        code, out = call(a.validator, token, "POST", "/v1/operator/approve", {"hotkey": a.hotkey})
    else:
        code, out = call(
            a.validator,
            token,
            "POST",
            f"/v1/operator/{a.command}",
            {"hotkey": a.hotkey, "reason": a.reason},
        )
    stream = sys.stdout if code == 200 else sys.stderr
    print(json.dumps(out, indent=1), file=stream)
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
