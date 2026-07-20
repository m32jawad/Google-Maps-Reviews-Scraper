"""Find all places for a search query (e.g. "restaurants in Berlin") via
Google's internal /search?tbm=map RPC -- the same request the Maps frontend
sends while you scroll the results panel. Plain HTTPS GET, no API key.

Layout facts, all verified against the live endpoint (July 2026):

* Results live at data[0][1]: entry [0] is metadata, each real result entry
  carries a full place node at [14] with the SAME field indexes as the
  /maps/preview/place node, so `details.parse_place_node` decodes both.
* Pagination is `!8i<offset>` in the pb (20 per page via `!7i20`). The stream
  ends with an empty page (a ~760-byte response with no result list); Google
  caps one query at roughly 200-300 places. Out-of-range offsets WRAP AROUND
  to page 0 instead of erroring, so paging must stop on the first empty page
  and dedupe by feature id.
* The search node never carries `total_reviews` / the histogram ([4] is cut
  to just the rating and [175][3] is null); those need one
  `fetch_place_details` call per place, which `find_places(details=True)`
  does on a small thread pool.
* Each result entry embeds exactly one ChIJ.. place id (not inside the node
  itself), so it is regex-scanned from the entry.
"""
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

from .details import fetch_place_details, parse_place_node
from .fetcher import ProxyPool
from .resolver import CONSENT_COOKIES, DEFAULT_HEADERS

ENDPOINT = "https://www.google.com/search"

PAGE_SIZE = 20  # !7i -- the endpoint ignores larger values

# Captured from the Maps frontend's search request. !1s is the query, !7i the
# page size, !8i the offset; the !20m57 block is the same place field mask the
# details endpoint uses -- trimming it returns marker-only results with no
# place nodes at all.
PB_TEMPLATE = (
    "!1s{query}!7i{count}!8i{offset}!10b1!12m6!1m1!18b1!2m1!20e3!6m1!114b1"
    "!17m1!3e1!20m57!2m2!1i203!2i100!3m2!2i4!5b1!6m6!1m2!1i86!2i86!1m2!1i408"
    "!2i240!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e8!2b0"
    "!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2"
    "!2b1!9b0!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
)

PLACE_ID_RE = re.compile(r'"(ChIJ[0-9A-Za-z_-]{10,})"')


def make_search_session():
    s = requests.Session()
    s.headers.update({**DEFAULT_HEADERS, "Accept": "*/*"})
    for name, value in CONSENT_COOKIES.items():
        s.cookies.set(name, value, domain=".google.com")
    return s


def build_search_url(query, offset=0, hl="en", gl="us", count=PAGE_SIZE):
    pb = PB_TEMPLATE.format(query=urllib.parse.quote(query), count=count, offset=offset)
    return (f"{ENDPOINT}?tbm=map&authuser=0&hl={hl}&gl={gl}"
            f"&q={urllib.parse.quote(query)}&pb={urllib.parse.quote(pb, safe='!*')}")


def fetch_search_page(session, query, offset=0, hl="en", gl="us", timeout=30,
                      retries=5, proxy_pool=None):
    """Fetch one page of search results. Returns a list of place dicts."""
    url = build_search_url(query, offset=offset, hl=hl, gl=gl)
    last_err = None
    rate_limited = False
    for attempt in range(retries + 1):
        if attempt:
            if rate_limited and proxy_pool:
                proxy_pool.rotate()
                time.sleep(1)
            elif rate_limited:
                time.sleep(min(10 * 2 ** (attempt - 1), 120))
            else:
                time.sleep(min(2 ** attempt, 15))
        try:
            resp = session.get(url, timeout=timeout,
                               proxies=proxy_pool.current() if proxy_pool else None)
            if resp.status_code in (429, 500, 502, 503):
                rate_limited = resp.status_code == 429
                last_err = RuntimeError(f"HTTP {resp.status_code} from Google")
                continue
            resp.raise_for_status()
            body = resp.text
            if ")]}'" in body[:20]:  # XSSI protection prefix
                body = body.split(")]}'", 1)[1]
            data = json.loads(body)
            entries = data[0][1] if (isinstance(data, list) and data
                                     and isinstance(data[0], list)
                                     and len(data[0]) > 1) else None
            if not isinstance(entries, list):
                return []
            places = []
            for entry in entries:
                if not (isinstance(entry, list) and len(entry) > 14
                        and isinstance(entry[14], list)):
                    continue  # entry [0] is metadata, not a place
                m = PLACE_ID_RE.search(json.dumps(entry))
                place = parse_place_node(entry[14], place_id=m.group(1) if m else None)
                if place and place["feature_id"]:
                    places.append(place)
            return places
        except (requests.RequestException, json.JSONDecodeError, IndexError, TypeError) as e:
            last_err = e
    raise RuntimeError(f"Failed to fetch search page after {retries + 1} attempts: {last_err}")


