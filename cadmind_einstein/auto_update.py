"""The miner's own auto-update: new releases are picked up between jobs, never during one.

A miner runs from a git checkout of this repository (``pip install -e .``). Every
``--auto-update-interval`` seconds (default 30 minutes) it fetches the remote's tags; when a release
tag ``vMAJOR.MINOR.PATCH`` newer than the running code exists, the update is *pending*. The miner
applies it only at its idle point (:meth:`Updater.between_jobs`): after a job has ended (submitted,
rejected or cancelled) and before it accepts another one. A job already accepted is always built to
its end first, and no new job is accepted while an update is pending.

Applying it: the checkout must have no local modifications (else one log line, nothing changes);
the tag is checked out, reinstalled into the running interpreter's environment the way it was
installed (an editable install of the checkout, with the extras whose requirements are present),
then a fresh interpreter must import the new code and report the tag's version. Then the process
re-executes itself with the same command line (``os.execv``: the same PID, so systemd, pm2 or a
shell see no restart). Any failure returns the checkout to the commit it was on, reinstalls that,
logs one line and keeps the miner running on the old code; that tag is not tried again.

Modes: ``release`` (the default, above); ``follow-checkout`` (no fetching and no install: when the
checkout's HEAD moves, the miner re-executes onto it between jobs, once the new code imports);
``off``. ``--no-auto-update`` or ``CADMIND_MINER_AUTO_UPDATE=0`` turns it off; that is never
penalised. A miner not running from a git checkout (a wheel, the Docker image) has it off.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

from . import __version__

ENV = "CADMIND_MINER_AUTO_UPDATE"
MODES = ("release", "follow-checkout", "off")
RELEASE_INTERVAL_S = 1800.0
CHECKOUT_INTERVAL_S = 60.0
GIT_TIMEOUT_S = 120.0
INSTALL_TIMEOUT_S = 900.0
VERIFY_TIMEOUT_S = 120.0
DIST = "cadmind-einstein"
VERIFY_MODULES = ("cadmind_einstein", "cadmind_einstein.agent_miner")
_RELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_OFF = {"0", "false", "no", "off"}

Run = Callable[[Sequence[str], float], tuple[int, str]]


def release_of(tag: str) -> tuple[int, int, int] | None:
    """A release tag ``vMAJOR.MINOR.PATCH`` as a comparable tuple; None for anything else
    (pre-releases, branch names, other tags)."""
    m = _RELEASE.match(tag.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def version_of(version: str) -> tuple[int, int, int] | None:
    """The running ``MAJOR.MINOR.PATCH`` as a tuple (None when it is not one)."""
    return release_of("v" + version.strip().removeprefix("v"))


def newest_release(tags: Iterable[str], current: str) -> str | None:
    """The newest release tag strictly newer than ``current``; None when there is none."""
    have = version_of(current)
    best: tuple[tuple[int, int, int], str] | None = None
    for tag in tags:
        v = release_of(tag)
        if v is None or (have is not None and v <= have):
            continue
        if best is None or v > best[0]:
            best = (v, tag.strip())
    return best[1] if best else None


def opted_out(flag: bool, environ: Mapping[str, str] | None = None) -> bool:
    """``--no-auto-update``, or ``CADMIND_MINER_AUTO_UPDATE`` set to 0/false/no/off."""
    env = os.environ if environ is None else environ
    return flag or env.get(ENV, "").strip().lower() in _OFF


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--auto-update",
        choices=MODES,
        default="release",
        help="release (default): between jobs, update to a newer release tag of the remote and "
        "restart; follow-checkout: restart onto the checkout's new HEAD between jobs (no fetch, no "
        "install); off: never",
    )
    ap.add_argument(
        "--no-auto-update",
        action="store_true",
        help=f"never update or restart by itself (same as --auto-update off or {ENV}=0)",
    )
    ap.add_argument(
        "--auto-update-interval",
        type=float,
        default=None,
        help=f"seconds between checks (default {RELEASE_INTERVAL_S:.0f} for release, "
        f"{CHECKOUT_INTERVAL_S:.0f} for follow-checkout)",
    )
    ap.add_argument(
        "--auto-update-remote", default="origin", help="the git remote read for release tags"
    )


def _run(argv: Sequence[str], timeout: float, cwd: Path | None = None) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        p = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            list(argv),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"
    return p.returncode, (p.stdout + p.stderr).strip()


def checkout_of(module_file: str | None = None) -> Path | None:
    """The git checkout the running ``cadmind_einstein`` is imported from; None when it is not
    one (a wheel in site-packages, the Docker image)."""
    here = Path(module_file or __file__).resolve().parent.parent
    return here if (here / ".git").exists() else None


def editable_from(repo: Path) -> bool | None:
    """True when the installed distribution is an editable install of ``repo``; False when it is
    installed from somewhere else; None when it is not installed (imported via PYTHONPATH)."""
    try:
        dist = metadata.distribution(DIST)
    except metadata.PackageNotFoundError:
        return None
    raw = dist.read_text("direct_url.json")
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except ValueError:
        return False
    url = str(info.get("url") or "")
    editable = bool((info.get("dir_info") or {}).get("editable"))
    path = url.removeprefix("file://")
    try:
        same = Path(path).resolve() == repo.resolve()
    except OSError:
        same = False
    return editable and same


def installed_extras() -> list[str]:
    """The package's extras whose requirements are all installed: reinstalled with it, so an
    update keeps (and refreshes) what the operator chose (``.[review]``)."""
    try:
        dist = metadata.distribution(DIST)
    except metadata.PackageNotFoundError:
        return []
    extras = dist.metadata.get_all("Provides-Extra") or []
    out = []
    for extra in extras:
        names = []
        for req in dist.requires or []:
            if f"extra == '{extra}'" in req or f'extra == "{extra}"' in req:
                names.append(re.split(r"[\s;<>=!~\[(]", req, maxsplit=1)[0])
        if names and all(_installed(n) for n in names):
            out.append(extra)
    return out


def _installed(name: str) -> bool:
    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return False
    return True


def exec_self() -> None:
    """Replace this process with a fresh interpreter on the same command line (same PID)."""
    for h in logging.getLogger().handlers:
        with contextlib.suppress(Exception):
            h.flush()
    for s in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            s.flush()
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    os.execv(sys.executable, [sys.executable, *argv[1:]])  # noqa: S606 -- our own command line


class Updater:
    """Checks for an update (any time, :meth:`poll`) and applies it only at the miner's idle point
    (:meth:`between_jobs`). ``run``, ``restart`` and ``clock`` are injectable for tests."""

    def __init__(
        self,
        mode: str,
        repo: Path,
        *,
        log: Callable[[str], None],
        interval_s: float | None = None,
        remote: str = "origin",
        current: str = __version__,
        python: str = sys.executable,
        install: bool | None = None,
        modules: Sequence[str] = VERIFY_MODULES,
        run: Run | None = None,
        restart: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode not in ("release", "follow-checkout"):
            raise ValueError(f"not an update mode: {mode}")
        self.mode, self.repo, self.log, self.remote = mode, repo, log, remote
        self.interval_s = interval_s or (
            RELEASE_INTERVAL_S if mode == "release" else CHECKOUT_INTERVAL_S
        )
        self.current, self.python, self.modules = current, python, tuple(modules)
        self.install = editable_from(repo) is True if install is None else install
        self._run = run or (lambda argv, t: _run(argv, t, cwd=repo))
        self.restart = restart or (lambda: exec_self())  # noqa: PLW0108 -- read at call time
        self.clock = clock
        self.pending: str | None = None
        self.skipped: set[str] = set()  # targets that failed or were refused: never tried again
        self._last = float("-inf")
        self._lock = threading.Lock()
        self.head = self._head() if mode == "follow-checkout" else ""

    # -- reading --------------------------------------------------------------------------
    def _git(self, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
        return self._run(["git", "-C", str(self.repo), *args], timeout)

    def _head(self) -> str:
        code, out = self._git("rev-parse", "HEAD")
        return out.strip() if code == 0 else ""

    def _dirty(self) -> bool:
        code, out = self._git("status", "--porcelain", "--untracked-files=no")
        return code != 0 or bool(out.strip())

    def due(self) -> bool:
        return self.clock() - self._last >= self.interval_s

    def check(self) -> str | None:
        """Look for an update now; it becomes :attr:`pending` (never applied here)."""
        self._last = self.clock()
        if self.mode == "follow-checkout":
            head = self._head()
            found = head if head and head != self.head else None
        else:
            code, out = self._git(
                "fetch", "--quiet", "--tags", "--no-recurse-submodules", self.remote
            )
            if code != 0:
                self.log(f"auto-update: fetch from {self.remote} failed: {_last_line(out)}")
                return self.pending
            code, out = self._git("tag", "--list", "v*")
            found = newest_release(out.split(), self.current) if code == 0 else None
        if found and found not in self.skipped and found != self.pending:
            self.pending = found
            self.log(f"auto-update: {self._name(found)} found; applied between jobs")
        return self.pending

    def poll(self) -> None:
        """A check when one is due; safe from any thread, never applies anything."""
        if not self.due() or not self._lock.acquire(blocking=False):
            return
        try:
            self.check()
        except Exception as e:  # noqa: BLE001 -- a failed check never touches the miner
            self.log(f"auto-update: check failed: {type(e).__name__}: {e}")
        finally:
            self._lock.release()

    def watch(self, busy: Callable[[], bool], stop: threading.Event) -> threading.Thread:
        """A background thread that checks while the miner is busy, so an update found during a
        job is applied as soon as the job ends."""

        def loop() -> None:
            while not stop.wait(min(60.0, self.interval_s)):
                if busy():
                    self.poll()

        t = threading.Thread(target=loop, name="auto-update", daemon=True)
        t.start()
        return t

    # -- applying -------------------------------------------------------------------------
    def between_jobs(self, *, job_waiting: bool = False) -> bool:
        """The miner's idle point: no job is accepted and none is being built. Checks when due
        (not while an unaccepted job waits), then applies a pending update and restarts. Returns
        True when a restart was made (``restart`` returned: tests); False otherwise."""
        with self._lock:
            try:
                if not job_waiting and self.due():
                    self.check()
                if self.mode == "follow-checkout" and self.pending:
                    now = self._head()  # moved again since the check: restart onto the newest
                    self.pending = None if now in ("", self.head, *self.skipped) else now
                if not self.pending:
                    return False
                target = self.pending
                ok = self._apply(target)
            except Exception as e:  # noqa: BLE001 -- never stops the miner
                target = self.pending or "?"
                self.log(f"auto-update: {self._name(target)} failed: {type(e).__name__}: {e}")
                ok = False
            if not ok:
                self.skipped.add(target)
                self.pending = None
                return False
        self.log(f"auto-update: restarting onto {self._name(target)}")
        try:
            self.restart()
        except Exception as e:  # noqa: BLE001 -- the old code keeps running; a restart starts the new
            self.skipped.add(target)  # not again each idle poll: the next manual restart runs it
            self.pending = None
            self.log(f"auto-update: the restart failed ({type(e).__name__}: {e}); still running")
            return False
        return True

    def _name(self, target: str) -> str:
        return target if self.mode == "release" else f"checkout HEAD {target[:12]}"

    def _verify(self, want: str | None) -> str:
        """A fresh interpreter imports the new code; '' when it does (and has ``want``)."""
        code_ = "import importlib, sys\n" + "".join(
            f"importlib.import_module({m!r})\n" for m in self.modules
        )
        code_ += "import cadmind_einstein\nprint(cadmind_einstein.__version__)\n"
        code, out = self._run([self.python, "-c", code_], VERIFY_TIMEOUT_S)
        if code != 0:
            return f"the new code does not import: {_last_line(out)}"
        got = out.strip().splitlines()[-1] if out.strip() else ""
        if want and got != want:
            return f"the new code says version {got!r}, not {want!r}"
        return ""

    def _apply(self, target: str) -> bool:
        if self.mode == "follow-checkout":
            why = self._verify(None)
            if why:
                self.log(f"auto-update: not restarting onto {self._name(target)}: {why}")
                return False
            return True
        if self._dirty():
            self.log(
                "auto-update: local changes in the checkout: not updating "
                f"{self.current} -> {target}"
            )
            return False
        prev = self._head()
        code, branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
        back = branch.strip() if code == 0 and branch.strip() else prev
        self.log(f"auto-update: updating {self.current} -> {target}")
        why = ""
        code, out = self._git(
            "-c", "advice.detachedHead=false", "checkout", "--quiet", f"refs/tags/{target}"
        )
        if code != 0:
            why = f"checkout failed: {_last_line(out)}"
        if not why and self.install:
            why = self._pip()
        if not why:
            why = self._verify(target.removeprefix("v"))
        if not why:
            return True
        self._git("-c", "advice.detachedHead=false", "checkout", "--quiet", back)
        undo = self._pip() if self.install else ""
        self.log(
            f"auto-update: {target} failed ({why}); back on {self.current} at {back[:40]}"
            + (f" (reinstall failed too: {undo})" if undo else "")
            + "; still running"
        )
        return False

    def _pip(self) -> str:
        extras = installed_extras()
        spec = str(self.repo) + (f"[{','.join(extras)}]" if extras else "")
        code, out = self._run(
            [
                self.python,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                "-e",
                spec,
            ],
            INSTALL_TIMEOUT_S,
        )
        return "" if code == 0 else f"reinstall failed: {_last_line(out)}"


def _last_line(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "(no output)")[:300]


def from_args(
    a: Any,
    *,
    log: Callable[[str], None],
    environ: Mapping[str, str] | None = None,
    module_file: str | None = None,
    **kw: Any,
) -> Updater | None:
    """The miner's updater from its flags (:func:`add_args`), or None when it is off; one log line
    either way."""
    mode = getattr(a, "auto_update", "release")
    if opted_out(getattr(a, "no_auto_update", False), environ) or mode == "off":
        log("auto-update: off")
        return None
    repo = checkout_of(module_file)
    if repo is None:
        log("auto-update: off (not running from a git checkout; update by hand)")
        return None
    up = Updater(
        mode,
        repo,
        log=log,
        interval_s=getattr(a, "auto_update_interval", None),
        remote=getattr(a, "auto_update_remote", "origin"),
        **kw,
    )
    log(
        f"auto-update: {mode} every {up.interval_s / 60:.0f} min, between jobs only "
        f"(--no-auto-update or {ENV}=0 turns it off)"
    )
    return up
