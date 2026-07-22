"""Apify-style run API for the reviews-finder scrapers.

  POST   /v1/runs               submit a run -> {id, status: QUEUED}
  GET    /v1/runs               list runs (?status=&actor=&limit=&offset=)
  GET    /v1/runs/{id}          status + progress
  POST   /v1/runs/{id}/abort    stop a queued/running run
  GET    /v1/runs/{id}/results  full result JSON (once SUCCEEDED)
  GET    /v1/runs/{id}/items    just the items (reviews/places), paginated;
                                also serves partial items of a RUNNING run
  DELETE /v1/runs/{id}          delete a finished run
  GET    /health                liveness + queue stats (no auth)

Auth: set API_TOKEN and send  Authorization: Bearer <token>  (or ?token=).

A browser console for all of the above is served at  /  (see static/index.html).
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import settings
from .db import SessionLocal, init_db
from .models import FINISHED, Run, RunItem
from .schemas import CreateRun
from .scheduler import Scheduler

scheduler = Scheduler()


@asynccontextmanager
async def lifespan(_app):
    init_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="reviews-finder API", version="1.0", lifespan=lifespan)


def require_auth(request: Request, token: str = Query(default="")):
    if not settings.API_TOKEN:
        return
    header = request.headers.get("Authorization", "")
    bearer = header.removeprefix("Bearer ").strip()
    if (bearer or token) != settings.API_TOKEN:
        raise HTTPException(401, "invalid or missing API token")


def get_db():
    with SessionLocal() as db:
        yield db


def get_run(run_id: str, db) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    return run


@app.get("/health")
def health():
    with SessionLocal() as db:
        counts = {s: db.query(Run).filter(Run.status == s).count()
                  for s in ("QUEUED", "RUNNING")}
    return {"status": "ok", "executor": settings.EXECUTOR,
            "auth_required": bool(settings.API_TOKEN),
            "max_concurrent_workers": settings.MAX_CONCURRENT_WORKERS, **counts}


@app.post("/v1/runs", status_code=201, dependencies=[Depends(require_auth)])
def create_run(body: CreateRun, db=Depends(get_db)):
    try:
        validated = body.validated_input()
    except ValidationError as e:
        raise HTTPException(422, e.errors())
    run = Run(actor=body.actor, input=validated,
              memory_mb=body.clamped_memory(),
              timeout_secs=body.clamped_timeout())
    db.add(run)
    db.commit()
    return run.to_dict()


@app.get("/v1/runs", dependencies=[Depends(require_auth)])
def list_runs(status: str | None = None, actor: str | None = None,
              limit: int = Query(default=50, le=500), offset: int = 0,
              db=Depends(get_db)):
    q = db.query(Run)
    if status:
        q = q.filter(Run.status == status.upper())
    if actor:
        q = q.filter(Run.actor == actor)
    total = q.count()
    runs = q.order_by(Run.created_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [r.to_dict() for r in runs]}


@app.get("/v1/runs/{run_id}", dependencies=[Depends(require_auth)])
def run_status(run_id: str, db=Depends(get_db)):
    return get_run(run_id, db).to_dict()


@app.post("/v1/runs/{run_id}/abort", dependencies=[Depends(require_auth)])
def abort_run(run_id: str, db=Depends(get_db)):
    run = get_run(run_id, db)
    if run.status == "QUEUED":
        run.status = "ABORTED"
    elif run.status == "RUNNING":
        run.status = "ABORTING"  # scheduler kills the worker on its next tick
    elif run.status not in ("ABORTING",):
        raise HTTPException(409, f"run is already {run.status}")
    db.commit()
    return run.to_dict()


@app.get("/v1/runs/{run_id}/results", dependencies=[Depends(require_auth)])
def run_results(run_id: str, db=Depends(get_db)):
    run = get_run(run_id, db)
    if run.status != "SUCCEEDED":
        raise HTTPException(409, f"run is {run.status}, results exist only for SUCCEEDED runs")
    return run.result


@app.get("/v1/runs/{run_id}/items", dependencies=[Depends(require_auth)])
def run_items(run_id: str, offset: int = 0,
              limit: int = Query(default=1000, le=10000), db=Depends(get_db)):
    """Paginated access to the run's item list (reviews or places).

    Unlike /results this also answers for an unfinished run, serving whatever
    the worker has streamed into run_item so far so a caller can render
    reviews while the scrape is still going. `total` therefore grows between
    calls until the run finishes; once it SUCCEEDED the complete `result` takes
    over as the answer, which is the authoritative and fully sorted set.
    """
    run = get_run(run_id, db)
    if run.status != "SUCCEEDED":
        q = db.query(RunItem).filter(RunItem.run_id == run.id)
        rows = q.order_by(RunItem.seq).offset(offset).limit(limit).all()
        return {"total": q.count(), "offset": offset,
                "items": [r.data for r in rows]}
    key = "reviews" if run.actor == "reviews" else "places"
    items = (run.result or {}).get(key) or []
    return {"total": len(items), "offset": offset,
            "items": items[offset:offset + limit]}


@app.delete("/v1/runs/{run_id}", dependencies=[Depends(require_auth)])
def delete_run(run_id: str, db=Depends(get_db)):
    run = get_run(run_id, db)
    if run.status not in FINISHED:
        raise HTTPException(409, "abort the run before deleting it")
    db.delete(run)
    db.commit()
    return {"deleted": run_id}


# --- browser console -------------------------------------------------------
# Mounted last so it never shadows an API route. The page itself is public;
# every call it makes goes through the same token auth as the API.
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/ui/")


if STATIC_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
