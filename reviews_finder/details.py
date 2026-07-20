"""Fetch business details for a place from Google's /maps/preview/place RPC.

Returns name, address (full + components), phone, website, coordinates,
categories, opening hours, place id, and -- most importantly for rating
maths -- Google's own overall `rating` and `total_reviews` across ALL reviews,
not just the ones we scrape.

Two quirks of this endpoint, both found by testing it live:

* `total_reviews` only comes back in a "rich" response variant that Google
  serves non-deterministically, and only to a session that has first loaded
  google.com/maps (it needs the __Secure-ENID cookie). `fetch_place_details`
  therefore warms a session and retries until the count appears.
* The per-star histogram is not returned at all. Older scrapers read it at
  [175][3]; that slot is now permanently null, and Google's own Places API
  never exposed it either. Use `scraper.rating_distribution` (computed from
  scraped reviews, with a `coverage` figure) for the breakdown.

Google stores no email address for a business, so that field cannot be
scraped from Maps at all -- follow `website` to find one.
"""
import json
import re
import urllib.parse

import requests

from .fetcher import make_review_session

ENDPOINT = "https://www.google.com/maps/preview/place"

# The complete field mask the Maps frontend itself sends, captured from a real
# browser session (only !1s<feature id> is substituted). Shorter hand-built
# masks return a cut-down place node with no review count and no histogram --
# the two fields that matter most here -- so keep this verbatim.
PB_TEMPLATE = (
    "!1m14!1s{fid}!3m12!1m3!1d424769.64968674467!2d72.8236032!3d33.7215488!2m3!1f0.0!2f0.0"
    "!3f0.0!3m2!1i1024!2i768!4f13.1!12m4!2m3!1i360!2i120!4i8!13m57!2m2!1i203!2i100!3m2!2i4"
    "!5b1!6m6!1m2!1i86!2i86!1m2!1i408!2i240!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2"
    "!2b0!3e3!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2!1m3!1e10!2b0!3e4!1m3!1e9"
    "!2b1!3e2!2b1!9b0!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20!14m3!1sAN9dapnXCs-0kdUPqJz8kAw"
    "!7e81!15i10112!15m108!1m26!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1!18m15!3b1!4b1"
    "!5b1!6b1!13b1!14b1!17b1!21b1!22b1!30b1!32b1!33m1!1b1!34b1!36e2!10m1!8e3!11m1!3e1!17b1"
    "!20m2!1e3!1e6!24b1!25b1!26b1!27b1!29b1!30m1!2b1!36b1!37b1!39m3!2m2!2i1!3i1!43b1!52b1"
    "!54m1!1b1!55b1!56m1!1b1!61m2!1m1!1e1!65m5!3m4!1m3!1m2!1i224!2i298!72m22!1m8!2b1!5b1!7b1"
    "!12m4!1b1!2b1!4m1!1e1!4b1!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4!3sother_user_google_review_posts"
    "__and__hotel_and_vr_partner_review_posts!6m1!1e1!9b1!89b1!90m2!1m1!1e2!98m3!1b1!2b1!3b1"
    "!103b1!113b1!114m3!1b1!2m1!1b1!117b1!122m1!1b1!126b1!127b1!128m1!1b0!21m0!22m1!1e81"
    "!30m8!3b1!6m2!1b1!2b1!7m2!1e3!2b1!9b1!34m5!7b1!10b1!14b1!15m1!1b0!37i786"
)

# [6][175][3] -> [1-star, 2-star, 3-star, 4-star, 5-star] counts
STARS = (1, 2, 3, 4, 5)

PLACE_ID_RE = re.compile(r'"(ChIJ[0-9A-Za-z_-]{10,})"')

MAPS_HOME = "https://www.google.com/maps?hl=en"
SOCS_COOKIE = "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmRlIAEaBgiA_LyaBg"


def make_details_session(proxy_pool=None):
    """A session warmed on google.com/maps -- required for total_reviews."""
    s = make_review_session()
    s.cookies.set("SOCS", SOCS_COOKIE, domain=".google.com")
    try:
        s.get(MAPS_HOME, timeout=30,
              proxies=proxy_pool.current() if proxy_pool else None)
    except requests.RequestException:
        pass  # warm-up is best effort; details still parse without it
    s.headers.update({"Accept": "*/*", "Referer": "https://www.google.com/maps/"})
    return s


