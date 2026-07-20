"""Resolve any Google Maps place reference to the internal feature id (0x..:0x..).

Accepted inputs:
  - Full place URL:      https://www.google.com/maps/place/Name/@.../data=...!1s0x...:0x...!...
  - Search URL:          https://www.google.com/maps/search/?api=1&query=...&query_place_id=ChIJ...
  - Short link:          https://maps.app.goo.gl/xxxx
  - Bare place id:       ChIJ3y2MDjnEyIARGwx3B-xtpvM
  - Bare feature id:     0x80c8c4390e8c2ddf:0xf3a66dec07770c1b
"""
import re
import urllib.parse

import requests

FEATURE_ID_RE = re.compile(r"0x[0-9a-fA-F]{6,}:0x[0-9a-fA-F]+")
# In place-URL data blobs the target place's feature id is tagged !1s; other
# tags (!5s = context/containing feature) must not be mistaken for it.
PRIMARY_FEATURE_ID_RE = re.compile(r"!1s(0x[0-9a-fA-F]{6,}:0x[0-9a-fA-F]+)")
PLACE_ID_RE = re.compile(r"^ChIJ[0-9A-Za-z_-]+$")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Cookies that skip the EU cookie-consent interstitial, which otherwise
# replaces the page body (and hides the feature id) for EU egress IPs.
CONSENT_COOKIES = {
    "CONSENT": "PENDING+987",
    "SOCS": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmRlIAEaBgiA_LyaBg",
}


def _make_session(session=None):
    if session is not None:
        return session
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    for name, value in CONSENT_COOKIES.items():
        s.cookies.set(name, value, domain=".google.com")
    return s


def _extract_place_name(html):
    m = re.search(r"<title>(.*?)(?: - Google Maps)?</title>", html, re.S)
    if m:
        title = m.group(1).strip()
        if title and title not in ("Google Maps", "Google Maps"):
            return title
    m = re.search(r'<meta content="([^"]+)" itemprop="name"', html)
    if m:
        name = m.group(1).split("·")[0].strip()
        if name and name != "Google Maps":
            return name
    return None


def resolve_feature_id(place, session=None, timeout=30):
    """Return (feature_id, place_name_or_None) for any supported place reference."""
    place = place.strip()

    # Already a feature id, or a URL that embeds one — no network needed.
    fid = _pick_feature_id(urllib.parse.unquote(place))
    if fid:
        return fid, _place_name_from_url(place)

    if PLACE_ID_RE.match(place):
        place = (
            "https://www.google.com/maps/search/?api=1&query=place&query_place_id="
            + urllib.parse.quote(place)
        )

    if not place.startswith("http"):
        raise ValueError(f"Unrecognized place reference: {place!r}")

    s = _make_session(session)
    resp = s.get(place, allow_redirects=True, timeout=timeout)
    resp.raise_for_status()

    # Short links may redirect to a URL that already carries the feature id.
    fid = _pick_feature_id(urllib.parse.unquote(resp.url)) or _pick_feature_id(resp.text)
    if not fid:
        raise ValueError(
            "Could not find a feature id (0x..:0x..) for this place. "
            "Check that the URL points to a single place (not a search with many results)."
        )
    name = _extract_place_name(resp.text) or _place_name_from_url(resp.url)
    if not name:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(resp.url).query).get("query")
        if query and query[0] != "place":
            name = query[0]
    return fid, name


def _pick_feature_id(text):
    """Prefer the !1s-tagged (target place) feature id over any other match."""
    m = PRIMARY_FEATURE_ID_RE.search(text)
    if m:
        return m.group(1)
    m = FEATURE_ID_RE.search(text)
    return m.group(0) if m else None


def _place_name_from_url(url):
    m = re.search(r"/maps/place/([^/@?#]+)", urllib.parse.unquote(url))
    if m:
        return m.group(1).replace("+", " ").strip() or None
    return None
