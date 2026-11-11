---
name: uncapped
description: Get every result from search APIs that stop returning results after a fixed limit, such as GitHub search (1,000), Algolia (paginationLimitedTo 1,000), Elasticsearch and OpenSearch from/size (max_result_window 10,000), Google Places (60), and listing, job or travel sites whose pagination stops early. Uses the Python library uncapped, which splits one search into filter windows that each fit under the cap. Use when code must collect all matching results but the reported total is larger than what paging returns, when errors like "Only the first 1000 search results are available" or "Result window is too large" appear, or when the user asks to scrape, export or download every result of a capped search.
license: MIT
compatibility: Python 3.10+ with the uncapped package (pip install uncapped). Needs network access to the API being queried.
metadata:
  author: bhatti013
  version: "0.1.0"
  homepage: https://github.com/bhatti013/uncapped
---

# Getting every result past a search cap with uncapped

Many search APIs stop at a fixed number of results per query, however many match. The fix is to narrow the query with filters until each piece is under the limit, then page through every piece. `uncapped` works out those pieces (windows); your code still makes every request.

## 1. Check that uncapped is the right tool

Use it when all of these hold:

- The source reports more matches than it lets you page through. For example, GitHub's `total_count` says 2,478, but page 11 at 100 per page fails.
- The search can be filtered by at least one field where each item has exactly one value: a number (price, stars, id), a date (created, listed) or a category (city, type, language).
- You can get a count for a filtered search, usually a `total` field in the response. If there isn't one, see "No total count" in [references/recipes.md](references/recipes.md).

Prefer the source's own way past the limit when you have access to it:

- Elasticsearch: `search_after` with a point in time, or the scroll API.
- Algolia: the browse endpoint.
- Non-search list endpoints, such as GitHub's "list organization repositories".

uncapped is for when all you get is filters and a page number.

## 2. Install

```bash
pip install uncapped
```

## 3. Write one filter mapping and a count function

Put the filter-to-parameter mapping in one function, and use it for counting and for fetching. `filters` is a dict keyed by dimension name. A dimension that isn't in the dict is unconstrained, so only add that filter when its key is present.

```python
import math
import requests
from uncapped import split, Range, DateRange

URL = "https://api.example.com/search"
CAP = 1000          # the most results the API returns for one search
PAGE_SIZE = 100

def params_for(filters):
    params = {"category": "cars"}
    if "price" in filters:
        params["price_min"], params["price_max"] = filters["price"]  # both inclusive
    if "listed" in filters:
        lo, hi = filters["listed"]
        params["listed_from"], params["listed_to"] = lo.isoformat(), hi.isoformat()
    return params

def count(filters):
    r = requests.get(URL, params={**params_for(filters), "page_size": 1})
    r.raise_for_status()
    return r.json()["total"]
```

When counting, ask for the smallest page the API allows (`per_page=1`, `size=0`, `hitsPerPage=0`): you only need the total.

## 4. Choose the dimensions

| Dimension | Use for | Your `count` receives |
|---|---|---|
| `Range(name, lo, hi, step=1)` | numbers: price, stars, id | `filters[name] == (lo, hi)`, both inclusive |
| `DateRange(name, start, end, step=...)` | dates or datetimes | `(start, end)`, both inclusive, as `date` or `datetime` |
| `Values(name, values)` | categories the API takes one at a time | one value |
| `Values(name, values, multi=True)` | categories the API takes as a list | a tuple of values |

Rules:

- **Order:** list dimensions in order of preference. uncapped splits the first one until it is down to a single value, then moves to the next.
- **`step`:** set it to the smallest difference the source can tell apart, for example:
  - `1` for whole-number prices
  - `0.01` for cents
  - `timedelta(days=1)` for date-only filters
  - `timedelta(seconds=1)` or finer for timestamps

  If the step is too coarse, results that fall between two windows are never fetched.
- **Range:** cover every value. Items outside `lo..hi`, or with no value for the field, are never reached; they're reported as `unaccounted`.
- **No multi-valued fields:** never split on a field where one item can have several values (tags, amenities), or on keyword search. The windows would overlap.
- **Unique id:** a unique numeric field (an id with `id_min`/`id_max` filters) can always be narrowed to one item, so no window ever stays stuck over the cap. Add it as the last dimension when the API supports it.

## 5. Plan the windows and fetch each one

```python
run = split(count, cap=CAP, dims=[
    Range("price", 0, 50_000_000),
    DateRange("listed", "2020-01-01", "today"),
])

seen = set()
for window in run:                  # windows arrive in order while planning continues
    pages = math.ceil(min(window.count, CAP) / PAGE_SIZE)
    for page in range(1, pages + 1):
        r = requests.get(URL, params={**params_for(window.filters), "page": page, "page_size": PAGE_SIZE})
        r.raise_for_status()
        for item in r.json()["items"]:
            if item["id"] not in seen:      # safety net if data changes mid-run
                seen.add(item["id"])
                save(item)

print(run.stats)
```

A window with `window.complete == True` has at most `cap` results, so normal paging reaches all of them.

## 6. Check what was missed

After the loop, read `run.stats`, and never silence `UncappedWarning`:

- **`capped_windows > 0`:** some windows are still over the cap, because every dimension is already down to one value. Those windows have `window.complete == False`, and only `cap` of their results can be fetched. Add another dimension or use a finer `step`.
- **`unaccounted > 0`:** some results aren't in any window. These are usually items with no value for a filter (a listing with no price) or values outside the range. Widen the range, or fetch those items separately.
- **`coverage`:** `fetchable / total`.
- **Estimated counts:** if any counts were estimates, `unaccounted` can be overstated. Compare the number of unique items you fetched with `run.stats.total` instead.

`on_capped="raise"` turns a stuck window into a `CapExceeded` exception instead of a warning.

## Options

- `fanout="auto"` (default) sizes each split from how far over the cap a window is. `fanout=2` always halves.
- `merge=True` (default) joins neighbouring windows that fit under the cap together, so there are fewer pages to request. It never merges across unreachable results.
- `cache=shelve.open("counts")` stores counts, so an interrupted run resumes without counting again. Cached counts never expire; delete the file to start fresh.
- `asplit(...)` takes an `async def count` and runs sibling counts concurrently (`concurrency=4`). Iterate it with `async for`.

## Gotchas

- **Exclusive max:** if the API's max parameter is exclusive (`price < max`), send `hi + step`.
- **Dates:** windows contain `date`/`datetime` objects. Format them the way the API expects, for example `lo.isoformat()`.
- **Timezones:** for timezone-aware datetimes, pass aware `start` and `end` values. Don't use `"now"`, which gives a naive datetime.
- **Estimated counts:** if the source says a count is only an estimate, return `max(estimate, cap + 1)` from `count`. Examples are Algolia `exhaustive.nbHits: false` and Elasticsearch `hits.total.relation: "gte"`. uncapped then keeps splitting until every window's count is exact.
- **Changing data:** if the data changes during the run, leave headroom with a lower cap, for example `cap=950`.
- **Elasticsearch counts:** for Elasticsearch, count with the `_count` API (`es.count`), which is exact.
- **Rate limits:** counting costs one request for each window considered, usually a few dozen.

## Recipes

[references/recipes.md](references/recipes.md) has complete, tested recipes:

- GitHub search
- Elasticsearch and OpenSearch
- Algolia
- APIs that don't report a total
