import os
import sys
import time
import threading

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.queue_manager import QueueManager


def wait_for(predicate, timeout=5.0, interval=0.02):
    """Poll ``predicate`` until it returns truthy or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_max_two_concurrent_and_fifo_order():
    """At most two jobs run at once; the rest wait and start in FIFO order."""
    lock = threading.Lock()
    active = {"now": 0, "peak": 0}
    started = []
    gate = threading.Event()

    def worker(idx):
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            started.append(idx)
        gate.wait(timeout=5)
        with lock:
            active["now"] -= 1
        return idx

    qm = QueueManager(worker=worker, max_concurrent=2, default_job_seconds=10)
    snaps = [qm.submit(i) for i in range(5)]

    # Exactly two jobs should be running; the other three are queued.
    assert wait_for(lambda: len(started) == 2)
    assert set(started) == {0, 1}
    assert active["peak"] == 2

    # The three queued jobs report sequential positions and a positive ETA.
    positions = set()
    for snap in snaps[2:]:
        status = qm.get_status(snap["job_id"])
        assert status["status"] == "queued"
        assert status["estimated_wait_seconds"] > 0
        positions.add(status["position"])
    assert positions == {1, 2, 3}

    # Release everything; remaining jobs drain through the two lanes in FIFO order.
    gate.set()
    assert wait_for(lambda: len(started) == 5)
    assert active["peak"] == 2
    assert set(started[:2]) == {0, 1}
    assert set(started[2:4]) == {2, 3}
    assert started[4] == 4

    for snap in snaps:
        assert wait_for(lambda s=snap: qm.get_status(s["job_id"])["status"] == "completed")


def test_status_transitions_running_then_completed():
    """A job moves queued -> running -> completed and exposes its result."""
    gate = threading.Event()

    def worker():
        gate.wait(timeout=5)
        return {"ok": True}

    qm = QueueManager(worker=worker, max_concurrent=1)
    snap = qm.submit()

    assert wait_for(lambda: qm.get_status(snap["job_id"])["status"] == "running")
    running = qm.get_status(snap["job_id"])
    assert running["position"] == 0
    assert running["started_at"] is not None

    gate.set()
    assert wait_for(lambda: qm.get_status(snap["job_id"])["status"] == "completed")
    done = qm.get_status(snap["job_id"], include_result=True)
    assert done["result"] == {"ok": True}
    assert done["finished_at"] is not None


def test_failed_job_sets_error_and_queue_continues():
    """A worker exception marks the job failed without stalling the queue."""
    def worker(should_fail):
        if should_fail:
            raise ValueError("boom")
        return "ok"

    qm = QueueManager(worker=worker, max_concurrent=1, default_job_seconds=1)

    bad = qm.submit(True)
    assert wait_for(lambda: qm.get_status(bad["job_id"])["status"] == "failed")
    assert "boom" in qm.get_status(bad["job_id"])["error"]

    # The next job still runs to completion.
    good = qm.submit(False)
    assert wait_for(lambda: qm.get_status(good["job_id"])["status"] == "completed")


def test_result_is_error_predicate_marks_failed():
    """A normal return that matches ``result_is_error`` is reported as failed."""
    def worker():
        return {"error": "private repo"}

    qm = QueueManager(
        worker=worker,
        max_concurrent=1,
        result_is_error=lambda r: isinstance(r, dict) and bool(r.get("error")),
    )
    snap = qm.submit()

    assert wait_for(lambda: qm.get_status(snap["job_id"])["status"] == "failed")
    status = qm.get_status(snap["job_id"], include_result=True)
    assert status["error"] == "private repo"
    assert status["result"] == {"error": "private repo"}


def test_cancel_queued_job_keeps_queue_consistent():
    """Cancelling a queued job removes it and never executes its work."""
    gate = threading.Event()
    started = []
    lock = threading.Lock()

    def worker(idx):
        with lock:
            started.append(idx)
        gate.wait(timeout=5)
        return idx

    qm = QueueManager(worker=worker, max_concurrent=2, default_job_seconds=5)
    a = qm.submit(0)
    b = qm.submit(1)
    c = qm.submit(2)  # queued behind the two running jobs

    assert wait_for(lambda: len(started) == 2)
    assert qm.get_status(c["job_id"])["status"] == "queued"

    cancelled = qm.cancel(c["job_id"])
    assert cancelled["status"] == "cancelled"

    gate.set()
    assert wait_for(lambda: qm.get_status(a["job_id"])["status"] == "completed")
    assert wait_for(lambda: qm.get_status(b["job_id"])["status"] == "completed")

    # The cancelled job's work must never have run.
    time.sleep(0.1)
    assert 2 not in started
    assert qm.get_status(c["job_id"])["status"] == "cancelled"


def test_stats_reports_capacity():
    qm = QueueManager(worker=lambda: None, max_concurrent=2)
    stats = qm.stats()
    assert stats["max_concurrent"] == 2
    assert stats["running"] == 0
    assert stats["queued"] == 0


def test_get_status_unknown_job_returns_none():
    qm = QueueManager(worker=lambda: None, max_concurrent=2)
    assert qm.get_status("missing") is None
    assert qm.cancel("missing") is None
