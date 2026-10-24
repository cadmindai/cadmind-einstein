"""A delivered project loaded into a fresh Watt workspace, for the tools judge
(``cadmind-judge/1.2b-tools``, :mod:`.judge_tools`): CADmind's own pipeline and development runs,
never a validator.

    ws, record = load(WorkspaceClient(platform, key), project_dir, "judge-<name>")
    build(client, ws)            # once, unchanged: the instruments read the built model
    ... judge(..., version="1.2b-tools") with ToolAccess(platform, key_file, ws, variants) ...
    delete(client, ws)

Unlike the instruments' :class:`.judge_tools.ReadOnlyWatt`, this client WRITES (it makes, fills,
builds and deletes a workspace), so it is never handed to a model or to the judge.

**A re-import is lossy** (the design note's blocker 2): the agent API cannot write ``brief/``, so a
need that links a client photo under ``brief/`` and a decision answered by the client in
``brief/answer-*`` would make the copy refuse to build. In the judge's copy the photo link becomes
the brief's own sentence about the photo, and an answered decision reads as decided (both
provenance only, recorded in ``patched``). A judge in production should read the delivered
workspace itself, frozen and read-only, rather than a re-import.

The agent key is the caller's; it is never printed or logged here.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TEXT_DIRS = ("spec", "src", "tests")
TEXT_TOP = ("watt.toml",)
MAX_TEXT = 400_000
BUSY_TRIES = 90
BRIEF_ATTACHMENT = re.compile(r'^(\s*attachment\s*=\s*"brief/[^"]*".*)$', re.M)
_VARIANT = re.compile(r"\.variant\(\s*[\"']([^\"']+)[\"']")


class WorkspaceClient:
    """The agent API with an agent key: make, write, upload, build, delete."""

    def __init__(self, platform: str, key: str) -> None:
        self.platform, self._key = platform.rstrip("/"), key

    def __repr__(self) -> str:  # never the key
        return f"WorkspaceClient({self.platform!r})"

    def call(
        self, method: str, path: str, body: Any = None, *, timeout: float = 300, tries: int = 6
    ) -> tuple[int, Any]:
        busy, i = 0, -1
        while i < tries - 1:
            i += 1
            req = urllib.request.Request(  # noqa: S310 -- the operator's platform URL
                self.platform + path,
                method=method,
                data=None if body is None else json.dumps(body).encode(),
                headers={
                    "Authorization": "Bearer " + self._key,
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                    return int(r.status), json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                data = e.read()
                if e.code in (429, 502, 503, 504) and i < tries - 1:
                    time.sleep(5 * (i + 1))
                    continue
                if e.code == 409 and b"run_in_progress" in data and busy < BUSY_TRIES:
                    busy += 1  # one command per key: another user of the key holds it
                    i -= 1
                    time.sleep(10)
                    continue
                try:
                    return e.code, json.loads(data or b"{}")
                except ValueError:
                    return e.code, {"error": data[:300].decode(errors="replace")}
            except (OSError, ValueError) as e:
                if i < tries - 1:
                    time.sleep(5 * (i + 1))
                    continue
                return 0, {"error": str(e)[:300]}
        return 0, {"error": "no answer"}

    def run(self, ws: str, command: str, budget_s: float = 1500) -> dict[str, Any]:
        t0 = time.time()
        while True:
            st, out = self.call(
                "POST", f"/api/agent/workspaces/{ws}/run", {"command": command, "wait_s": 90}
            )
            busy = st == 409 and isinstance(out, dict) and out.get("code") == "run_in_progress"
            if not busy or time.time() - t0 > budget_s:
                break
            time.sleep(10)
        while (
            isinstance(out, dict)
            and out.get("state") in ("running", "queued", "waiting")
            and time.time() - t0 < budget_s
        ):
            st, out = self.call("GET", f"/api/agent/runs/{out['run']}?wait_s=60")
        return out if isinstance(out, dict) else {"error": str(out)[:300]}

    def compute_used_s(self) -> float:
        st, out = self.call("GET", "/api/agent/workspaces")
        if st != 200 or not isinstance(out, dict):
            return -1.0
        return float((out.get("limits") or {}).get("compute_s_used_24h") or 0)


def declared_variants(project: Path) -> list[str]:
    """The assembly's declared variants (``asm.variant("name")``): structure, not a claim."""
    names: list[str] = []
    for p in (project / "src").rglob("*.py"):
        names += _VARIANT.findall(p.read_text(encoding="utf-8", errors="replace"))
    return sorted(set(names))


