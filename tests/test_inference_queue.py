"""The GPU gate. These run real threads: serialisation is a concurrency claim,
and asserting it without concurrency proves nothing.
"""
import threading
import time

import pytest

from app import inference_queue as q
from app.inference_queue import InferenceBusy, depth, gpu_slot


@pytest.fixture(autouse=True)
def fresh_queue():
    """Each test starts with an empty, default-sized queue."""
    q._slot = threading.BoundedSemaphore(1)
    q._waiting = 0
    original = (q.MAX_WAIT, q.MAX_QUEUE)
    yield
    q.MAX_WAIT, q.MAX_QUEUE = original


def test_only_one_runs_at_a_time():
    inside = []
    peak = [0]
    lock = threading.Lock()

    def work():
        with gpu_slot('test'):
            with lock:
                inside.append(1)
                peak[0] = max(peak[0], len(inside))
            time.sleep(0.05)
            with lock:
                inside.pop()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The whole point: six callers, never two on the card together.
    assert peak[0] == 1


def test_the_queue_actually_queues():
    """Six 50ms jobs must take at least 300ms if they are serialised."""
    started = time.monotonic()

    def work():
        with gpu_slot('test'):
            time.sleep(0.05)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert time.monotonic() - started >= 0.25


def test_refuses_when_the_queue_is_too_deep():
    q.MAX_QUEUE = 2
    release = threading.Event()
    holding = threading.Event()

    def hold():
        with gpu_slot('holder'):
            holding.set()
            release.wait(2)

    blocker = threading.Thread(target=hold)
    blocker.start()
    holding.wait(2)

    waiter = threading.Thread(target=lambda: _swallow(hold))
    waiter.start()
    time.sleep(0.05)

    # Two in the queue already; the third is turned away rather than parked.
    with pytest.raises(InferenceBusy):
        with gpu_slot('third'):
            pass

    release.set()
    blocker.join()
    waiter.join()


def _swallow(fn):
    try:
        fn()
    except InferenceBusy:
        pass


def test_gives_up_rather_than_waiting_forever():
    q.MAX_WAIT = 0.05
    release = threading.Event()
    holding = threading.Event()

    def hold():
        with gpu_slot('holder'):
            holding.set()
            release.wait(2)

    blocker = threading.Thread(target=hold)
    blocker.start()
    holding.wait(2)

    # A browser tab held open for minutes is worse than a clear refusal.
    with pytest.raises(InferenceBusy):
        with gpu_slot('waiter'):
            pass

    release.set()
    blocker.join()


def test_the_slot_is_released_when_the_work_raises():
    with pytest.raises(ValueError):
        with gpu_slot('boom'):
            raise ValueError('inference failed')

    # A leaked permit would wedge every later request.
    with gpu_slot('after'):
        pass

    assert depth() == 0


def test_depth_returns_to_zero():
    def work():
        with gpu_slot('test'):
            time.sleep(0.01)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert depth() == 0
