# reviews-finder

Self-hosted Google Maps review scraper — a drop-in replacement for the Apify
reviews actor. Pass any Google place reference and it fetches **all** reviews
over plain HTTPS (no browser, no API key, no third-party service).

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
python main.py "<place>" [--sort newest|relevant|highest|lowest] [--max N] [--out file.json] [--hl en] [--delay 0.3] [--raw] [--proxy URL ...] [--proxy-file proxies.txt] [--resume]
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
