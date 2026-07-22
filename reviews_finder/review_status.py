"""Check whether a single Google review still exists, and return its details.

Everything here goes through the same plain-HTTPS GetLocalBoqProxy endpoint the
review scraper uses -- no browser, no API key.

Two lookup routes, cheapest first:

* **search** -- the review-list request has a free-text filter slot (the
  "Search reviews" box in Maps). Querying a distinctive phrase from the
  review's own text usually returns that one review in a single request.
  It matches review *text* only: author names don't match, and a rating-only
  review can never be found this way.
* **scan** -- page through the place's reviews looking for the review id.
  Always correct, but costs one request per 60 reviews. Used when no text is
  known, and always before declaring a review deleted, since a search miss on
  its own is not proof (very short or very common review text ranks below the
  page of results Google returns).

A review URL is enough input on its own: it carries both the review id and the
place's feature id in the short `0x0:0x...` form, which the endpoint accepts.
"""
import re
import time
import urllib.parse

from .fetcher import ProxyPool, SORT_ORDERS, fetch_page, make_review_session
from .parser import parse_review
from .resolver import resolve_feature_id

# In a review permalink's data blob: !1s<review id>!2m1!1s<feature id>
REVIEW_ID_RE = re.compile(r"!1s(C[0-9A-Za-z_\-]{20,}=*)")
URL_FEATURE_ID_RE = re.compile(r"!2m1!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
# Review URLs carry the place as 0x0:0x<id>, so the leading half can be a
# single zero -- looser than the resolver's full-feature-id pattern.
ANY_FEATURE_ID_RE = re.compile(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+")
# A bare review id, as stored in scrape output ("Ci9DQUlRQUNvZ...").
BARE_REVIEW_ID_RE = re.compile(r"^C[0-9A-Za-z_\-]{20,}={0,2}$")

# Words of the review's own text used as the search query. Long enough to be
# distinctive, short enough that a truncated stored copy still matches.
QUERY_WORDS = 10
MIN_QUERY_WORDS = 2

# Search hits come back with every matched word wrapped in <b>...</b>.
HIGHLIGHT_RE = re.compile(r"</?b>")

EXISTS, DELETED, UNKNOWN = "exists", "deleted", "unknown"


def parse_review_url(url):
    """Pull the review id and place feature id out of a review permalink.

    Returns {"review_id", "feature_id"}; either value may be None. Also accepts
    a bare review id (feature id then comes back None).
    """
    url = (url or "").strip()
    if BARE_REVIEW_ID_RE.match(url):
        return {"review_id": url, "feature_id": None}

    decoded = urllib.parse.unquote(url)
    review_match = REVIEW_ID_RE.search(decoded)
    feature_match = URL_FEATURE_ID_RE.search(decoded) or ANY_FEATURE_ID_RE.search(decoded)
    return {
        "review_id": review_match.group(1) if review_match else None,
        "feature_id": feature_match.group(1) if feature_match else None,
    }


def build_query(text, words=QUERY_WORDS):
    """A search query from a review's known text, or None if it is too short."""
    if not isinstance(text, str):
        return None
    # Stored text can carry the <br> markup Google puts in truncated copies.
    cleaned = re.sub(r"<br\s*/?>", " ", text)
    tokens = [w for w in cleaned.split() if w]
    if len(tokens) < MIN_QUERY_WORDS:
        return None
    return " ".join(tokens[:words])


def _unhighlight(parsed):
    """Undo the <b> markup a search hit carries, so text matches a plain scrape."""
    if isinstance(parsed.get("text"), str):
        parsed["text"] = HIGHLIGHT_RE.sub("", parsed["text"])
    owner = parsed.get("owner_response")
    if isinstance(owner, dict) and isinstance(owner.get("text"), str):
        owner["text"] = HIGHLIGHT_RE.sub("", owner["text"])
    return parsed


def _resolve_target(review, feature_id=None):
    """Normalize (review ref, optional place ref) into (review_id, feature_id)."""
    if isinstance(review, str):
        parsed = parse_review_url(review)
    else:
        parsed = {"review_id": review.get("review_id"),
                  "feature_id": review.get("feature_id")}
        if not all(parsed.values()) and review.get("review_url"):
            # Scrape output keeps the place at the top level, not on each
            # review -- the permalink carries it, so fill the gaps from there.
            from_url = parse_review_url(review["review_url"])
            parsed = {k: v or from_url.get(k) for k, v in parsed.items()}
    review_id = parsed.get("review_id")
    if not review_id:
        raise ValueError(f"Could not find a review id in {review!r}")

    fid = feature_id or parsed.get("feature_id")
    if fid and not ANY_FEATURE_ID_RE.fullmatch(fid):
        # A place URL / ChIJ id / goo.gl link rather than a raw feature id.
        fid, _ = resolve_feature_id(fid)
    if not fid:
        raise ValueError(
            "No place could be determined for this review. Pass feature_id "
            "(or a place URL) alongside a bare review id.")
    return review_id, fid


def _result(review_id, feature_id, status, method, review=None, pages=0, error=None):
    return {
        "review_id": review_id,
        "feature_id": feature_id,
        "status": status,
        "exists": status == EXISTS,
        "method": method,
        "pages_fetched": pages,
        "review": review,
        "error": error,
    }


def _pages(session, feature_id, sort, hl, proxy_pool, query=None, max_pages=None,
           delay=0.0):
    """Yield parsed reviews page by page; stops on a dead cursor or an error."""
    token, page, seen_token = "", 0, set()
    while max_pages is None or page < max_pages:
        page += 1
        if proxy_pool:
            proxy_pool.rotate()
        raw_reviews, next_token = fetch_page(
            session, feature_id, sort=sort, token=token, hl=hl,
            proxy_pool=proxy_pool or None, query=query)
        yield page, [p for p in (parse_review(r) for r in raw_reviews) if p]
        if not raw_reviews or not next_token or next_token in seen_token:
            return
        seen_token.add(next_token)
        token = next_token
        if delay:
            time.sleep(delay)


def check_review(review, feature_id=None, text=None, hl="en", sort="newest",
                 proxies=None, session=None, quick=False, max_scan_pages=None,
                 search_pages=2, delay=0.3):
    """Is this review still live? Returns a status dict.

    review          -- review permalink URL, a bare review id, or a dict with
                       "review_id" (and optionally "feature_id" / "text")
    feature_id      -- the review's place; only needed when `review` is a bare
                       id. Accepts a feature id, place URL or ChIJ place id
    text            -- the review's text from a previous scrape. With it, a live
                       review is usually confirmed in one request
    quick           -- skip the confirming scan: a search miss returns "unknown"
                       instead of paging the whole place
    max_scan_pages  -- cap the scan (60 reviews per page). Hitting the cap
                       without a match returns "unknown", never "deleted"
    search_pages    -- pages of search results to look through before falling
                       back to the scan

    status is "exists" (with the review's current details under "review"),
    "deleted" (scanned the place in full, review not there), or "unknown"
    (the check could not reach a verdict -- never treat this as a deletion).
    """
    if isinstance(review, dict):
        text = text if text is not None else review.get("text")
    review_id, fid = _resolve_target(review, feature_id)

    if sort not in SORT_ORDERS:
        raise ValueError(f"sort must be one of {', '.join(SORT_ORDERS)}")
    sort_code = SORT_ORDERS[sort]
    session = session or make_review_session()
    proxy_pool = ProxyPool(proxies)
    pages = 0

    query = build_query(text)
    if query:
        try:
            for pages, batch in _pages(session, fid, sort_code, hl, proxy_pool,
                                       query=query, max_pages=search_pages, delay=delay):
                for parsed in batch:
                    if parsed["review_id"] == review_id:
                        return _result(review_id, fid, EXISTS, "search",
                                       _unhighlight(parsed), pages)
        except RuntimeError as e:
            if quick:
                return _result(review_id, fid, UNKNOWN, "search", pages=pages, error=str(e))

    if quick:
        return _result(review_id, fid, UNKNOWN, "search", pages=pages,
                       error=None if query else "no review text to search with")

    scanned = 0
    try:
        for scanned, batch in _pages(session, fid, sort_code, hl, proxy_pool,
                                     max_pages=max_scan_pages, delay=delay):
            for parsed in batch:
                if parsed["review_id"] == review_id:
                    return _result(review_id, fid, EXISTS, "scan", parsed, pages + scanned)
    except RuntimeError as e:
        return _result(review_id, fid, UNKNOWN, "scan", pages=pages + scanned, error=str(e))

    if not scanned:
        return _result(review_id, fid, UNKNOWN, "scan", pages=pages,
                       error="place returned no reviews at all")
    if max_scan_pages and scanned >= max_scan_pages:
        return _result(review_id, fid, UNKNOWN, "scan", pages=pages + scanned,
                       error=f"scan stopped at the {max_scan_pages}-page cap")
    return _result(review_id, fid, DELETED, "scan", pages=pages + scanned)


def check_reviews(reviews, feature_id=None, hl="en", sort="newest", proxies=None,
                  session=None, quick=False, max_scan_pages=None, search_pages=2,
                  delay=0.3, on_result=None):
    """Check several reviews, sharing one scan across those at the same place.

    reviews -- review URLs, bare ids, or dicts with "review_id"/"feature_id"/"text".
    Results come back in input order. Reviews whose text search finds them cost
    one request each; whatever is left is resolved by a single pass over each
    place, instead of one pass per review.
    """
    session = session or make_review_session()
    proxy_pool = ProxyPool(proxies)
    sort_code = SORT_ORDERS[sort]
    results = [None] * len(reviews)
    pending = {}  # feature_id -> [(index, review_id, pages_so_far)]

    for i, review in enumerate(reviews):
        try:
            r = check_review(review, feature_id=feature_id, hl=hl, sort=sort,
                             session=session, proxies=proxies, quick=True,
                             search_pages=search_pages, delay=delay)
        except ValueError as e:
            results[i] = _result(None, None, UNKNOWN, "search", error=str(e))
            if on_result:
                on_result(i, results[i])
            continue
        if r["status"] == EXISTS or quick:
            results[i] = r
            if on_result:
                on_result(i, r)
        else:
            pending.setdefault(r["feature_id"], []).append((i, r["review_id"], r["pages_fetched"]))

    for fid, items in pending.items():
        wanted = {review_id: (i, pages) for i, review_id, pages in items}
        scanned = 0
        error = None
        try:
            for scanned, batch in _pages(session, fid, sort_code, hl, proxy_pool,
                                         max_pages=max_scan_pages, delay=delay):
                for parsed in batch:
                    hit = wanted.pop(parsed["review_id"], None)
                    if hit:
                        i, pages = hit
                        results[i] = _result(parsed["review_id"], fid, EXISTS, "scan",
                                             parsed, pages + scanned)
                        if on_result:
                            on_result(i, results[i])
                if not wanted:
                    break
        except RuntimeError as e:
            error = str(e)

        if error:
            status, note = UNKNOWN, error
        elif not scanned:
            status, note = UNKNOWN, "place returned no reviews at all"
        elif max_scan_pages and scanned >= max_scan_pages:
            status, note = UNKNOWN, f"scan stopped at the {max_scan_pages}-page cap"
        else:
            status, note = DELETED, None
        for review_id, (i, pages) in wanted.items():
            results[i] = _result(review_id, fid, status, "scan", pages=pages + scanned,
                                 error=note)
            if on_result:
                on_result(i, results[i])

    return results
