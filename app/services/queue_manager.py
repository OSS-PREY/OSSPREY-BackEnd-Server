"""In-process FIFO request queue with bounded concurrency.

The OSSPREY pipeline (Rust scraper -> pex-forecaster -> ReACT) is a long running,
CPU/IO heavy job. Running many of them at once exhausts the host, so this module
limits how many run simultaneously and queues the rest in First-In-First-Out
order.

Design notes
------------
* The manager keeps all of its state in memory and guards it with a single
  re-entrant lock. The Gunicorn config for this project uses a single worker
  process (see ``gunicorn.conf.py``), so an in-memory queue is consistent for
  every request. If the deployment ever scales to multiple worker processes the
  state would need to move to a shared store (e.g. Redis); this is documented in
  the README.
* Jobs execute on a ``ThreadPoolExecutor`` sized to ``max_concurrent`` so the web
  worker thread that accepts the request is freed immediately and remains
  available to serve status polls while the pipeline runs in the background.
* The manager is intentionally generic: it accepts a ``worker`` callable so it
  can be unit tested with a fast fake task instead of the real pipeline.
"""

import math
import time
import uuid
import logging
import threading
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Public, stable status values used by both the API and the frontend.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}


def _iso(epoch_seconds):
    """Convert an epoch timestamp to an ISO-8601 UTC string (or ``None``)."""
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


class _Job:
    """Internal bookkeeping record for a single queued request."""

    __slots__ = (
        "job_id", "args", "kwargs", "metadata", "status",
        "created_at", "started_at", "finished_at", "result", "error",
    )

    def __init__(self, job_id, args, kwargs, metadata):
        self.job_id = job_id
        self.args = args
        self.kwargs = kwargs
        self.metadata = metadata or {}
        self.status = STATUS_QUEUED
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.result = None
        self.error = None


