"""CLI: find all places in a city for one or more categories (no reviews).

Usage:
  python find.py "<city>" --categories "restaurant,cafe" [options]

Examples:
  python find.py "Berlin" --categories restaurant
  python find.py "Berlin, Germany" --categories "dentist,orthodontist" --out berlin-dentists.json
  python find.py "Munich" --categories restaurant --fast   # skip per-place details
"""
import argparse
import json
import sys
import time

from reviews_finder import find_places


def main():
    ap = argparse.ArgumentParser(
        description="Find all Google Maps places for a city + categories, with business details.")
    ap.add_argument("city", help='City (or any area), e.g. "Berlin" or "Berlin, Germany"')
    ap.add_argument("--categories", required=True,
                    help='Comma-separated categories, e.g. "restaurant,cafe,bar". '
                         "Each category is its own search; Google caps one search "
                         "at ~200-300 results, so narrower categories find more.")
    ap.add_argument("--max", type=int, default=None, help="Maximum unique places (default: all)")
    ap.add_argument("--out", default="places.json", help="Output JSON file (default: places.json)")
    ap.add_argument("--hl", default="en", help="Language for names/addresses (default: en)")
    ap.add_argument("--gl", default="us", help="Country bias, e.g. de (default: us)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="Seconds between search pages (default: 0.3; with proxies use 0)")
    ap.add_argument("--fast", action="store_true",
                    help="Skip the per-place details lookup (no total_reviews / histogram; "
                         "one request per 20 places instead of a few per place)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel details lookups (default: 4)")
    ap.add_argument("--proxy", action="append", default=[],
                    help="Proxy URL (repeatable), e.g. http://user:pass@host:port")
    ap.add_argument("--proxy-file", help="File with one proxy URL per line")
    args = ap.parse_args()

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    if not categories:
        ap.error("--categories is empty")

    proxies = list(args.proxy)
    if args.proxy_file:
        with open(args.proxy_file, encoding="utf-8") as f:
            proxies += [line.strip() for line in f if line.strip() and not line.startswith("#")]

    started = time.time()

    def progress(stage, label, count):
        if stage == "search":
            print(f"  [{label}] {count} places found", file=sys.stderr)
        else:
            print(f"  details {count}: {label}", file=sys.stderr)

    result = find_places(
        args.city, categories, max_places=args.max, hl=args.hl, gl=args.gl,
        delay=args.delay, details=not args.fast, workers=args.workers,
        proxies=proxies or None, on_progress=progress,
    )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - started
    print(f"\nDone: {result['place_count']} places in {args.city} "
          f"({', '.join(categories)}) in {elapsed:.1f}s -> {args.out}")


if __name__ == "__main__":
    main()
