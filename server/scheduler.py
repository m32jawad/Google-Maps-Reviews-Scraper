"""Background loop that turns QUEUED runs into worker containers.

Runs inside the API process on a daemon thread. Each tick:
  1. reconcile RUNNING runs whose worker died without reporting back -> FAILED
  2. enforce per-run timeouts -> TIMED_OUT
  3. kill workers of runs marked ABORTING -> ABORTED
  4. launch QUEUED runs (oldest first) while below MAX_CONCURRENT_WORKERS
"""
import logging
import threading
import time

from . import settings
from .db import SessionLocal
from .executor import make_executor
from .models import Run, utcnow

log = logging.getLogger("scheduler")


class Scheduler:
    def __init__(self):
        self.executor = make_executor()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(settings.SCHEDULER_INTERVAL_SECS):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    def tick(self):
        with SessionLocal() as db:
            self._reconcile(db)
            self._enforce_timeouts(db)
            self._handle_aborts(db)
            self._launch_queued(db)

    def _finish(self, db, run, status, error=None):
        run.status = status
        run.error = error
        run.finished_at = utcnow()
        db.commit()

    def _reconcile(self, db):
        for run in db.query(Run).filter(Run.status == "RUNNING").all():
            if run.handle and self.executor.is_running(run.handle):
                continue
            # Worker gone. Re-read: it may have just committed SUCCEEDED/FAILED.
            db.refresh(run)
            logs = self.executor.cleanup(run.handle) if run.handle else None
            if run.status == "RUNNING":  # died without reporting back (OOM, crash)
                error = "worker exited unexpectedly (out of memory?)"
                if logs:
                    error += "\n--- last worker output ---\n" + logs
                self._finish(db, run, "FAILED", error)

    def _enforce_timeouts(self, db):
        now = utcnow()
        for run in db.query(Run).filter(Run.status == "RUNNING").all():
            started = run.started_at
            if started is None:
                continue
            if started.tzinfo is None:  # SQLite loses tzinfo
                elapsed = (now.replace(tzinfo=None) - started).total_seconds()
            else:
                elapsed = (now - started).total_seconds()
            if elapsed > run.timeout_secs:
                if run.handle:
                    self.executor.kill(run.handle)
                    self.executor.cleanup(run.handle)
                self._finish(db, run, "TIMED_OUT",
                             f"run exceeded timeout of {run.timeout_secs}s")

    def _handle_aborts(self, db):
        for run in db.query(Run).filter(Run.status == "ABORTING").all():
            if run.handle:
                self.executor.kill(run.handle)
                self.executor.cleanup(run.handle)
            self._finish(db, run, "ABORTED")

    def _launch_queued(self, db):
        running = db.query(Run).filter(Run.status == "RUNNING").count()
        capacity = settings.MAX_CONCURRENT_WORKERS - running
        if capacity <= 0:
            return
        queued = (db.query(Run).filter(Run.status == "QUEUED")
                  .order_by(Run.created_at).limit(capacity).all())
        for run in queued:
            try:
                # Mark RUNNING before launch so a slow launch can't double-start.
                run.status = "RUNNING"
                run.started_at = utcnow()
                db.commit()
                run.handle = self.executor.launch(run)
                db.commit()
                log.info("launched run %s (%s) as %s", run.id, run.actor, run.handle)
            except Exception as e:
                log.exception("failed to launch run %s", run.id)
                self._finish(db, run, "FAILED", f"failed to launch worker: {e}")
