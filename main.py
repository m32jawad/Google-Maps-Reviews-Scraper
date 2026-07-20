"""CLI: fetch all Google reviews for a place.

Usage:
  python main.py "<place url | ChIJ place id | 0x..:0x.. feature id>" [options]

Examples:
  python main.py "https://www.google.com/maps/search/?api=1&query=Hilton&query_place_id=ChIJ3y2MDjnEyIARGwx3B-xtpvM"
  python main.py ChIJ3y2MDjnEyIARGwx3B-xtpvM --sort newest --max 200 --out hilton.json
"""
import argparse
import json
import sys
import time

from reviews_finder import scrape_reviews


def main():
    ap = argparse.ArgumentParser(description="Fetch all Google Maps reviews for a place.")
    ap.add_argument("place", help="Place URL, maps.app.goo.gl link, ChIJ.. place id, or 0x..:0x.. feature id")
    ap.add_argument("--sort", choices=["relevant", "newest", "highest", "lowest"], default="newest")
    ap.add_argument("--max", type=int, default=None, help="Maximum reviews to fetch (default: all)")
    ap.add_argument("--out", default="reviews.json", help="Output JSON file (default: reviews.json)")
    ap.add_argument("--hl", default="en", help="Language for relative dates (default: en)")
    ap.add_argument("--delay", type=float, default=0.3, help="Seconds between page requests (default: 0.3; with proxies use 0)")
    ap.add_argument("--raw", action="store_true", help="Include raw response arrays under _raw")
    ap.add_argument("--proxy", action="append", default=[],
                    help="Proxy URL (repeatable), e.g. http://user:pass@host:port")
    # "
    ap.add_argument("--proxy-file", help="File with one proxy URL per line")
    ap.add_argument("--resume", action="store_true",
                    help="Continue an interrupted run from <out>.partial.jsonl / <out>.state.json")
    ap.add_argument("--ratings", help="Keep only these star ratings, comma-separated, e.g. 1,2 "
                                      "(use with --sort newest for a complete set)")
    ap.add_argument("--no-details", action="store_true",
                    help="Skip the business-details lookup (address, phone, website)")
    ap.add_argument("--details-only", action="store_true",
                    help="Fetch only business details, no reviews")
    args = ap.parse_args()

    ratings = None
    if args.ratings:
        ratings = {int(x) for x in args.ratings.split(",") if x.strip()}

    proxies = list(args.proxy)
    if args.proxy_file:
        with open(args.proxy_file, encoding="utf-8") as f:
            proxies += [line.strip() for line in f if line.strip() and not line.startswith("#")]

    started = time.time()

    def progress(count, page):
        print(f"  page {page}: {count} reviews fetched", file=sys.stderr)

    result = scrape_reviews(
        args.place, sort=args.sort, max_reviews=0 if args.details_only else args.max,
        hl=args.hl, delay=args.delay, raw=args.raw, on_progress=progress,
        proxies=proxies or None,
        checkpoint=None if args.details_only else args.out,
        resume=args.resume, ratings=ratings, details=not args.no_details,
    )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - started
    status = "Done" if result["complete"] else f"Stopped early ({result['stopped_reason']})"
    print(f"\n{status}: {result['review_count']} reviews for feature id {result['feature_id']} "
          f"in {elapsed:.1f}s -> {args.out}")
    if not result["complete"]:
        print(f"Progress kept in {args.out}.partial.jsonl - rerun with --resume to continue.")


if __name__ == "__main__":
    main()
