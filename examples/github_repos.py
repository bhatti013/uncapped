"""Every repository matching a GitHub search, past GitHub's 1,000-result limit.

GitHub's search API returns at most 1,000 results per query, however many
match. This splits the query by creation date until every window fits, then
(optionally) pages through each window.

    python examples/github_repos.py "language:rust stars:>1000"
    python examples/github_repos.py "language:rust stars:>1000" --fetch rust.jsonl

Set GITHUB_TOKEN for a higher rate limit (30 searches/minute instead of 10).
Standard library only, besides uncapped.
"""

import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from uncapped import DateRange, split

API = "https://api.github.com/search/repositories"
CAP = 1000  # GitHub search never returns more than this
PER_PAGE = 100


def search(q, page=1, per_page=PER_PAGE):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "uncapped-example"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    url = f"{API}?{urlencode({'q': q, 'page': page, 'per_page': per_page})}"
    while True:
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as resp:
                return json.load(resp)
        except HTTPError as e:
            if e.code not in (403, 429):
                raise
            wait = int(e.headers.get("retry-after") or 0)
            if not wait and e.headers.get("x-ratelimit-remaining") == "0":
                wait = int(e.headers["x-ratelimit-reset"]) - int(time.time()) + 1
            if wait <= 0:
                raise
            print(f"  rate limited, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query", help='GitHub search query, e.g. "language:rust stars:>1000"')
    parser.add_argument("--since", default="2008-01-01", help="earliest creation date")
    parser.add_argument("--fetch", metavar="FILE", help="also download every repo to this JSONL file")
    args = parser.parse_args()

    def with_filters(filters):
        if "created" not in filters:
            return args.query
        lo, hi = filters["created"]
        return f"{args.query} created:{lo}..{hi}"

    def count(filters):
        return search(with_filters(filters), per_page=1)["total_count"]

    run = split(count, cap=CAP, dims=[DateRange("created", args.since, "today")])

    print(f"{'created':<25} {'repos':>7}")
    windows = []
    for w in run:
        lo, hi = w.filters.get("created", ("(all)", ""))
        print(f"{lo!s:>10} .. {hi!s:<11} {w.count:>7,}")
        windows.append(w)

    s = run.stats
    print(
        f"\n{s.total:,} repos match. Without uncapped you'd get {min(s.total, CAP):,}.\n"
        f"{s.windows} windows, {s.fetchable:,} fetchable ({s.coverage:.0%}), "
        f"{s.queries} count requests."
    )

    if not args.fetch:
        return
    seen = set()
    with open(args.fetch, "w") as out:
        for w in windows:
            q = with_filters(w.filters)
            for page in range(1, -(-min(w.count, CAP) // PER_PAGE) + 1):
                for repo in search(q, page=page)["items"]:
                    if repo["id"] not in seen:
                        seen.add(repo["id"])
                        out.write(json.dumps(repo) + "\n")
            print(f"  fetched {len(seen):,} / {s.total:,}", file=sys.stderr)
    print(f"\nWrote {len(seen):,} unique repos to {args.fetch}")


if __name__ == "__main__":
    main()
