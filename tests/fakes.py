"""An in-memory search API for tests."""

import random
from datetime import date, timedelta

from uncapped import DateRange, Range, Values


class FakeSearch:
    """Filters items the same way uncapped describes filters."""

    def __init__(self, items, dims):
        self.items = items
        self.dims = {d.name: d for d in dims}
        self.calls = []

    def matches(self, item, filters):
        for name, value in filters.items():
            x = item.get(name)
            if x is None:
                return False
            dim = self.dims[name]
            if isinstance(dim, (Range, DateRange)):
                lo, hi = value
                if not lo <= x <= hi:
                    return False
            elif isinstance(dim, Values) and dim.multi:
                if x not in value:
                    return False
            elif x != value:
                return False
        return True

    def count(self, filters):
        self.calls.append(dict(filters))
        return sum(1 for item in self.items if self.matches(item, filters))

    async def acount(self, filters):
        return self.count(filters)


def make_items(n, *, seed=7, skew=False, none_price=0):
    rng = random.Random(seed)
    start = date(2020, 1, 1)
    items = []
    for i in range(n):
        if skew:
            # Most items are cheap, a long tail is expensive: like real listings.
            price = int(rng.paretovariate(1.2) * 100) if rng.random() < 0.9 else rng.randint(0, 100_000)
            price = min(price, 100_000)
        else:
            price = rng.randint(0, 100_000)
        items.append(
            {
                "id": i,
                "price": price,
                "listed": start + timedelta(days=rng.randint(0, 365 * 3)),
                "type": rng.choice(["apartment", "villa", "townhouse", "office"]),
            }
        )
    for item in items[:none_price]:
        item["price"] = None
    return items


def assert_partition(search, windows, cap, reachable=None):
    """Every reachable item lands in exactly one window, and complete windows fit the cap."""
    reachable = search.items if reachable is None else reachable
    seen = {}
    for w in windows:
        hits = [it["id"] for it in search.items if search.matches(it, w.filters)]
        assert len(hits) == w.count
        if w.complete:
            assert w.count <= cap
        for i in hits:
            assert i not in seen, f"item {i} is in two windows: {seen[i]} and {w.filters}"
            seen[i] = w.filters
    missing = {it["id"] for it in reachable} - set(seen)
    assert not missing, f"{len(missing)} items are in no window"