class QueueManager:
    """Thread-safe FIFO queue that runs at most ``max_concurrent`` jobs at once."""

    def __init__(
        self,
        worker,
        max_concurrent=2,
        default_job_seconds=120,
        max_history=200,
        result_is_error=None,
        on_update=None,
    ):
        """
        Args:
            worker: callable invoked as ``worker(*args, **kwargs)`` for each job.
            max_concurrent: maximum number of jobs allowed to run simultaneously.
            default_job_seconds: seed value for the estimated job duration, used
                for wait-time estimates until real timings are observed.
            max_history: maximum number of finished jobs to retain for status
                look-ups before the oldest are pruned.
            result_is_error: optional predicate ``fn(result) -> bool``. When it
                returns ``True`` the job is marked ``failed`` even though the
                worker returned normally (used so a pipeline result like
                ``{"error": ...}`` is reported as a failure).
            on_update: optional callback ``fn(snapshot)`` invoked (outside the
                internal lock) every time a job changes state, so the state can
                be mirrored to a persistent store.
        """
        self._worker = worker
        self.max_concurrent = max(1, int(max_concurrent))
        self._default_job_seconds = max(1, int(default_job_seconds))
        self._avg_job_seconds = float(self._default_job_seconds)
        self._max_history = max_history
        self._result_is_error = result_is_error
        self._on_update = on_update

        self._lock = threading.RLock()
        self._jobs = OrderedDict()   # job_id -> _Job (insertion ordered)
        self._waiting = deque()      # job_ids awaiting a free slot (FIFO)
        self._running = set()        # job_ids currently executing
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrent,
            thread_name_prefix="osprey-queue",
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def submit(self, *args, metadata=None, **kwargs):
        """Enqueue a job and return its initial status snapshot."""
        with self._lock:
            job_id = uuid.uuid4().hex
            job = _Job(job_id, args, kwargs, metadata)
            self._jobs[job_id] = job
            self._waiting.append(job_id)
            logger.info("Queued job %s (queue length=%d)", job_id, len(self._waiting))
            self._dispatch_locked()
            self._prune_locked()
            snapshot = self._snapshot_locked(job)
        self._notify(job_id)
        return snapshot

    def get_status(self, job_id, include_result=False):
        """Return a status snapshot for ``job_id`` (or ``None`` if unknown)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            snapshot = self._snapshot_locked(job)
            if include_result and job.status in (STATUS_COMPLETED, STATUS_FAILED):
                snapshot["result"] = job.result
            return snapshot

    def cancel(self, job_id):
        """Cancel a job that has not started yet.

        Running jobs cannot be interrupted safely, so only ``queued`` jobs are
        cancellable. Returns the resulting snapshot, or ``None`` if unknown.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status == STATUS_QUEUED:
                try:
                    self._waiting.remove(job_id)
                except ValueError:
                    pass
                job.status = STATUS_CANCELLED
                job.finished_at = time.time()
                logger.info("Cancelled queued job %s", job_id)
            snapshot = self._snapshot_locked(job)
        self._notify(job_id)
        return snapshot

    def list_jobs(self, statuses=None):
        """Return snapshots of all known jobs, newest first.

        Args:
            statuses: optional iterable of status values to include; when
                ``None`` every job is returned.
        """
        with self._lock:
            snapshots = [
                self._snapshot_locked(job) for job in self._jobs.values()
                if statuses is None or job.status in statuses
            ]
        snapshots.sort(key=lambda s: s["created_at"] or "", reverse=True)
        return snapshots

    def stats(self):
        """Return aggregate queue statistics."""
        with self._lock:
            return {
                "running": len(self._running),
                "queued": len(self._waiting),
                "max_concurrent": self.max_concurrent,
                "avg_job_seconds": round(self._avg_job_seconds, 1),
                "total_jobs": len(self._jobs),
            }

    # ------------------------------------------------------------------ #
    # Internal helpers (all assume the caller holds ``self._lock``)
    # ------------------------------------------------------------------ #
    def _dispatch_locked(self):
        """Start queued jobs while free concurrency slots are available."""
        while len(self._running) < self.max_concurrent and self._waiting:
            job_id = self._waiting.popleft()
            job = self._jobs.get(job_id)
            if job is None or job.status != STATUS_QUEUED:
                continue  # cancelled or pruned while waiting
            job.status = STATUS_RUNNING
            job.started_at = time.time()
            self._running.add(job_id)
            logger.info("Starting job %s (%d running)", job_id, len(self._running))
            self._executor.submit(self._run, job_id)

    def _run(self, job_id):
        """Execute a single job on a worker thread."""
        job = self._jobs.get(job_id)
        if job is None:
            return

        result = None
        error = None
        try:
            result = self._worker(*job.args, **job.kwargs)
            if self._result_is_error is not None and self._result_is_error(result):
                error = self._extract_error_message(result)
        except Exception as exc:  # noqa: BLE001 - report any failure to the caller
            logger.exception("Queue job %s failed", job_id)
            error = str(exc)

        with self._lock:
            job.finished_at = time.time()
            job.result = result
            if error is not None:
                job.status = STATUS_FAILED
                job.error = error
            else:
                job.status = STATUS_COMPLETED
            self._running.discard(job_id)
            self._update_avg_locked(job)
            # A slot just freed up: start the next queued job (FIFO).
            self._dispatch_locked()
            # Jobs that were just dispatched also changed state; persist them too.
            running_ids = list(self._running)
        self._notify(job_id, *running_ids)

    def _notify(self, *job_ids):
        """Invoke the ``on_update`` listener with fresh job snapshots.

        Snapshots are taken under the lock, but the listener itself runs
        outside it so a slow listener (e.g. a database write) never blocks
        queue bookkeeping. Listener errors are logged, never raised.
        """
        if self._on_update is None:
            return
        with self._lock:
            snapshots = [
                self._snapshot_locked(self._jobs[job_id])
                for job_id in dict.fromkeys(job_ids)
                if job_id in self._jobs
            ]
        for snapshot in snapshots:
            try:
                self._on_update(snapshot)
            except Exception:  # noqa: BLE001 - persistence must not kill jobs
                logger.exception("on_update listener failed for job %s", snapshot.get("job_id"))

    @staticmethod
    def _extract_error_message(result):
        if isinstance(result, dict):
            return str(result.get("error") or "Processing failed.")
        return "Processing failed."

    def _update_avg_locked(self, job):
        """Update the exponential moving average of completed job durations."""
        if job.started_at and job.finished_at:
            duration = job.finished_at - job.started_at
            if duration > 0:
                alpha = 0.3  # weight of the most recent observation
                self._avg_job_seconds = (
                    alpha * duration + (1 - alpha) * self._avg_job_seconds
                )

    def _position_locked(self, job):
        """1-based position of a queued job; 0 for running/terminal jobs."""
        if job.status == STATUS_QUEUED:
            for index, waiting_id in enumerate(self._waiting):
                if waiting_id == job.job_id:
                    return index + 1
        return 0

    def _eta_locked(self, job):
        """Estimated seconds until the job finishes. Best-effort, not exact."""
        if job.status == STATUS_RUNNING:
            elapsed = time.time() - (job.started_at or time.time())
            return max(int(self._avg_job_seconds - elapsed), 0)
        if job.status == STATUS_QUEUED:
            position = self._position_locked(job)
            # All slots are full while a job waits, so it can only start once a
            # running job frees a slot. With ``max_concurrent`` lanes draining in
            # parallel, the job at ``position`` starts after roughly
            # ceil(position / max_concurrent) average-length jobs complete.
            waves = math.ceil(position / self.max_concurrent)
            return int(waves * self._avg_job_seconds)
        return 0

    def _snapshot_locked(self, job):
        """Build the public, JSON-serialisable view of a job."""
        return {
            "job_id": job.job_id,
            "status": job.status,
            "position": self._position_locked(job),
            "estimated_wait_seconds": self._eta_locked(job),
            "queue_length": len(self._waiting),
            "running": len(self._running),
            "max_concurrent": self.max_concurrent,
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
            "error": job.error,
            "metadata": job.metadata,
        }

    def _prune_locked(self):
        """Drop the oldest terminal jobs once history exceeds ``max_history``."""
        if len(self._jobs) <= self._max_history:
            return
        excess = len(self._jobs) - self._max_history
        removable = [
            job_id for job_id, job in self._jobs.items()
            if job.status in _TERMINAL_STATUSES
        ]
        for job_id in removable[:excess]:
            self._jobs.pop(job_id, None)
