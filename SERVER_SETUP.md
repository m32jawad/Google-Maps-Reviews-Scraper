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

## Browser console

Open the server root (`http://localhost:8000/`) and you get a small web UI —
the same thing the API does, without curl:

- **API token** field in the header, kept in `localStorage` (per browser).
  Leave it empty when `API_TOKEN` is unset. The header shows whether the
  server currently requires one.
- **Reviews scraper / Places scraper** tabs, each with the full input form
  (sort, max, star filter, language, delay, proxies, details toggle).
- **Runs table**, auto-refreshing every 2.5s: status badge, live progress
  (`40 reviews · page 1`, `details · <place> · 4`), plus Abort / Delete.
- **Results panel** — click *View* on a succeeded run to render the records
  in-page: for reviews, the business details and Google's own star histogram
  above the review table; for places, one row per business.
- **Download JSON** for the full result, and an optional *auto-download when
  a run finishes* checkbox (only fires for runs that finish while the page is
  open, so it never dumps your whole history on load).

The page is plain static HTML served from `server/static/` — no build step, no
CDN — and every call it makes goes through the same token auth as the API.

## API

Auth: every `/v1/*` request needs `Authorization: Bearer <API_TOKEN>`
(or `?token=<API_TOKEN>`). `/health` and the console page itself are open.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/runs` | submit a run → `{id, status: "QUEUED", ...}` |
| GET | `/v1/runs` | list runs (`?status=RUNNING&actor=reviews&limit=&offset=`) |
| GET | `/v1/runs/{id}` | status + live progress |
| POST | `/v1/runs/{id}/abort` | stop a queued/running run |
| GET | `/v1/runs/{id}/results` | full result JSON (SUCCEEDED only) |
| GET | `/v1/runs/{id}/items` | just the reviews/places array, `?offset=&limit=` |
| DELETE | `/v1/runs/{id}` | delete a finished run (results live in the DB — clean up periodically) |
| GET | `/health` | liveness + queue counts + `auth_required` |
| GET | `/` | browser console (redirects to `/ui/`) |

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

## mapnovi-web integration (done)

mapnovi-web talks to this API from `backend/api/custom_scraper.py`. It is a
**partial** swap by design: only the REVIEW run moves here — the place
resolve/stats run stays on Apify, because it supplies the business name,
address and the 1–5★ distribution the review run is sized from.

Turn it on under **Admin → Konfiguration → "Eigener Scraper (reviews-finder)"**
(also in the Django admin): tick the box, set the API URL and paste the
`API_TOKEN`. All three are required — with anything missing mapnovi silently
keeps using Apify, so the switch is safe to flip back at any time.

What happens per extraction:

1. Apify stats run resolves the place → name, address, rating, star counts.
2. The deletable 1–3★ count sizes the review run, which is sent here as
   `POST /v1/runs` with `ratings: [1,2,3]` and a sort chosen from that count:
   `lowest` (fast) up to ~700, else `newest` (walks the full history, since
   Google caps rating-sorted pagination at ~800).
3. The funnel polls `GET /v1/runs/{id}`; when it reports `SUCCEEDED` the
   backend fetches `/results` and maps each review into the same dict the
   Apify path produced. Run ids are prefixed `cs-` so poll/abort route to the
   right backend, and the funnel's abort-on-leave hits `/abort` here.

The token lives encrypted in `AppConfig` (server-side only) and is never sent
to the Next.js frontend.
