# uncapped recipes

Complete patterns for common capped sources. Each one follows the same shape:

1. A single function maps `filters` to the source's query.
2. `count` asks the source for a total.
3. Paging uses the same mapping.

## GitHub search (repositories, issues, users)

GitHub search returns at most 1,000 results per query.

```python
import os
import requests
from uncapped import split, DateRange

URL = "https://api.github.com/search/repositories"   # /search/issues and /search/users work the same way
QUERY = "language:rust stars:>1000"
CAP, PER_PAGE = 1000, 100
HEADERS = {"Accept": "application/vnd.github+json"}
if os.environ.get("GITHUB_TOKEN"):
    HEADERS["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"

def query(filters):
    if "created" not in filters:
        return QUERY
    lo, hi = filters["created"]
    return f"{QUERY} created:{lo.isoformat()}..{hi.isoformat()}"

def search(filters, page=1, per_page=PER_PAGE):
    r = requests.get(URL, headers=HEADERS, params={"q": query(filters), "page": page, "per_page": per_page})
    r.raise_for_status()
    return r.json()

def count(filters):
    return search(filters, per_page=1)["total_count"]

run = split(count, cap=CAP, dims=[DateRange("created", "2008-01-01", "today")])
for w in run:
    for page in range(1, -(-min(w.count, CAP) // PER_PAGE) + 1):
        for repo in search(w.filters, page=page)["items"]:
            print(repo["full_name"])
```

**Notes:**

- **Rate limits:** search allows 10 requests a minute without a token and 30 with one. On a 403 or 429 response, wait for `retry-after`, or until the `x-ratelimit-reset` time. The repository's `examples/github_repos.py` does this.
- **Incomplete results:** if a response has `"incomplete_results": true`, the search timed out and `total_count` may be too low. Retry that count.
- **Busy days:** if a single day has more than 1,000 matches, split by time instead. GitHub accepts qualifiers like `created:2024-01-01T00:00:00+00:00..2024-01-01T11:59:59+00:00`. Use timezone-aware datetimes; `query()` above already formats both with `isoformat()`:

  ```python
  from datetime import datetime, timedelta, timezone
  DateRange("created",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            step=timedelta(seconds=1))
  ```

## Elasticsearch and OpenSearch (from + size)

`from + size` can't go past `index.max_result_window`, which is 10,000 by default.

```python
from datetime import datetime, timedelta, timezone
from elasticsearch import Elasticsearch
from uncapped import split, DateRange

es = Elasticsearch("http://localhost:9200")
INDEX = "logs"
CAP, SIZE = 10_000, 1_000          # index.max_result_window

def query(filters):
    clauses = [{"term": {"status": "failed"}}]          # your own query
    if "ts" in filters:
        lo, hi = filters["ts"]
        clauses.append({"range": {"@timestamp": {"gte": lo.isoformat(), "lte": hi.isoformat()}}})
    return {"bool": {"filter": clauses}}

def count(filters):
    return es.count(index=INDEX, query=query(filters))["count"]   # exact, unlike hits.total

end = datetime.now(timezone.utc).replace(microsecond=0)
last_year = DateRange("ts", end - timedelta(days=365), end, step=timedelta(milliseconds=1))

for w in split(count, cap=CAP, dims=[last_year]):
    for offset in range(0, min(w.count, CAP), SIZE):
        res = es.search(index=INDEX, query=query(w.filters), from_=offset, size=SIZE,
                        sort=[{"@timestamp": "asc"}, {"event_id": "asc"}])  # add a unique tiebreaker
        for hit in res["hits"]["hits"]:
            ...
```

**Notes:**

- **Step:** `step=timedelta(milliseconds=1)` matches the millisecond precision of Elasticsearch `date` fields. With a coarser step, documents between two windows are reported as `unaccounted`.
- **Unique tiebreaker:** sort on a unique field as the last sort key, so the same document never appears on two pages.
- **If you control the cluster:** `search_after` with a point in time exports everything without uncapped.

## Algolia (paginationLimitedTo)

Algolia returns at most 1,000 hits per query by default. Count with `hitsPerPage: 0`.