def search_places(query, max_places=None, hl="en", gl="us", delay=0.3,
                  session=None, proxy_pool=None, on_progress=None):
    """All places for one query, paged until Google's stream runs out.

    Dedupes by feature id (adjacent pages overlap by a result or two).
    """
    session = session or make_search_session()
    places, seen = [], set()
    offset = 0
    while True:
        if proxy_pool:
            proxy_pool.rotate()
        page = fetch_search_page(session, query, offset=offset, hl=hl, gl=gl,
                                 proxy_pool=proxy_pool)
        if not page:
            break
        new = 0
        for place in page:
            if place["feature_id"] in seen:
                continue
            seen.add(place["feature_id"])
            places.append(place)
            new += 1
            if max_places and len(places) >= max_places:
                break
        if on_progress:
            on_progress(query, len(places))
        if max_places and len(places) >= max_places:
            break
        if new == 0:
            break  # wrapped around / stuck cursor
        offset += PAGE_SIZE
        if delay:
            time.sleep(delay)
    return places


def _enrich(place, hl, gl, proxy_pool):
    """Fill total_reviews / reviews_distribution via the details endpoint."""
    try:
        full = fetch_place_details(place["feature_id"], hl=hl, gl=gl,
                                   proxy_pool=proxy_pool)
    except Exception:
        full = None
    if not full:
        return place
    # Details wins where it has a value; the search result fills the gaps.
    merged = {k: (full.get(k) if full.get(k) is not None else place.get(k))
              for k in place}
    # Exception: search results carry the full week of opening hours, the
    # details endpoint usually just the current day -- keep the richer one.
    search_hours, details_hours = place.get("opening_hours"), full.get("opening_hours")
    if search_hours and len(search_hours) > len(details_hours or {}):
        merged["opening_hours"] = search_hours
    merged["search_categories"] = place.get("search_categories")
    return merged


def find_places(city, categories, max_places=None, hl="en", gl="us", delay=0.3,
                details=True, workers=4, proxies=None, on_progress=None):
    """Find all places for a city across one or more categories.

    city        -- e.g. "Berlin" or "Berlin, Germany"
    categories  -- iterable of category strings, e.g. ["restaurant", "cafe"];
                   each becomes its own "<category> in <city>" query (Google
                   caps a single query at ~200-300 results, so more/narrower
                   categories = better coverage)
    max_places  -- stop after this many unique places (None = all)
    details     -- also fetch full details per place (total_reviews and the
                   per-star histogram are NOT in search results); runs on a
                   thread pool of `workers`
    proxies     -- optional list of proxy URLs, rotated per page / on 429
    on_progress -- optional callback(stage, label, count)
                   stage is "search" or "details"
    """
    proxy_pool = ProxyPool(proxies) or None
    session = make_search_session()

    places, by_fid = [], {}
    for category in categories:
        query = f"{category} in {city}"
        # Cap paging at what is still needed; cross-category duplicates in the
        # result are dropped below, which can leave a capped run slightly
        # under max -- the next category then tops it up.
        found = search_places(
            query, max_places=(max_places - len(places)) if max_places else None,
            hl=hl, gl=gl, delay=delay, session=session,
            proxy_pool=proxy_pool,
            on_progress=(lambda q, n: on_progress("search", q, n)) if on_progress else None,
        )
        for place in found:
            fid = place["feature_id"]
            if fid in by_fid:
                by_fid[fid]["search_categories"].append(category)
                continue
            place["search_categories"] = [category]
            by_fid[fid] = place
            places.append(place)
        if max_places and len(places) >= max_places:
            places = places[:max_places]
            break

    if details and places:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(_enrich, p, hl, gl, proxy_pool) for p in places]
            for i, fut in enumerate(futures):
                places[i] = fut.result()
                done += 1
                if on_progress:
                    on_progress("details", places[i].get("name") or "", done)

    return {
        "city": city,
        "categories": list(categories),
        "place_count": len(places),
        "places": places,
    }