def _patch(rel: str, text: str, brief: str, project: Path, rec: dict[str, Any]) -> str:
    if rel.startswith("spec/") and BRIEF_ATTACHMENT.search(text):
        sentences = re.split(r"(?<=[.!?])\s+", brief.strip())
        said = next(
            (s for s in sentences if re.search(r"photo|attach|picture", s, re.I)), sentences[0]
        )
        said = said.replace('"', "'") if said.replace('"', "'") in brief else said
        text = BRIEF_ATTACHMENT.sub(
            lambda m: (
                f"# (judge copy: brief photo not loadable) {m.group(1).strip()}\n"
                f"quote = {json.dumps(said)}"
            ),
            text,
        )
        rec.setdefault("patched", []).append(rel)
    answered = 'status = "answered"' in text
    if rel == "spec/decisions.toml" and any((project / "brief").glob("answer-*")) and answered:
        blocks = re.split(r"(?m)^(?=\[)", text)
        for k, blk in enumerate(blocks):
            if 'status = "answered"' in blk:
                decided = blk.replace('by = "client"', 'by = "agent"').replace(
                    'status = "answered"', 'status = "decided"'
                )
                blocks[k] = re.sub(
                    r"^(\s*answer\s*=.*)$", r"# (judge copy) \1", decided, flags=re.M
                )
        text = "".join(blocks)
        rec.setdefault("patched", []).append(rel)
    return text


def load(api: WorkspaceClient, project: Path, name: str) -> tuple[str, dict[str, Any]]:
    """Make a workspace with the project's brief and load the project's text files and assets.
    Answers ``(workspace, record)``; the record names what was loaded, refused and patched."""
    rec: dict[str, Any] = {"files": 0, "assets": 0, "refused": []}
    brief = (project / "brief" / "brief.md").read_text(encoding="utf-8")
    st, made = api.call(
        "POST", "/api/agent/workspaces", {"brief": brief, "effort": "max", "name": name[:60]}
    )
    if st not in (200, 201) or not isinstance(made, dict) or not made.get("workspace"):
        raise RuntimeError(f"workspace not made: {st} {str(made)[:200]}")
    ws = str(made["workspace"])
    files = [
        p
        for p in project.rglob("*")
        if p.is_file()
        and (
            p.relative_to(project).parts[0] in TEXT_DIRS or str(p.relative_to(project)) in TEXT_TOP
        )
        and "__pycache__" not in p.parts
    ]
    for p in sorted(files):
        rel = str(p.relative_to(project))
        text = _patch(rel, p.read_text(encoding="utf-8", errors="replace"), brief, project, rec)
        if len(text) > MAX_TEXT:
            rec["refused"].append(f"{rel}: too long")
            continue
        st, out = api.call(
            "POST", f"/api/agent/workspaces/{ws}/file", {"path": rel, "content": text}
        )
        if st == 200:
            rec["files"] += 1
        else:
            rec["refused"].append(f"{rel}: {st} {str(out)[:120]}")
    assets = sorted((project / "assets").glob("*")) if (project / "assets").is_dir() else []
    for p in assets:
        if not p.is_file():
            continue
        st, out = api.call(
            "POST",
            f"/api/agent/workspaces/{ws}/upload",
            {
                "url": f"{api.platform}/judge/{p.name}",
                "save_as": p.name,
                "data_base64": base64.b64encode(p.read_bytes()).decode(),
            },
            timeout=180,
        )
        if st == 200:
            rec["assets"] += 1
        else:
            rec["refused"].append(f"assets/{p.name}: {st} {str(out)[:120]}")
    return ws, rec


def build(api: WorkspaceClient, ws: str) -> dict[str, Any]:
    """Build the loaded project once, unchanged: ``{ok, s, compute_ms, errors}``."""
    t0 = time.time()
    out = api.run(ws, "build")
    diags = (out.get("data") or {}).get("diagnostics") or []
    return {
        "ok": out.get("ok"),
        "s": round(time.time() - t0, 1),
        "compute_ms": out.get("compute_ms"),
        "errors": [
            f"{d.get('code')}: {str(d.get('found'))[:160]}"
            for d in diags
            if isinstance(d, dict) and d.get("severity") == "error"
        ][:8],
    }


def delete(api: WorkspaceClient, ws: str) -> int:
    st, _ = api.call("DELETE", f"/api/agent/workspaces/{ws}")
    return st