```python
import requests
from uncapped import split, Range

APP_ID, API_KEY, INDEX = "YourApplicationID", "YourSearchOnlyAPIKey", "products"
URL = f"https://{APP_ID}-dsn.algolia.net/1/indexes/{INDEX}/query"
HEADERS = {"X-Algolia-Application-Id": APP_ID, "X-Algolia-API-Key": API_KEY}
CAP, PER_PAGE = 1000, 1000          # paginationLimitedTo; hitsPerPage can be up to 1,000

def search(filters, page=0, hits_per_page=0):
    body = {"query": "", "page": page, "hitsPerPage": hits_per_page}
    if "price" in filters:
        lo, hi = filters["price"]
        body["numericFilters"] = [f"price:{lo} TO {hi}"]   # inclusive
    r = requests.post(URL, json=body, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def count(filters):
    r = search(filters)
    exact = r.get("exhaustive", {}).get("nbHits", r.get("exhaustiveNbHits", True))
    # Algolia only estimates large totals. Report an estimate as over the cap, so
    # uncapped keeps splitting until every window's count is exact.
    return r["nbHits"] if exact else max(r["nbHits"], CAP + 1)

run = split(count, cap=CAP, dims=[Range("price", 0, 100_000, step=0.01)])
for w in run:
    for page in range(-(-min(w.count, CAP) // PER_PAGE)):          # Algolia pages start at 0
        for hit in search(w.filters, page=page, hits_per_page=PER_PAGE)["hits"]:
            ...
```

**Notes:**

- **Estimated totals:** Algolia marks estimates with `exhaustive.nbHits: false` (older responses use `exhaustiveNbHits`). Small result sets come back exact, which is why `count` above treats an estimate as over the cap.
- **Shared values:** if more than 1,000 records share one exact price, price alone can't split them, and uncapped warns about a capped window. Add a second numeric attribute as another dimension, such as an id or a popularity score.
- **Overstated warning:** with estimated totals, `run.stats.unaccounted` and its warning can overstate what was missed. Count the unique records you fetched and compare that with the unfiltered total.
- **Live test:** on Algolia's public demo index, which has 21,468 products, splitting on price and popularity fetched all 21,468 with no duplicates, even though the warning reported 1,798 unreachable.
- **Browse endpoint:** with an API key that has the browse permission, Algolia's browse endpoint returns everything without uncapped.

## APIs that don't report a total

If the API returns pages but no total, `count` can work out whether a search is over the cap from the last page it allows. For searches under the cap, it finds the exact count.

```python
import requests
from uncapped import split, Range

URL = "https://api.example.com/listings"
PAGE_SIZE, MAX_PAGES = 50, 20            # the site stops at page 20
CAP = PAGE_SIZE * MAX_PAGES              # so a search returns at most 1,000

def page(filters, n):
    params = {"page": n, "page_size": PAGE_SIZE}
    if "price" in filters:
        params["min_price"], params["max_price"] = filters["price"]
    r = requests.get(URL, params=params)
    r.raise_for_status()
    return r.json()["items"]

def count(filters):
    """Exact under the cap; CAP + 1 ("too many") when the last allowed page is full."""
    last = page(filters, MAX_PAGES)
    if len(last) == PAGE_SIZE:
        return CAP + 1
    if last:
        return (MAX_PAGES - 1) * PAGE_SIZE + len(last)
    lo, hi = 0, MAX_PAGES - 1            # binary search for the last page with results
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if page(filters, mid):
            lo = mid
        else:
            hi = mid - 1
    return 0 if lo == 0 else (lo - 1) * PAGE_SIZE + len(page(filters, lo))

run = split(count, cap=CAP, dims=[Range("price", 0, 10_000_000)])
for w in run:
    for n in range(1, -(-min(w.count, CAP) // PAGE_SIZE) + 1):
        for item in page(w.filters, n):
            ...
```

**Notes:**

- **Cost:** each count makes up to about 6 page requests instead of 1.
- **Totals:** `run.stats.total` is a lower bound here, because over-cap searches report `CAP + 1`. Window counts are exact.
