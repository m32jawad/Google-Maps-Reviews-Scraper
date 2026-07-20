"""Turn a raw review array from GetLocalBoqProxy into a clean dict.

Observed layout (element index -> content). Positions have been stable in
testing, but everything below is parsed defensively with type checks and a
scanning fallback for the review text, because Google reshuffles these
arrays from time to time:

  [1]  rating (int 1-5)
  [2]  [relative_time, ?, ms_timestamp_str]
  [3]  [author_name, avatar_url, contrib_url, ...]
  [4]  owner response: [None, relative_time, text, ...] or None
  [5]  review id
  [12] canonical review URL
  [14] photos: [[url, ...], ...]
  [26] language code ("en")
  [27] full review text
  [28] truncated review text (may contain <br> markup)
"""
import datetime
import re


def _get(arr, *path):
    cur = arr
    for p in path:
        if not isinstance(cur, (list, tuple)) or p >= len(cur):
            return None
        cur = cur[p]
    return cur


def _ms_to_iso(ms):
    try:
        dt = datetime.datetime.fromtimestamp(int(ms) / 1000, tz=datetime.timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def _find_text_fallback(review):
    """If the fixed text slots move, scan for the language-code + text pair."""
    for i, el in enumerate(review):
        if (
            isinstance(el, str)
            and re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", el)
            and isinstance(_get(review, i + 1), str)
        ):
            return el, review[i + 1]
    # last resort: longest non-URL string beyond the fixed header fields
    candidates = [
        el for i, el in enumerate(review)
        if i > 5 and isinstance(el, str) and len(el) > 5
        and not el.startswith("http") and "//" not in el[:10]
        and not re.fullmatch(r"[0-9A-Za-z_-]{30,}", el)
    ]
    return None, max(candidates, key=len) if candidates else None


def _parse_images(review):
    images = []
    for el in review[6:]:
        if not isinstance(el, list):
            continue
        for item in el:
            url = _get(item, 0)
            if isinstance(url, str) and "googleusercontent" in url:
                images.append(url)
        if images:
            break
    return images


def parse_review(review):
    if not isinstance(review, list) or len(review) < 6:
        return None

    review_id = _get(review, 5)
    if not isinstance(review_id, str):
        return None

    rating = _get(review, 1)
    rating = rating if isinstance(rating, (int, float)) else None

    published_relative = _get(review, 2, 0)
    published_at = _ms_to_iso(_get(review, 2, 2))

    author_name = _get(review, 3, 0)
    author_avatar = _get(review, 3, 1)
    author_url = _get(review, 3, 2)
    author_id = None
    if isinstance(author_url, str):
        m = re.search(r"/contrib/(\d+)", author_url)
        author_id = m.group(1) if m else None

    language = _get(review, 26)
    text = _get(review, 27)
    text_truncated = _get(review, 28)
    if not isinstance(text, str):
        text = None
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", language or ""):
        language = None
    if text is None and isinstance(text_truncated, str):
        text = text_truncated
    if text is None:
        language_fb, text_fb = _find_text_fallback(review)
        language = language or language_fb
        text = text_fb

    owner = _get(review, 4)
    owner_response = None
    if isinstance(owner, list):
        owner_text = _get(owner, 2)
        if isinstance(owner_text, str) and owner_text:
            owner_response = {
                "text": owner_text,
                "published_relative": _get(owner, 1),
            }

    review_url = _get(review, 12)

    return {
        "review_id": review_id,
        "rating": rating,
        "text": text,
        "language": language,
        "published_at": published_at,
        "published_relative": published_relative,
        "author": {
            "id": author_id,
            "name": author_name if isinstance(author_name, str) else None,
            "avatar_url": author_avatar if isinstance(author_avatar, str) else None,
            "profile_url": author_url if isinstance(author_url, str) else None,
        },
        "owner_response": owner_response,
        "images": _parse_images(review),
        "review_url": review_url if isinstance(review_url, str) else None,
    }
