"""The validator's calibration archive: what the judge said and how each job went, kept past the
store's 7-day window (the calibration of the judge and the miners).

The store forgets a finished job (``DONE_KEEP_MS``) and its votes (:meth:`.votes.Reviewer.
forget_before`) once they are out of the scoring window; their scores live on in the hourly reports,
but the judge's words, each model call's raw reply, tokens and cost, and each attempt's timeline,
rejections, gate errors and retries did not. This archive keeps them.

**Layout.** One append-only JSON-lines file per job, ``<state>/archive/jobs/<job>.jsonl``: every
record of the job in the order it was written. A record is ``{"kind", "at_ms", "einstein", ...}``:

- ``vote``: one vote as recorded (:meth:`.votes.Reviewer._review`) -- the signed vote with its
  parsed samples, the judge (rubric version and digest), the model asked, the bundle it read
  (its sha256, image set and manifest: the renderer is named there), and ``calls``: every model
  call made for it, in order, with the raw reply text, what it parsed to (or why it was
  unavailable), the model that answered, tokens, dollars and latency;
- ``vote-attempt``: model calls that made no vote (too few valid samples, a provider error): they
  cost money and say how the judge fails, so they are kept too;
- ``job``: the validator's job row with every attempt (``event`` is ``ended`` when the platform took
  its result, ``dropped`` when the store let it go), with the versions it was judged under.

**Never on the public face.** Nothing here is served or published: the words are the client's
project. The archive is the operator's, read with the files themselves.

**A client's deletion is honoured.** When the platform says a job was deleted (the ``forget``
signal, :meth:`.desk.Desk.forget`), its file is removed: no calibration copy outlives the client's
own delete. A platform wipe sends no such signal, so the archive survives it.

**Failure never stops the validator.** A write that fails is logged and dropped: the archive is
analytics, never a reason to fail a vote or a job.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import __version__

FORMAT = "cadmind-einstein-archive/1"
_JOB = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class Archive:
    """Append-only per-job JSON-lines files under ``root`` (``None``: kept in memory, for tests)."""

    def __init__(
        self,
        root: Path | str | None,
        *,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else None
        self.clock = clock
        self.log = log or (lambda _line: None)
        self._lock = threading.Lock()
        self._mem: dict[str, list[dict[str, Any]]] = {}
        self._forgotten: set[str] = set()
        """jobs their client deleted: never archived again (a vote made after the delete)"""
        if self.root is not None:
            (self.root / "jobs").mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                text = (self.root / "forgotten").read_text(encoding="utf-8")
                self._forgotten = {line.strip() for line in text.splitlines() if line.strip()}

    def _path(self, job: str) -> Path | None:
        if self.root is None or not _JOB.match(job or "") or job.startswith("."):
            return None
        return self.root / "jobs" / f"{job}.jsonl"

    def append(self, job: str, kind: str, record: Mapping[str, Any]) -> bool:
        """Append one record to ``job``'s file. Answers whether it was written; never raises."""
        row = {
            "format": FORMAT,
            "kind": kind,
            "job": job,
            "at_ms": int(self.clock() * 1000),
            "einstein": __version__,
            **dict(record),
        }
        try:
            line = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str) + "\n"
        except (TypeError, ValueError) as e:
            self.log(f"archive: {job} {kind} not written: {type(e).__name__}: {e}")
            return False
        with self._lock:
            if job in self._forgotten:
                return False
            if self.root is None:
                if not _JOB.match(job or ""):
                    return False
                self._mem.setdefault(job, []).append(json.loads(line))
                return True
            path = self._path(job)
            if path is None:
                self.log(f"archive: {job!r} is not a job id: {kind} not written")
                return False
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as e:
                self.log(f"archive: {job} {kind} not written: {type(e).__name__}: {e}")
                return False
        return True

    def records(self, job: str) -> list[dict[str, Any]]:
        """Every record of ``job``, in the order written (a torn last line is skipped)."""
        with self._lock:
            if self.root is None:
                return [dict(r) for r in self._mem.get(job, [])]
            path = self._path(job)
            if path is None or not path.is_file():
                return []
            out = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
            return out

    def jobs(self) -> list[str]:
        with self._lock:
            if self.root is None:
                return sorted(self._mem)
            return sorted(p.stem for p in (self.root / "jobs").glob("*.jsonl"))

    def forget(self, job: str) -> bool:
        """Remove ``job``'s file (its client deleted it), and archive nothing of it again. Answers
        whether there was one."""
        with self._lock:
            if not _JOB.match(job or ""):
                return False
            if job not in self._forgotten:
                self._forgotten.add(job)
                if self.root is not None:
                    try:
                        fd = os.open(
                            self.root / "forgotten", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                        )
                        try:
                            os.write(fd, f"{job}\n".encode())
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                    except OSError as e:
                        self.log(f"archive: {job}'s deletion not noted: {type(e).__name__}: {e}")
            if self.root is None:
                return self._mem.pop(job, None) is not None
            path = self._path(job)
            if path is None:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            except OSError as e:
                self.log(f"archive: {job} not forgotten: {type(e).__name__}: {e}")
                return False
            return True
