"""Orchestrator: place reference -> all reviews."""
import json
import os
import time

from .fetcher import ProxyPool, SORT_ORDERS, fetch_page, make_review_session
from .parser import parse_review
from .resolver import resolve_feature_id


def _checkpoint_paths(base):
    return base + ".partial.jsonl", base + ".state.json"


def _load_checkpoint(base, feature_id, sort, ratings_key):
    """Load a previous interrupted run's reviews + pagination token, if compatible."""
    partial_path, state_path = _checkpoint_paths(base)
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    if (state.get("feature_id") != feature_id or state.get("sort") != sort
            or state.get("ratings") != ratings_key):
        return None
    reviews, seen_ids = [], set()
    try:
        with open(partial_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get("review_id") and r["review_id"] not in seen_ids:
                    seen_ids.add(r["review_id"])
                    reviews.append(r)
    except (OSError, ValueError):
        return None
    return {
        "token": state.get("next_token", ""),
        "page": state.get("page", 0),
        "reviews": reviews,
        "seen_ids": seen_ids,
    }


def _save_state(state_path, feature_id, sort, ratings_key, next_token, page):
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"feature_id": feature_id, "sort": sort, "ratings": ratings_key,
                   "next_token": next_token, "page": page}, f)
    os.replace(tmp, state_path)


def scrape_reviews(place, sort="newest", max_reviews=None, hl="en",
                   delay=0.3, raw=False, on_progress=None, proxies=None,
                   checkpoint=None, resume=False, ratings=None):
    """Scrape reviews for a Google Maps place.

    place       -- place URL, maps.app.goo.gl link, ChIJ.. place id, or 0x..:0x.. feature id
    sort        -- relevant | newest | highest | lowest
    max_reviews -- stop after this many (None = all)
    hl          -- interface language for relative dates / owner replies
    delay       -- seconds to sleep between pages (with proxies, 0 is fine)
    raw         -- also keep the raw arrays under each review's "_raw" key
    on_progress -- optional callback(fetched_count, page_number)
    proxies     -- optional list of proxy URLs, rotated once per page and on 429
    checkpoint  -- base path for crash-safe progress files: each page is appended
                   to <checkpoint>.partial.jsonl and the pagination token saved to
                   <checkpoint>.state.json; both are removed after a complete run
    resume      -- continue from the checkpoint files of an interrupted run
                   (same place + sort required)
    ratings     -- keep only these star ratings, e.g. {1, 2}. All pages are
                   still traversed (Google can't filter server-side); use
                   sort="newest" with this for a complete low-star set, since
                   rating-sorted views are capped by Google at ~800 reviews
    """
    ratings = set(ratings) if ratings else None
    ratings_key = sorted(ratings) if ratings else None
    if sort not in SORT_ORDERS:
        raise ValueError(f"sort must be one of {', '.join(SORT_ORDERS)}")

    feature_id, place_name = resolve_feature_id(place)
    session = make_review_session()
    proxy_pool = ProxyPool(proxies)

    reviews = []
    seen_ids = set()
    token = ""
    page_num = 0
    stopped_reason = None
    no_new_pages = 0

    partial_path = state_path = partial_file = None
    if checkpoint:
        partial_path, state_path = _checkpoint_paths(checkpoint)
        if resume:
            prev = _load_checkpoint(checkpoint, feature_id, sort, ratings_key)
            if prev:
                reviews, seen_ids = prev["reviews"], prev["seen_ids"]
                token, page_num = prev["token"], prev["page"]
        # fresh (non-resumed) runs start the journal over
        partial_file = open(partial_path, "a" if resume else "w", encoding="utf-8")

    try:
        while True:
            page_num += 1
            if proxy_pool:
                proxy_pool.rotate()  # spread pages across the pool
            try:
                raw_reviews, next_token = fetch_page(
                    session, feature_id, sort=SORT_ORDERS[sort], token=token, hl=hl,
                    proxy_pool=proxy_pool or None,
                )
            except RuntimeError as e:
                # Keep everything fetched so far instead of losing it to one bad page.
                stopped_reason = str(e)
                break

            new_seen = 0
            for raw_review in raw_reviews:
                parsed = parse_review(raw_review)
                if parsed is None or parsed["review_id"] in seen_ids:
                    continue
                seen_ids.add(parsed["review_id"])
                new_seen += 1
                if ratings and parsed["rating"] not in ratings:
                    continue
                if raw:
                    parsed["_raw"] = raw_review
                reviews.append(parsed)
                if partial_file:
                    partial_file.write(json.dumps(parsed, ensure_ascii=False) + "\n")
                if max_reviews and len(reviews) >= max_reviews:
                    break

            if partial_file:
                partial_file.flush()
            if state_path and next_token:
                _save_state(state_path, feature_id, sort, ratings_key, next_token, page_num)

            if on_progress:
                on_progress(len(reviews), page_num)

            if max_reviews and len(reviews) >= max_reviews:
                break
            if not next_token or next_token == token:
                break
            # Loop guard: pages of only already-seen reviews (e.g. right after a
            # resume refetches the last page) are fine once, but several in a row
            # means the cursor is stuck.
            no_new_pages = no_new_pages + 1 if new_seen == 0 else 0
            if not raw_reviews or no_new_pages >= 3:
                break
            token = next_token
            if delay:
                time.sleep(delay)
    finally:
        if partial_file:
            partial_file.close()

    complete = stopped_reason is None
    if checkpoint and complete:
        for path in (partial_path, state_path):
            try:
                os.remove(path)
            except OSError:
                pass

    return {
        "place": place,
        "place_name": place_name,
        "feature_id": feature_id,
        "sort": sort,
        "complete": complete,
        "stopped_reason": stopped_reason,
        "review_count": len(reviews),
        "reviews": reviews,
    }
