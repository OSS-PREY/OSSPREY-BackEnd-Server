"""One GPU, one job at a time.

Pain points and actionable selection both drive the same TITAN V, and every
request wants several seconds of it. Without a gate, two people loading
dashboards at once fire an embedding and two generations at the card
simultaneously: Ollama serialises them anyway, but only after each has loaded
its weights, so they thrash VRAM and every request gets slower than if they had
simply waited. Three at once can push the 12 GB card into swapping models in
and out between tokens.

So requests take turns. The backend runs a single gunicorn worker by design
(see gunicorn.conf.py -- the job queue is in-process), which is what makes a
plain in-process semaphore sufficient: there is no second worker to coordinate
with.

Callers that would wait too long are turned away rather than queued forever --
a browser tab holding a connection for five minutes is worse than a clear "busy,
try again".

Note this gates OUR calls only. RepoWise serves the chat widget from its own
process on the same GPU; nothing here can hold that back.
"""
import logging
import os
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Longest a request will wait for its turn. Gunicorn's own timeout is 600s and a
# generation can take 300s, so this leaves room for the run itself.
MAX_WAIT = int(os.environ.get('INFERENCE_QUEUE_WAIT', '240'))

# How many may be waiting before new arrivals are refused outright. Past this,
# the wait is longer than anyone will sit through, so saying so immediately is
# kinder than a connection that hangs.
MAX_QUEUE = int(os.environ.get('INFERENCE_QUEUE_DEPTH', '6'))

_slot = threading.BoundedSemaphore(1)
_counter = threading.Lock()
_waiting = 0


class InferenceBusy(Exception):
    """Too many requests already queued for the GPU."""


def depth():
    """How many requests are waiting or running. For logging and health."""
    return _waiting


@contextmanager
def gpu_slot(label):
    """Hold the GPU for the duration of the block.

    Raises InferenceBusy if the queue is already too deep, or if the wait runs
    past MAX_WAIT.
    """
    global _waiting

    with _counter:
        if _waiting >= MAX_QUEUE:
            raise InferenceBusy(f'{_waiting} requests already queued')
        _waiting += 1
        position = _waiting

    if position > 1:
        logger.info(f'inference queue: {label} waiting, position {position}')

    started = time.monotonic()
    try:
        if not _slot.acquire(timeout=MAX_WAIT):
            raise InferenceBusy(f'waited {MAX_WAIT}s for the GPU')

        waited = time.monotonic() - started
        if waited > 1:
            logger.info(f'inference queue: {label} starting after {waited:.0f}s')

        try:
            yield
        finally:
            _slot.release()
    finally:
        with _counter:
            _waiting -= 1
