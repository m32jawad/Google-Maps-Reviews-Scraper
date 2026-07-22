"""CLI: check whether Google reviews still exist, and print their details.

Usage:
  python check.py "<review url>" [--text "known review text"] [options]
  python check.py --file reviews.json            # scrape output, or a JSON list
  python check.py <review id> --place <place url | ChIJ.. | 0x..:0x..>

Passing the review's known text (or a --file of previously scraped reviews)
turns most checks into a single request; without it the place is paged through.
"""
import argparse
import json
import sys

from reviews_finder.review_status import check_reviews


def load_file(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    place_fid = None
    if isinstance(data, dict):
        place_fid = data.get("feature_id")  # scrape output keeps it top-level
        data = data.get("reviews") or []
    items = []
    for r in data:
        if isinstance(r, str):
            items.append(r)
        elif isinstance(r, dict):
            items.append({"review_id": r.get("review_id"),
                          "feature_id": r.get("feature_id") or place_fid,
                          "review_url": r.get("review_url"),
                          "text": r.get("text")})
    return items


def main():
    ap = argparse.ArgumentParser(description="Check if Google reviews still exist.")
    ap.add_argument("review", nargs="*", help="Review permalink URL or review id")
    ap.add_argument("--file", help="JSON file of reviews (scrape output or a list)")
    ap.add_argument("--place", help="Place URL / ChIJ id / feature id, if the reviews are bare ids")
    ap.add_argument("--text", help="Known review text (single review only), for the fast path")
    ap.add_argument("--sort", choices=["relevant", "newest", "highest", "lowest"], default="newest")
    ap.add_argument("--quick", action="store_true",
                    help="Text search only: report 'unknown' instead of paging the place")
    ap.add_argument("--max-scan-pages", type=int, default=None,
                    help="Cap the fallback scan (60 reviews per page)")
    ap.add_argument("--delay", type=float, default=0.3, help="Seconds between requests")
    ap.add_argument("--proxy", action="append", default=[], help="Proxy URL (repeatable)")
    ap.add_argument("--out", help="Write results to this JSON file")
    args = ap.parse_args()

    items = list(args.review)
    if args.text and len(items) == 1:
        items = [{"review_id": items[0], "text": args.text}]
    if args.file:
        items += load_file(args.file)
    if not items:
        ap.error("give a review URL/id, or --file")

    def progress(i, result):
        review = result.get("review") or {}
        detail = f" | {review.get('rating')}* {review.get('author', {}).get('name')}" if review else ""
        if result.get("error"):
            detail += f" | {result['error']}"
        print(f"  [{i + 1}/{len(items)}] {result['status']}{detail}", file=sys.stderr)

    results = check_reviews(
        items, feature_id=args.place, sort=args.sort, quick=args.quick,
        max_scan_pages=args.max_scan_pages, delay=args.delay,
        proxies=args.proxy or None, on_result=progress,
    )

    payload = {
        "checked": len(results),
        "exists": sum(1 for r in results if r["status"] == "exists"),
        "deleted": sum(1 for r in results if r["status"] == "deleted"),
        "unknown": sum(1 for r in results if r["status"] == "unknown"),
        "results": results,
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n{payload['exists']} exist, {payload['deleted']} deleted, "
              f"{payload['unknown']} unknown -> {args.out}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
