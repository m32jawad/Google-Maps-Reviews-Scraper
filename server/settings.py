"""Environment-driven configuration for the API server and workers.

Everything has a dev-friendly default: with no env at all you get SQLite +
subprocess workers + no auth, which is what the local test loop uses. The
docker-compose file overrides these for production.
"""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _list(name):
    raw = os.environ.get(name, "")
    return [p for p in (x.strip() for x in raw.replace("\n", ",").split(",")) if p]


# --- API ---
API_TOKEN = os.environ.get("API_TOKEN", "")  # empty = auth disabled (dev only)

# --- database (shared between API and all workers) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./runs.db")

# --- worker orchestration ---
# "docker": one container per run (production). "subprocess": plain child
# process, no isolation / RAM cap (local development on machines without docker).
EXECUTOR = os.environ.get("EXECUTOR", "docker").lower()
MAX_CONCURRENT_WORKERS = _int("MAX_CONCURRENT_WORKERS", 2)
SCHEDULER_INTERVAL_SECS = _float("SCHEDULER_INTERVAL_SECS", 2.0)

# --- per-run resources (request can override memory/timeout up to the max) ---
DEFAULT_MEMORY_MB = _int("DEFAULT_MEMORY_MB", 512)
MAX_MEMORY_MB = _int("MAX_MEMORY_MB", 2048)
DEFAULT_TIMEOUT_SECS = _int("DEFAULT_TIMEOUT_SECS", 3600)
MAX_TIMEOUT_SECS = _int("MAX_TIMEOUT_SECS", 6 * 3600)
WORKER_CPUS = _float("WORKER_CPUS", 1.0)

# --- docker executor ---
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "reviews-finder:latest")
# The compose network workers must join to reach the database service.
WORKER_NETWORK = os.environ.get("WORKER_NETWORK", "reviews-finder-net")
# DATABASE_URL as seen from INSIDE a worker container (the API may reach the
# db as "db:5432" via compose; workers launched as siblings need the same).
WORKER_DATABASE_URL = os.environ.get("WORKER_DATABASE_URL", "") or DATABASE_URL

# --- scraping defaults injected into every run that doesn't bring its own ---
DEFAULT_PROXY_URLS = _list("DEFAULT_PROXY_URLS")
