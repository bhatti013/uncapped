# uncapped

[![PyPI](https://img.shields.io/pypi/v/uncapped)](https://pypi.org/project/uncapped/)
[![Python](https://img.shields.io/pypi/pyversions/uncapped)](https://pypi.org/project/uncapped/)
[![CI](https://github.com/bhatti013/uncapped/actions/workflows/ci.yml/badge.svg)](https://github.com/bhatti013/uncapped/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Get every result from searches that stop at 1,000.**

Lots of APIs and sites cut you off no matter how many results match:

| Source | Most results per search |
|---|---|
| GitHub search API | 1,000 |
| Algolia (default `paginationLimitedTo`) | 1,000 |
| Elasticsearch / OpenSearch `from` + `size` (default `max_result_window`) | 10,000 |
| Google Places text search | 60 |
| Most listing, travel and job sites | a few hundred to ~1,000 |

The fix is always the same: narrow the search with filters (price, date,
category...) until each piece is under the limit, then page through every
piece. uncapped does the narrowing for you, picks how finely to cut so you
spend as few calls as possible, and tells you if anything still can't be
reached.

- Zero dependencies, Python 3.10+, fully typed
- Works with any client: `requests`, `httpx`, an SDK, Scrapy, SQL
- Sync and async
- Splits on numbers, dates and categories, and falls back to the next filter when one runs out
- Resumable: counts can be cached in any dict-like store

## Install

```bash
pip install uncapped
```

## Example: every Rust repo with more than 1,000 stars

GitHub's search finds 2,476 of them but will only ever return 1,000. You
tell uncapped how to **count** results for a set of filters; it gives you back
windows that each fit under the cap.

```python
import requests
from uncapped import split, DateRange

QUERY = "language:rust stars:>1000"

def with_filters(filters):
    if "created" not in filters:
        return QUERY
    lo, hi = filters["created"]
    return f"{QUERY} created:{lo}..{hi}"

def count(filters):
    r = requests.get("https://api.github.com/search/repositories",
                     params={"q": with_filters(filters), "per_page": 1})
    return r.json()["total_count"]

run = split(count, cap=1000, dims=[DateRange("created", "2008-01-01", "today")])

for window in run:
    print(window.filters["created"], window.count)
    # page through with_filters(window.filters) as usual: it's under 1,000

print(run.stats)
```

Real output (November 2026):

```
created                     repos
2008-01-01 .. 2012-09-17       13
2012-09-18 .. 2017-06-04      406
2017-06-05 .. 2022-02-19      984
2022-02-20 .. 2024-06-29      513
2024-06-30 .. 2026-11-06      560

2,476 repos match. Without uncapped you'd get 1,000.
5 windows, 2,476 fetchable (100%), 7 count requests.
```

Paging through those 5 windows downloaded all 2,476 repos, with no duplicates.
The runnable version, with rate-limit handling and download, is
[`examples/github_repos.py`](examples/github_repos.py).

## How it works

1. Count the whole search. Under the cap? Done: one window, no filters.
2. Over the cap? Cut the first dimension's range into parts. With the default
   `fanout="auto"` the number of parts depends on how far over the cap it is
   (5× over the cap → 7 parts), so you don't pay for many rounds of halving.
3. Count each part. Parts under the cap become windows; parts over it are cut
   again. Empty parts are dropped.
4. When a dimension is down to a single value (every listing in this window
   costs exactly 42) uncapped moves on to the next dimension.
5. Windows come out in order, as soon as they're ready, so you can start
   fetching before the plan is finished.

## Dimensions

Your `count` function receives a dict keyed by dimension name. A dimension
that hasn't been needed yet is **left out**, so filter on it only when it's there.

```python
from datetime import datetime, timedelta
from uncapped import Range, DateRange, Values

Range("price", 0, 5_000_000)                # filters["price"] == (0, 2_499_999), inclusive
Range("rating", 0, 5, step=0.1)             # floats work; step is the smallest gap the source sees
DateRange("listed", "2023-01-01", "today")  # filters["listed"] == (date(...), date(...)), inclusive
DateRange("posted", datetime(2024, 1, 1), "now", step=timedelta(minutes=1))
Values("type", ["apartment", "villa", "office"])            # one value at a time: filters["type"] == "villa"
Values("lang", ["python", "go", "rust"], multi=True)         # groups: filters["lang"] == ("python", "go")
```

Give several dimensions in order of preference. uncapped splits on the first
until it can't, then the second:

```python
split(count, cap=1000, dims=[
    Range("price", 0, 10_000_000),
    DateRange("listed", "2020-01-01", "today"),
])
```

## Knowing what you didn't get

uncapped never fails silently. After a run, `run.stats` has the numbers and an
`UncappedWarning` is raised for anything you'd miss:

```python
Stats(total=48213, windows=61, fetchable=47650, capped_windows=1,
      unaccounted=313, queries=74, cache_hits=0, max_depth=3)
```

- **`capped_windows`**: windows still over the cap after every dimension is
  down to one value. They're yielded with `window.complete == False`; you can
  fetch the first `cap` results of each. Add another dimension or a finer
  `step`. Use `on_capped="raise"` to stop instead, or `"ignore"` to stay quiet.
- **`unaccounted`**: the parts of a split added up to less than the whole.
  That's almost always items with **no value** for the filter (a listing
  without a price never matches any price range) or values outside the range
  you gave. Widen the range, or fetch those items another way.

## Async

`count` can be async. Sibling counts run concurrently, up to `concurrency`:

```python
import httpx
from uncapped import asplit, Range

async def count(filters):
    lo, hi = filters.get("price", (None, None))
    r = await client.get("/search", params={"min_price": lo, "max_price": hi, "limit": 1})
    return r.json()["total"]

async for window in asplit(count, cap=1000, dims=[Range("price", 0, 10**7)], concurrency=8):
    ...
```

## Resuming and saving calls

Pass any dict-like object as `cache` and counts are stored there. With a
persistent one, a crashed or repeated run re-plans without calling the API
again:

```python
import shelve

with shelve.open("counts") as cache:
    for window in split(count, cap=1000, dims=dims, cache=cache):
        ...
```

Counts are cached by their filters and don't expire. Delete the file when you
want fresh numbers.

## Recipes

**Elasticsearch / OpenSearch** (when you can't use `search_after` or a point in time):

```python
def count(filters):
    query = {"bool": {"filter": []}}
    if "ts" in filters:
        lo, hi = filters["ts"]
        query["bool"]["filter"].append({"range": {"ts": {"gte": lo.isoformat(), "lte": hi.isoformat()}}})
    return es.count(index="events", query=query)["count"]

split(count, cap=10_000, dims=[DateRange("ts", datetime(2024, 1, 1), "now", step=timedelta(seconds=1))])
```

**A listing site's JSON API** with min/max price parameters:

```python
def count(filters):
    params = {"category": "cars", "page_size": 1}
    if "price" in filters:
        params["price_min"], params["price_max"] = filters["price"]
    return session.get(SEARCH_URL, params=params).json()["meta"]["total"]
```

## FAQ

**Why not just ask for page 11?** The cap is enforced by the server. Past it
you get an error or the same results again.

**My source's counts are estimates.** Fine: windows are still correct, but
`unaccounted` may show small numbers from rounding. Leave some headroom by
passing a slightly smaller `cap` (e.g. `950`).

**Data changes while I fetch.** Windows are planned from the counts at that
moment. New items in a window may push it a little over the cap; a smaller
`cap` gives you headroom here too.

**Is this allowed?** uncapped only decides which searches to run. Respect the
terms and rate limits of whatever you query.

## License

MIT
