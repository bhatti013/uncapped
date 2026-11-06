import asyncio

import pytest
from fakes import FakeSearch, assert_partition, make_items

from uncapped import DateRange, Range, UncappedWarning, asplit, split

PRICE = Range("price", 0, 100_000)
LISTED = DateRange("listed", "2020-01-01", "2022-12-31")


async def collect(r):
    return [w async for w in r]


@pytest.mark.parametrize("merge", [True, False])
def test_async_matches_sync(merge):
    items = make_items(10_000, skew=True)
    search = FakeSearch(items, [PRICE, LISTED])
    sync = list(split(search.count, cap=700, dims=[PRICE, LISTED], merge=merge))
    s = FakeSearch(items, [PRICE, LISTED])
    r = asplit(s.acount, cap=700, dims=[PRICE, LISTED], merge=merge)
    windows = asyncio.run(collect(r))
    assert windows == sync
    assert_partition(s, windows, cap=700)
    assert r.stats.fetchable == 10_000


def test_async_accepts_a_plain_function():
    s = FakeSearch(make_items(3_000), [PRICE])
    windows = asyncio.run(collect(asplit(s.count, cap=1000, dims=[PRICE])))
    assert_partition(s, windows, cap=1000)


def test_concurrency_limit_is_respected():
    s = FakeSearch(make_items(20_000), [PRICE])
    running = 0
    peak = 0

    async def count(filters):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.001)
        running -= 1
        return s.count(filters)

    windows = asyncio.run(collect(asplit(count, cap=500, dims=[PRICE], concurrency=3)))
    assert_partition(s, windows, cap=500)
    assert 1 < peak <= 3


def test_async_warns_when_capped():
    s = FakeSearch([{"id": i, "price": 1} for i in range(30)], [Range("price", 0, 5)])
    with pytest.warns(UncappedWarning):
        windows = asyncio.run(collect(asplit(s.acount, cap=10, dims=[Range("price", 0, 5)])))
    assert not windows[0].complete


def test_invalid_concurrency():
    with pytest.raises(ValueError):
        asplit(lambda f: 0, cap=10, dims=[PRICE], concurrency=0)
