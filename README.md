# reviews-finder

Self-hosted Google Maps review scraper — a drop-in replacement for the Apify
reviews actor. Pass any Google place reference and it fetches **all** reviews
over plain HTTPS (no browser, no API key, no third-party service). It can also
**find all places** for a city + categories, with full business details
(see [Finding places](#finding-places-city--categories) below).

It also ships as a **self-hosted run API** (Apify-style: submit → run id →
poll → fetch results) with one Docker container per run, RAM/CPU caps and a
shared Postgres — see [SERVER_SETUP.md](SERVER_SETUP.md).

## How it works

When you open a place on Google Maps and scroll the reviews panel, the page
calls an internal paginated RPC endpoint
(`/httpservice/web/PrivateLocalSearchUiDataService/GetLocalBoqProxy`) that
returns ~10 reviews on the first page and ~20 per page after that, chained by
an opaque pagination token. This tool calls that endpoint directly with
`requests` — the same technique Apify's actor uses (their logs show a plain
`HttpCrawler`, no browser).

Pipeline:

1. **Resolve** ([resolver.py](reviews_finder/resolver.py)) — turn the input
   (place URL / short link / `ChIJ..` place id) into Google's internal
   feature id (`0x..:0x..`) by fetching the place page HTML once.
2. **Fetch** ([fetcher.py](reviews_finder/fetcher.py)) — page through the RPC
   endpoint with retry/backoff, following the pagination token until it runs out.
3. **Parse** ([parser.py](reviews_finder/parser.py)) — decode each raw
   protobuf-over-JSON array into a clean review dict (defensive parsing with
   scanning fallbacks, since Google occasionally reshuffles field positions).

## Install

```
pip install -r requirements.txt
```

## CLI usage

```
python main.py "<place>" [--sort newest|relevant|highest|lowest] [--max N] [--out file.json] [--hl en] [--delay 0.3] [--raw] [--proxy URL ...] [--proxy-file proxies.txt] [--resume] [--ratings 1,2] [--details-only] [--no-details]
```

`<place>` accepts any of:

- Full place URL: `https://www.google.com/maps/place/...`
- Search URL: `https://www.google.com/maps/search/?api=1&query=...&query_place_id=ChIJ...`
- Short link: `https://maps.app.goo.gl/xxxx`
- Bare place id: `ChIJ3y2MDjnEyIARGwx3B-xtpvM`
- Bare feature id: `0x80c8c4390e8c2ddf:0xf3a66dec07770c1b`

Example:

```
python main.py ChIJ3y2MDjnEyIARGwx3B-xtpvM --sort newest --out hilton.json
```

## Library usage

```python
from reviews_finder import scrape_reviews

result = scrape_reviews(
    "https://www.google.com/maps/search/?api=1&query=...&query_place_id=ChIJ...",
    sort="newest",       # relevant | newest | highest | lowest
    max_reviews=None,    # None = all
    hl="en",             # language for relative dates / owner replies
    delay=0.3,           # politeness delay between pages
)
print(result["review_count"], result["feature_id"])
for r in result["reviews"]:
    print(r["rating"], r["author"]["name"], r["published_at"], (r["text"] or "")[:80])
```

## Finding places (city + categories)

`find.py` discovers **all places** for a city across one or more categories and
returns each place's business details (no reviews) — same fields as
`place_details` below:

```
python find.py "<city>" --categories "cat1,cat2,..." [--max N] [--out places.json] [--hl en] [--gl us] [--delay 0.3] [--fast] [--workers 4] [--proxy URL ...] [--proxy-file proxies.txt]
```

Examples:

```
python find.py "Berlin" --categories restaurant
python find.py "Berlin, Germany" --categories "dentist,orthodontist" --gl de --out berlin-dentists.json
python find.py "Munich" --categories cafe --fast        # search data only, much faster
```

Library:

```python
from reviews_finder import find_places, search_places

result = find_places("Berlin", ["restaurant", "cafe"], details=True, workers=4)
for p in result["places"]:
    print(p["name"], p["rating"], p["total_reviews"], p["address"], p["search_categories"])

# or a single raw query, search data only:
places = search_places("dentist in Munich")
```

How it works: each category becomes a `"<category> in <city>"` query against
Google's internal `/search?tbm=map` RPC — the same request the Maps frontend
sends while you scroll the results panel — paged 20 at a time via the `!8i`
offset until the stream ends. Each result carries a full place node with the
**same field layout** as the details endpoint, so name, address, phone,
website, coordinates, categories, rating and opening hours come straight from
the search (one request per 20 places).

Facts about this endpoint, verified live:

- **Google caps one query at ~200–300 places.** "restaurants in Berlin" ends
  after ~230 even though Berlin has thousands. For wider coverage, split into
  narrower categories (`"pizza restaurant,italian restaurant,..."`) or
  city districts (`"restaurant in Kreuzberg, Berlin"`) — results are deduped
  by feature id across queries, and each place records which categories found
  it under `search_categories`.
- **The end of the stream is an empty page**; out-of-range offsets silently
  wrap around to page 0 (guarded by the dedupe).
- **Search results never include `total_reviews` or the histogram** — Google
  cuts those from the search node. By default every found place is therefore
  enriched with one `fetch_place_details` call (a few requests per place, on
  `--workers` threads). `--fast` skips this: you keep `rating` but
  `total_reviews`/`reviews_distribution` stay `null` — fine for building a
  place list, not enough for rating maths.
- **Opening hours are richer in search than in details** (full week vs.
  current day); the merge keeps the richer one.

Output shape:

```json
{
  "city": "Berlin",
  "categories": ["restaurant", "cafe"],
  "place_count": 412,
  "places": [ { ...same fields as place_details..., "search_categories": ["restaurant"] } ]
}
```

## Business details & rating distribution

Every run also returns `place_details` (one extra request, skip with
`--no-details`) and a `rating_distribution` computed from the fetched reviews.
For details without scraping reviews at all, use `--details-only`.

```json
"place_details": {
  "name": "McDonald's",
  "place_id": "ChIJBUVXPv5QqEcRGlvQnGYFiUg",
  "feature_id": "0x47a850fe3e574505:0x488905669cd05b1a",
  "address": "Hardenbergpl. 11, 10623 Berlin, Germany",
  "address_components": {"street": "…", "locality": "10623 Berlin", "country": "Germany"},
  "phone": "+49 30 30827833",
  "phone_e164": "+493030827833",
  "website": "https://…",
  "website_domain": "mcdonalds.com",
  "categories": ["Fast food restaurant"],
  "latitude": 52.5070151, "longitude": 13.332541,
  "timezone": "Europe/Berlin",
  "rating": 3.6,             // Google's live rating, across ALL reviews
  "total_reviews": 6315,     // Google's live count, across ALL reviews
  "reviews_distribution": {  // Google's OWN histogram — the 5 bars in the UI
    "counts": {"1": 946, "2": 464, "3": 1094, "4": 1556, "5": 2255},
    "total": 6315,
    "average": 3.59
  },
  "reviews_url": "https://search.google.com/local/reviews?placeid=ChIJ…",
  "opening_hours": {"Monday": ["Open 24 hours"]},
  "maps_url": "https://www.google.com/maps/place/?q=place_id:ChIJ…"
},
"rating_distribution": {
  "counts": {"1": 22, "2": 12, "3": 30, "4": 61, "5": 175},
  "scraped": 300,            // reviews this run measured
  "average": 4.18,
  "total_reviews": 13460,    // Google's real total
  "coverage": 0.0223         // scraped / total -> how complete the breakdown is
}
```

`rating`, `total_reviews` and `reviews_distribution` are Google's own live
figures for the **whole place** — the exact numbers behind the 5 bars in the
Maps UI — and are fetched without scraping a single review. Verified against
Apify's `reviewsDistribution` for the same place: identical
(223/11/43/112/582 on Google Berlin), and each histogram sums exactly to
`total_reviews` across all places tested.

Do not confuse it with the separate top-level `rating_distribution`, which
only describes the reviews this run happened to scrape.

### Rating impact of deleting reviews

```python
from reviews_finder import fetch_place_details, projected_rating

d = fetch_place_details("0x47a851c4adb5e545:0x91a95da0b8c28d69")
projected_rating(d, [1] * 100)          # remove 100 one-star reviews
# {'current_rating': 3.84, 'current_total': 971, 'deleted_count': 100,
#  'new_rating': 4.17, 'new_total': 871, 'delta': 0.33,
#  'new_distribution': {1: 123, 2: 11, 3: 43, 4: 112, 5: 582}, 'exact': True}
```

`exact: True` means it was computed from Google's real histogram rather than
the rounded published rating, and `new_distribution` gives the bars the place
would show afterwards — useful for quoting the outcome to a client up front.

Caveats, all verified against the live endpoint:

- **The full pb matters.** `total_reviews` and `reviews_distribution` come
  back only for the complete field mask in `PB_TEMPLATE`, captured verbatim
  from a real browser session. Hand-trimmed masks silently return a place node
  with both fields null — which is why several published scrapers report the
  histogram as "no longer available". Do not shorten it.
- **Both fields also need a warmed session and a retry.** Google serves the
  full response non-deterministically, and only to a session that has already
  loaded google.com/maps. `fetch_place_details` warms a fresh session per
  attempt and retries up to 6 times (measured 6/6 success across three
  places), costing a few extra requests and a few seconds. No browser is
  needed at runtime.
- **No email.** Google Maps stores no email address for a business — it is in
  no response. Follow `website` (e.g. an Impressum/contact page) for that.
- **`opening_hours` usually holds only the current day** — that is all this
  endpoint returns; some places (hotels) return none at all.

## Output shape

```json
{
  "place": "<input>",
  "place_name": "…",
  "feature_id": "0x..:0x..",
  "sort": "newest",
  "review_count": 1984,
  "reviews": [
    {
      "review_id": "Ci9DQUlR…",
      "rating": 1,
      "text": "…",
      "language": "en",
      "published_at": "2026-07-18T05:38:57Z",
      "published_relative": "a day ago",
      "author": {"id": "1107…", "name": "…", "avatar_url": "…", "profile_url": "…"},
      "owner_response": {"text": "…", "published_relative": "4 months ago"} | null,
      "images": ["https://lh3.googleusercontent.com/…"],
      "review_url": "https://www.google.com/maps/reviews/…"
    }
  ]
}
```

`review_id` is stable across runs — use it to dedupe / diff against previous
snapshots.

## Notes & limits

- **Sorted views are capped by Google**: `--sort lowest/highest/relevant` are
  ranked views whose pagination Google terminates after ~800 reviews (verified:
  a 12k-review place ends its lowest-sorted cursor at 796, ratings ascending
  1★→5★), so they do NOT cover the full history on big places. Only
  `--sort newest` walks every review. To get *all* low-star reviews on a large
  place, use `--sort newest --ratings 1,2` — all pages are traversed and only
  matching ratings are kept.
- **Page size**: the endpoint serves up to 60 reviews per request (the Maps
  frontend only asks for 10/20); the fetcher always requests 60, so a
  5,000-review place is ~85 requests.
- **Rate limiting**: Google throttles sustained scraping per IP — in testing,
  ~230 rapid requests triggered HTTP 429. The fetcher backs off up to ~2
  minutes per retry on 429; if the throttle persists anyway, the scrape stops
  gracefully and returns everything fetched so far, with `"complete": false`
  and a `"stopped_reason"` in the output.
- **Crash safety & resume**: while running, every fetched review is appended
  immediately to `<out>.partial.jsonl` and the pagination cursor saved to
  `<out>.state.json`, so a killed/crashed run loses nothing. Rerun with
  `--resume` (same place + sort) to continue from the saved cursor —
  already-seen reviews are deduped by `review_id`. Both files are deleted
  automatically after a successful run. (Library: `checkpoint=`/`resume=`
  params on `scrape_reviews`.)
- **Proxies (production)**: pass `--proxy http://user:pass@host:port`
  (repeatable) or `--proxy-file proxies.txt` (one per line, `#` comments).
  Pages are rotated round-robin across the pool, and a 429 immediately hops
  to the next proxy instead of waiting out the cool-down — so `--delay 0` is
  fine. The pagination token is not IP-bound (no cookies involved), so a page
  chain can span proxies. Note proxies don't parallelize a *single* place —
  pagination is a sequential token chain — they remove the delay and let you
  scrape many places concurrently (one worker per place).
- **Translations**: `hl` controls Google's UI language. Review text comes back
  in its original language (plus Google-translated text for some locales).
- **Field drift**: Google reshuffles the raw arrays occasionally. The parser
  anchors on the stable fields (rating/time/author/id at fixed low indexes)
  and falls back to scanning for the text fields. If reviews suddenly come
  back with `"text": null` everywhere, run with `--raw` and inspect `_raw` to
  re-map indexes in [parser.py](reviews_finder/parser.py).
- The older public endpoints (`listentitiesreviews`, `listugcposts`) are
  dead/empty as of mid-2026; `GetLocalBoqProxy` is what the Maps frontend
  itself uses now.
