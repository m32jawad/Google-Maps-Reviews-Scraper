# reviews-finder API server

Turns the scrapers into an Apify-style run API: submit a run, get a run id,
poll its status, fetch the results when it succeeds. Every run executes in its
own Docker container with a RAM/CPU cap; one shared Postgres holds runs,
progress and results.

```
client ── POST /v1/runs ──> API (FastAPI) ──> run row: QUEUED
                             │  scheduler thread (every 2s)
                             │    · launches ≤ MAX_CONCURRENT_WORKERS
                             ▼      sibling containers via docker.sock
                        ┌─────────────┐   ┌─────────────┐
                        │ rf-worker-A │   │ rf-worker-B │   (RAM-capped,
                        │  reviews    │   │  places     │    same image)
                        └──────┬──────┘   └──────┬──────┘
                               └────── Postgres ─┘   (shared: status,
                                                      progress, results)
```

## Run lifecycle

`QUEUED → RUNNING → SUCCEEDED | FAILED | TIMED_OUT | ABORTED`
(`ABORTING` appears briefly between an abort request and the worker being killed.)

The scheduler also detects workers that die without reporting back — e.g.
killed by the RAM cap — and marks them `FAILED` with the container's last log
lines in `error`.

## API

Auth: every `/v1/*` request needs `Authorization: Bearer <API_TOKEN>`
(or `?token=<API_TOKEN>`). `/health` is open.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/runs` | submit a run → `{id, status: "QUEUED", ...}` |
| GET | `/v1/runs` | list runs (`?status=RUNNING&actor=reviews&limit=&offset=`) |
| GET | `/v1/runs/{id}` | status + live progress |
| POST | `/v1/runs/{id}/abort` | stop a queued/running run |
| GET | `/v1/runs/{id}/results` | full result JSON (SUCCEEDED only) |
| GET | `/v1/runs/{id}/items` | just the reviews/places array, `?offset=&limit=` |
| DELETE | `/v1/runs/{id}` | delete a finished run (results live in the DB — clean up periodically) |
| GET | `/health` | liveness + queue counts |

Interactive docs at `/docs` (Swagger UI).

### Submitting runs

Two actors. `memory_mb` / `timeout_secs` are optional per-run overrides
(clamped to `MAX_MEMORY_MB` / `MAX_TIMEOUT_SECS`).

**reviews** — all reviews for one place (mirrors `main.py`):

```bash
curl -X POST https://scraper.example.com/v1/runs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "actor": "reviews",
    "input": {
      "place": "ChIJBUVXPv5QqEcRGlvQnGYFiUg",
      "sort": "newest",
      "max_reviews": null,
      "ratings": [1, 2],
      "details": true
    },
    "memory_mb": 512
  }'
```

**places** — all places for a city + categories (mirrors `find.py`):

```bash
curl -X POST https://scraper.example.com/v1/runs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "actor": "places",
    "input": {
      "city": "Berlin, Germany",
      "categories": ["restaurant", "cafe"],
      "gl": "de",
      "details": true,
      "workers": 4
    }
  }'
```

Both accept `"proxies": ["http://user:pass@host:port"]`; without it, workers
use the server-wide `DEFAULT_PROXY_URLS`.

### Polling + fetching

```bash
curl -H "Authorization: Bearer $TOKEN" https://scraper.example.com/v1/runs/<id>
# {"status": "RUNNING", "progress": {"reviews": 1260, "page": 21}, ...}

curl -H "Authorization: Bearer $TOKEN" https://scraper.example.com/v1/runs/<id>/items?limit=1000
# {"total": 6315, "offset": 0, "items": [ ...reviews or places... ]}
```

`/results` returns the exact same JSON the CLI writes to `--out` (including
`place_details`, `rating_distribution`, etc.); `/items` is the paginated
shortcut to just the array — use it from mapnovi-web like Apify's
`datasets/{id}/items`.

## Deploy (Docker)

Needs: a VPS with Docker + compose plugin, a DNS A-record for the scraper
domain pointing at it.

```bash
git clone <repo> && cd reviews-finder
cp .env.example .env
nano .env            # set API_TOKEN (openssl rand -hex 32), POSTGRES_PASSWORD,
                     # MAX_CONCURRENT_WORKERS, DEFAULT_PROXY_URLS, ...
docker compose up -d --build
curl http://localhost:8000/health
```

The compose stack runs Postgres (volume `pgdata`) and the API on
`API_PORT` (default 8000). The API container mounts `/var/run/docker.sock` and
launches worker containers **as siblings on the host daemon**, named
`rf-worker-<run id>`, joined to the `reviews-finder-net` network so they reach
the `db` service. `docker ps` during a run shows them; they are removed after
each run.

Resource knobs (all in `.env`):

- `MAX_CONCURRENT_WORKERS` — how many containers may scrape at once.
- `DEFAULT_MEMORY_MB` / `MAX_MEMORY_MB` — RAM cap per worker container
  (`mem_limit` + `memswap_limit`, so no swap overflow); a run OOM-killed by
  its cap comes back as `FAILED` with "worker exited unexpectedly".
- `WORKER_CPUS` — CPU share per worker.
- `DEFAULT_TIMEOUT_SECS` — runs are killed past this (per-run overridable).

After changing scraper code: `docker compose up -d --build` (workers use the
same image, so one rebuild covers both).

### nginx + TLS for the domain

A ready-to-copy site config is in [deploy/nginx-scraper.conf](deploy/nginx-scraper.conf)
(edit the domain, then follow the numbered steps in its header):

```bash
sudo cp deploy/nginx-scraper.conf /etc/nginx/sites-available/scraper.conf
sudo ln -s /etc/nginx/sites-available/scraper.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d scraper.example.com     # TLS; also adds the 443 block
```

It proxies the domain to `127.0.0.1:8000` with `proxy_buffering off` and long
read timeouts, since `/results` for a big place can be tens of MB. If a result
ever gets too heavy even for that, page through `/items` instead.

### Start on boot (systemd)

The compose services carry `restart: unless-stopped`, so after a reboot they
return by themselves as long as Docker starts on boot
(`sudo systemctl enable docker`). For explicit `systemctl` control there is an
optional unit in [deploy/reviews-finder.service](deploy/reviews-finder.service):

```bash
# edit WorkingDirectory in the file to your clone path first
sudo cp deploy/reviews-finder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reviews-finder
```

Then `systemctl status reviews-finder`, `systemctl restart reviews-finder`,
logs via `docker compose logs -f api`.

## Local development (no Docker)

```bash
pip install -r server/requirements.txt   # psycopg2 optional locally
EXECUTOR=subprocess API_TOKEN=dev python -m uvicorn server.app:app --port 8000
```

`EXECUTOR=subprocess` runs workers as plain child processes and the DB
defaults to SQLite (`runs.db`) — no isolation or RAM caps, but the whole API
flow works for testing. This is exactly how the test suite exercised it.

## mapnovi-web integration (next step)

Replace the Apify client with three calls from the Django backend:

1. `POST /v1/runs` with `{"actor": "reviews", "input": {"place": <maps url>}}`
   → store `run["id"]` where the Apify run id lived.
2. Poll `GET /v1/runs/{id}` (status names match Apify's:
   `SUCCEEDED`/`FAILED`/`TIMED_OUT`/`ABORTED`).
3. On `SUCCEEDED`, `GET /v1/runs/{id}/items` → review objects
   (`review_id`, `rating`, `text`, `published_at`, `author`, ... — see the
   README's output shape).

Keep the token in Django settings/AppConfig (server-side only), never in the
Next.js frontend.
