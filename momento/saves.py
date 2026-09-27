"""The daemon's save queue: clips are exported one at a time, in order, off the main loop.

A save is registered as a ``SaveJob`` the moment it is asked for (so ``status``
can list it and a client can return at once), and handed to the queue once the
daemon has flushed the open segment and pinned the footage it needs
(``submit``). A single worker thread runs the jobs in order: two ffmpeg exports
never run at once. The thread exists only while there is work, so an idle daemon
has none. Every job ends in one state: ``saved``, ``failed`` or ``cancelled``,
and ``release`` (which unpins its footage) runs exactly once for each submitted
job, whether it ran or not.

Shutdown (``shutdown``) stops taking new jobs, cancels the waiting ones, gives
the running one a grace period to finish, then cancels it (the exporter stops
ffmpeg and removes its temp files) and joins the thread.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

MAX_WAITING = 10          # clip saves waiting behind the running one; more are refused
HISTORY = 10              # finished jobs kept for status.saves
SHUTDOWN_GRACE_S = 10.0   # a running save gets this long to finish when Momento closes
CANCEL_WAIT_S = 5.0       # then, after cancelling it, this long for ffmpeg and the thread to end

CANCELLED = "save cancelled: Momento was closing"
BUSY = "Still saving your earlier clips, try again in a moment"

# Values of SaveJob.state (protocol.SAVE_STATES).
ACTIVE = ("queued", "saving")
DONE = ("saved", "failed", "cancelled")


class Busy(Exception):
    """Too many saves waiting (or Momento is closing): this one is refused."""


class SaveJob:
    def __init__(self, job_id: int, seconds: int, kind: str = "clip"):
        self.id = job_id
        self.kind = kind            # "clip" (save) | "hour" (keep_history's span)
        self.requested = seconds
        self.until: float | None = None       # wall clock the clip ends at (the press)
        self.state = "queued"
        self.asked_at = time.time()
        self.started: float | None = None     # monotonic, for the log's duration
        self.finished_at: float | None = None  # wall clock (status)
        self.result: dict | None = None
        self.cancel = threading.Event()
        self.reply: Callable[[dict], None] | None = None   # a blocking save's reply
        self.run: Callable[[SaveJob], dict] | None = None
        self.release: Callable[[], None] | None = None
        self.info: dict = {}        # free-form, for the daemon (e.g. the notification's wording)

    def describe(self) -> dict:
        """The job as status.saves lists it."""
        r = self.result or {}
        return {
            "job": self.id,
            "kind": self.kind,
            "state": self.state,
            "requested": self.requested,
            "asked_at": round(self.asked_at, 3),
            "finished_at": round(self.finished_at, 3) if self.finished_at else None,
            "path": r.get("path"),
            "seconds": r.get("seconds"),
            "error": r.get("error"),
            "code": r.get("code"),
        }


class SaveQueue:
    def __init__(self, on_done: Callable[[SaveJob], None] | None = None, max_waiting: int = MAX_WAITING):
        self.on_done = on_done
        self.max_waiting = max_waiting
        self._cond = threading.Condition()
        self._jobs: list[SaveJob] = []     # active ones + the last HISTORY finished
        self._queue: list[SaveJob] = []    # submitted, waiting to run, in order
        self._current: SaveJob | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
        self._ids = itertools.count(1)

    # --- asking -----------------------------------------------------------------

    def new(self, seconds: int, kind: str = "clip") -> SaveJob:
        """Register a save; raises Busy when MAX_WAITING clip saves already wait (or closing).

        An hour save is never refused for a full queue: its footage is not asked again.
        """
        with self._cond:
            if self._closing:
                raise Busy(CANCELLED)
            # the first active job runs (or is about to); the rest wait
            active = [j for j in self._jobs if j.state in ACTIVE]
            if kind == "clip" and len(active) > self.max_waiting:
                raise Busy(BUSY)
            job = SaveJob(next(self._ids), seconds, kind)
            self._jobs.append(job)
            return job

    def ahead(self, job: SaveJob) -> int:
        """How many saves run or wait before ``job``."""
        with self._cond:
            return sum(1 for j in self._jobs if j.id < job.id and j.state in ACTIVE)

    def submit(self, job: SaveJob, run: Callable[[SaveJob], dict], release: Callable[[], None]) -> None:
        """Queue a registered job, its footage pinned. ``release`` unpins it, exactly once."""
        job.run, job.release = run, release
        with self._cond:
            closing = self._closing
            if not closing:
                self._queue.append(job)
                if self._thread is None:
                    self._thread = threading.Thread(target=self._work, name="save-queue", daemon=True)
                    self._thread.start()
        if closing:
            self._end(job, {"ok": False, "code": "cancelled", "error": CANCELLED})

    def fail(self, job: SaveJob, result: dict) -> None:
        """End a registered job that never needs to run (nothing recorded)."""
        self._end(job, result)

    # --- the worker ----------------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._cond:
                if not self._queue:
                    self._thread = None
                    self._cond.notify_all()
                    return
                job = self._current = self._queue.pop(0)
                job.state = "saving"
                job.started = time.monotonic()
            try:
                result = job.run(job) if not job.cancel.is_set() else {
                    "ok": False, "code": "cancelled", "error": CANCELLED}
            except Exception as e:  # noqa: BLE001 - one bad job must not stop the queue
                log.exception("save #%d failed", job.id)
                result = {"ok": False, "error": str(e) or e.__class__.__name__}
            self._end(job, result)

    def _end(self, job: SaveJob, result: dict) -> None:
        release, job.release = job.release, None
        if release is not None:
            try:
                release()
            except Exception:  # noqa: BLE001
                log.exception("save #%d: releasing its footage failed", job.id)
        with self._cond:
            job.result = result
            job.state = "saved" if result.get("ok") else (
                "cancelled" if result.get("code") == "cancelled" else "failed")
            job.finished_at = time.time()
            if self._current is job:
                self._current = None
            done = [j for j in self._jobs if j.state in DONE]
            for old in done[:-HISTORY]:
                self._jobs.remove(old)
            self._cond.notify_all()
        if self.on_done is not None:
            try:
                self.on_done(job)
            except Exception:  # noqa: BLE001
                log.exception("save #%d: reporting the result failed", job.id)

    # --- status and shutdown ----------------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self._cond:
            return [j.describe() for j in self._jobs]

    def busy(self) -> bool:
        with self._cond:
            return self._current is not None or bool(self._queue)

    def running(self) -> threading.Thread | None:
        with self._cond:
            return self._thread

    def shutdown(self, grace: float | None = None) -> None:
        """Stop taking saves; let the running one finish within ``grace``, else cancel it.

        Waiting saves are cancelled at once (their footage released). Returns once
        the worker thread has ended (or CANCEL_WAIT_S after cancelling, at most).
        """
        grace = SHUTDOWN_GRACE_S if grace is None else grace
        with self._cond:
            self._closing = True
            waiting, self._queue = self._queue, []
            current = self._current
        for job in waiting:
            job.cancel.set()
            self._end(job, {"ok": False, "code": "cancelled", "error": CANCELLED})
        if current is not None:
            log.info("waiting up to %.0f s for save #%d to finish", grace, current.id)
            deadline = time.monotonic() + grace
            with self._cond:
                while self._current is not None and time.monotonic() < deadline:
                    self._cond.wait(max(0.01, deadline - time.monotonic()))
                current = self._current
            if current is not None:
                current.cancel.set()
        with self._cond:
            thread = self._thread
        if thread is not None:
            thread.join(CANCEL_WAIT_S)
            if thread.is_alive():
                log.warning("the save thread did not end in %.0f s", CANCEL_WAIT_S)
