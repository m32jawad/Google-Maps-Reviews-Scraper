"""Fetch review pages from Google's internal GetLocalBoqProxy endpoint.

This is the same paginated RPC the Google Maps page calls when you scroll
the reviews panel: ~10 reviews on the first page, ~20 per page after that,
chained by an opaque pagination token. Plain HTTPS GET, no API key, no
browser needed.
"""
import json
import time
import urllib.parse

import requests

from .resolver import DEFAULT_HEADERS

ENDPOINT = (
    "https://www.google.com/httpservice/web/"
    "PrivateLocalSearchUiDataService/GetLocalBoqProxy"
)

SORT_ORDERS = {
    "relevant": 1,
    "newest": 2,
    "highest": 3,
    "lowest": 4,
}

# The endpoint serves at most 60 reviews per request regardless of what you
# ask for; requesting the max cuts request count (and 429 risk) by 3x vs the
# frontend's default of 10/20.
MAX_PAGE_SIZE = 60


class ProxyPool:
    """Round-robin pool of proxy URLs (http://user:pass@host:port or socks5://...)."""

    def __init__(self, proxies=None):
        self._proxies = [p.strip() for p in (proxies or []) if p and p.strip()]
        self._i = 0

    def __bool__(self):
        return bool(self._proxies)

    def current(self):
        if not self._proxies:
            return None
        p = self._proxies[self._i % len(self._proxies)]
        return {"http": p, "https": p}

    def rotate(self):
        self._i += 1


def build_page_url(feature_id, sort=2, token="", hl="en", page_size=MAX_PAGE_SIZE):
    # reqpld mirrors what the Maps frontend sends: the review-list request lives
    # at reqpld[1][9]; slot 1 is the sort order, slot 9 the page size, slot 11
    # the feature id, slot 19 the pagination token (follow-up pages only).
    if not token:
        inner = [None, sort, None, None, None, None, None, None, None, page_size, None, [feature_id]]
    else:
        inner = [None, sort, None, None, None, None, None, None, None, page_size, None, [feature_id],
                 None, None, None, None, None, None, None, token]
    reqpld = [None, [None] * 9 + [inner]]
    payload = json.dumps(reqpld, separators=(",", ":"))
    return f"{ENDPOINT}?msc=gwsrpc&hl={hl}&reqpld={urllib.parse.quote(payload, safe='')}"


def fetch_page(session, feature_id, sort=2, token="", hl="en", timeout=30, retries=5,
               proxy_pool=None):
    """Fetch one page. Returns (raw_reviews_list, next_token)."""
    url = build_page_url(feature_id, sort=sort, token=token, hl=hl)
    last_err = None
    rate_limited = False
    for attempt in range(retries + 1):
        if attempt:
            if rate_limited and proxy_pool:
                # A 429 is per-IP: switching proxy sidesteps the cool-down.
                proxy_pool.rotate()
                time.sleep(1)
            elif rate_limited:
                # Sustained scraping trips Google's per-IP throttle; a 429
                # needs a long cool-down, transient 5xx only a short one.
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
            if ")]}'" in body:  # XSSI protection prefix
                body = body.split(")]}'", 1)[1]
            data = json.loads(body)
            node = data[1][10] if len(data) > 1 and isinstance(data[1], list) and len(data[1]) > 10 else None
            if not node:
                return [], ""
            reviews = node[2] if len(node) > 2 and isinstance(node[2], list) else []
            next_token = node[6] if len(node) > 6 and isinstance(node[6], str) else ""
            return reviews, next_token
        except (requests.RequestException, json.JSONDecodeError, IndexError, TypeError) as e:
            last_err = e
    raise RuntimeError(f"Failed to fetch review page after {retries + 1} attempts: {last_err}")


def make_review_session():
    s = requests.Session()
    s.headers.update({**DEFAULT_HEADERS, "Accept": "*/*"})
    return s
