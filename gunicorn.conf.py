"""Gunicorn config: prometheus multiprocess housekeeping."""

import os
import shutil

# Set here (not in compose) so only gunicorn uses multiprocess mode; tests and the
# worker keep the default in-process registry. Inherited by forked workers.
os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/prometheus")  # noqa: S108 container-local, recreated on start

bind = "0.0.0.0:8000"
workers = int(os.environ.get("WEB_CONCURRENCY", "4"))
accesslog = None  # our middleware already logs each request as JSON


def on_starting(server):
    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if path:
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)


def child_exit(server, worker):
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
