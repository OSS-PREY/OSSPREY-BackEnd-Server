"""Gunicorn configuration for the OSSPREY backend.

IMPORTANT - SINGLE WORKER REQUIRED
----------------------------------
The repository-processing request queue (``app/services/queue_manager.py``) keeps
its state in memory, inside a single process. The endpoint that enqueues a job
(``POST /api/upload_git_link``) and the status polls that follow
(``GET /api/queue_status/<job_id>``) must therefore all be served by the SAME
process.

Running multiple workers (e.g. ``gunicorn -w 4 ...``) breaks this: the poll is
load-balanced to a different worker that has never seen the job, so the API
returns HTTP 404 and the UI shows "Status request failed (404)".

With the in-memory queue, do NOT:
  * set ``workers`` / ``-w`` greater than 1, or
  * pass ``--max-requests`` (recycling a worker mid-job wipes the queue and the
    running pipeline).

To scale horizontally, move the queue state to a shared store (e.g. Redis) and
only then increase the worker count. See the README.

Note: command-line flags override this file, so launch with:
    gunicorn -c gunicorn.conf.py run:app
"""

import os

# REQUIRED: a single worker so the in-memory queue is consistent across requests.
workers = 1

# Multiple threads let the one worker keep serving fast status polls while the
# pipeline runs on a background thread inside the queue's ThreadPoolExecutor.
# (Gunicorn auto-selects the gthread worker when threads > 1; set it explicitly.)
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

# The enqueue endpoint returns immediately, but some data-fetch endpoints can be
# slow; allow generous time before a request is killed.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "600"))

# Bind address/port. Override with GUNICORN_BIND, e.g. "0.0.0.0:5500".
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")

# Auto-reload the server whenever source files change so deployments pick up updates.
reload = True
