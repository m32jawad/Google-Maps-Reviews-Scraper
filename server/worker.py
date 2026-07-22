"""Worker entrypoint -- runs inside its own container (or subprocess in dev).

Reads RUN_ID + DATABASE_URL from the environment, executes the requested
actor, streams progress into the run row (throttled), and writes the final
result + status back. The scheduler handles everything else (timeouts,
aborts, crash detection) from outside.
"""
import logging
import os
import time
import traceback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")


def _proxies(input_proxies):
    if input_proxies:
        return input_proxies
    from . import settings
    return settings.DEFAULT_PROXY_URLS or None


class ProgressWriter:
    """Throttled progress updates so we don't hammer the shared database."""

    def __init__(self, db, run, interval=2.0):
        self.db, self.run, self.interval = db, run, interval
        self._last = 0.0

    def __call__(self, progress):
        now = time.monotonic()
        if now - self._last < self.interval:
            return
        self._last = now
        self.run.progress = progress
        self.db.commit()


class ItemWriter:
    """Appends each page's reviews to run_item so the API can serve them mid-run.

    Deliberately unthrottled (unlike progress): the point is that a consumer
    sees reviews as they are found. One insert batch + commit per page keeps
    that cheap. Failures are swallowed -- these rows are a convenience, the
    run's final `result` is what actually matters.
    """

    def __init__(self, db, run):
        self.db, self.run = db, run
        self.seq = 0

    def __call__(self, items):
        from .models import RunItem
        try:
            self.db.add_all([RunItem(run_id=self.run.id, seq=self.seq + i, data=item)
                             for i, item in enumerate(items)])
            self.db.commit()
            self.seq += len(items)
        except Exception as e:
            log.warning("run %s: could not store %d partial items: %s",
                        self.run.id, len(items), e)
            self.db.rollback()


def run_actor(run, progress, items=None):
    inp = dict(run.input)
    proxies = _proxies(inp.pop("proxies", None))

    if run.actor == "reviews":
        from reviews_finder import scrape_reviews
        return scrape_reviews(
            inp["place"], sort=inp.get("sort", "newest"),
            max_reviews=inp.get("max_reviews"), hl=inp.get("hl", "en"),
            delay=inp.get("delay", 0.3), proxies=proxies,
            ratings=set(inp["ratings"]) if inp.get("ratings") else None,
            details=inp.get("details", True),
            on_progress=lambda count, page: progress(
                {"reviews": count, "page": page}),
            on_items=items,
        )

    if run.actor == "places":
        from reviews_finder import find_places
        return find_places(
            inp["city"], inp["categories"], max_places=inp.get("max_places"),
            hl=inp.get("hl", "en"), gl=inp.get("gl", "us"),
            delay=inp.get("delay", 0.3), details=inp.get("details", True),
            workers=inp.get("workers", 4), proxies=proxies,
            on_progress=lambda stage, label, count: progress(
                {"stage": stage, "label": label, "count": count}),
        )

    raise ValueError(f"unknown actor: {run.actor}")


def main():
    run_id = os.environ["RUN_ID"]
    from .db import SessionLocal
    from .models import Run, utcnow

    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            log.error("run %s not found", run_id)
            raise SystemExit(2)
        log.info("run %s: actor=%s input=%s", run.id, run.actor, run.input)

        try:
            result = run_actor(run, ProgressWriter(db, run), ItemWriter(db, run))
            # The scheduler may have flipped the status (abort) while we worked;
            # only a still-RUNNING run gets to succeed.
            db.refresh(run)
            if run.status != "RUNNING":
                log.info("run %s ended as %s while working, discarding result",
                         run.id, run.status)
                return
            run.result = result
            run.status = "SUCCEEDED"
            run.finished_at = utcnow()
            db.commit()
            log.info("run %s succeeded", run.id)
        except Exception:
            err = traceback.format_exc()
            log.error("run %s failed:\n%s", run_id, err)
            db.rollback()
            run = db.get(Run, run_id)
            if run is not None and run.status == "RUNNING":
                run.status = "FAILED"
                run.error = err[-4000:]
                run.finished_at = utcnow()
                db.commit()
            raise SystemExit(1)


if __name__ == "__main__":
    main()