def _get(arr, *path):
    cur = arr
    for p in path:
        if not isinstance(cur, (list, tuple)) or p >= len(cur):
            return None
        cur = cur[p]
    return cur


def _str(value):
    return value if isinstance(value, str) and value else None


def _parse_hours(node):
    """[203][0] -> [["Monday", ..., [["Open 24 hours", ...]], ...], ...]"""
    days = _get(node, 203, 0)
    if not isinstance(days, list):
        return None
    hours = {}
    for day in days:
        name = _get(day, 0)
        slots = _get(day, 3)
        if not isinstance(name, str) or not isinstance(slots, list):
            continue
        texts = [_get(s, 0) for s in slots if isinstance(_get(s, 0), str)]
        if texts:
            hours[name] = texts
    return hours or None


def _parse_distribution(node):
    """Google's own per-star counts, at [175][3] -- the real histogram."""
    raw = _get(node, 175, 3)
    if not isinstance(raw, list) or len(raw) != 5:
        return None
    if not all(isinstance(x, int) for x in raw):
        return None
    counts = dict(zip(STARS, raw))
    total = sum(raw)
    return {
        "counts": counts,
        "total": total,
        "average": round(sum(s * c for s, c in counts.items()) / total, 2) if total else None,
    }


def _fetch_once(session, feature_id, hl, gl, timeout, proxy_pool):
    url = (f"{ENDPOINT}?authuser=0&hl={hl}&gl={gl}"
           f"&pb={urllib.parse.quote(PB_TEMPLATE.format(fid=feature_id), safe='')}")
    resp = session.get(url, timeout=timeout,
                       proxies=proxy_pool.current() if proxy_pool else None)
    resp.raise_for_status()
    body = resp.text
    if ")]}'" in body:
        body = body.split(")]}'", 1)[1]
    return json.loads(body)


def fetch_place_details(feature_id, hl="en", gl="us", session=None, timeout=30,
                        proxy_pool=None, count_attempts=6):
    """Return a dict of business details for a feature id (0x..:0x..).

    Retries up to `count_attempts` times to land the response variant that
    carries total_reviews; the last response is used either way, so details
    are still returned (with total_reviews=None) if the count never appears.
    """
    data = best = None
    for attempt in range(max(1, count_attempts)):
        # A fresh warmed session per attempt -- the rich variant correlates
        # with a newly warmed session, not with retrying on the same one.
        s = session if (session is not None and attempt == 0) else make_details_session(proxy_pool)
        try:
            data = _fetch_once(s, feature_id, hl, gl, timeout, proxy_pool)
        except (requests.RequestException, ValueError):
            continue
        best = best or data
        if _get(data, 6, 175, 3) and _get(data, 6, 4, 8):
            best = data
            break
    data = best

    if data is None:
        return None
    node = _get(data, 6)
    if not isinstance(node, list):
        return None

    street, locality, country = (list(_get(node, 2) or []) + [None, None, None])[:3]
    place_id_match = PLACE_ID_RE.search(json.dumps(data))

    return {
        "name": _str(_get(node, 11)),
        "place_id": place_id_match.group(1) if place_id_match else None,
        "feature_id": _str(_get(node, 10)) or feature_id,
        "address": _str(_get(node, 39)),
        "address_components": {
            "street": _str(street),
            "locality": _str(locality),
            "country": _str(country),
        },
        "phone": _str(_get(node, 178, 0, 0)),
        "phone_e164": _str(_get(node, 178, 0, 3)),
        "website": _str(_get(node, 7, 0)),
        "website_domain": _str(_get(node, 7, 1)),
        "categories": [c for c in (_get(node, 13) or []) if isinstance(c, str)],
        "latitude": _get(node, 9, 2),
        "longitude": _get(node, 9, 3),
        "timezone": _str(_get(node, 30)),
        # Google's own figures across ALL reviews -- the basis for rating maths.
        "rating": _get(node, 4, 7),
        "total_reviews": _get(node, 4, 8),
        "reviews_distribution": _parse_distribution(node),
        "reviews_url": _str(_get(node, 4, 3, 0)),
        "opening_hours": _parse_hours(node),
        "maps_url": f"https://www.google.com/maps/place/?q=place_id:{place_id_match.group(1)}"
                    if place_id_match else None,
    }
