import shelve
from datetime import date

import pytest
from fakes import FakeSearch, assert_partition, make_items

from uncapped import CapExceeded, DateRange, Range, UncappedWarning, Values, split

PRICE = Range("price", 0, 100_000)
LISTED = DateRange("listed", "2020-01-01", "2022-12-31")


def run(search, **kw):
    r = split(search.count, **kw)
    return r, list(r)


def test_whole_search_fits():
    s = FakeSearch(make_items(500), [PRICE])
    r, windows = run(s, cap=1000, dims=[PRICE])
    assert [(w.filters, w.count, w.complete) for w in windows] == [({}, 500, True)]
    assert r.stats.queries == 1
    assert r.stats.coverage == 1.0


def test_empty_search():
    s = FakeSearch([], [PRICE])
    r, windows = run(s, cap=1000, dims=[PRICE])
    assert windows == []
    assert r.stats.total == 0
    assert r.stats.coverage == 1.0


@pytest.mark.parametrize("fanout", ["auto", 2, 5])
@pytest.mark.parametrize("skew", [False, True])
def test_every_item_in_exactly_one_window(fanout, skew):
    s = FakeSearch(make_items(12_000, skew=skew), [PRICE, LISTED])
    r, windows = run(s, cap=1000, dims=[PRICE, LISTED], fanout=fanout)
    assert_partition(s, windows, cap=1000)
    assert all(w.complete for w in windows)
    assert r.stats.total == 12_000
    assert r.stats.fetchable == 12_000
    assert r.stats.windows == len(windows)
    assert r.stats.unaccounted == 0
    assert r.stats.queries == len(s.calls)


def test_windows_come_in_dimension_order():
    s = FakeSearch(make_items(5_000), [PRICE])
    _, windows = run(s, cap=1000, dims=[PRICE])
    lows = [w.filters["price"][0] for w in windows]
    assert lows == sorted(lows)


def test_auto_fanout_saves_calls():
    items = make_items(20_000)
    auto = FakeSearch(items, [PRICE])
    halving = FakeSearch(items, [PRICE])
    list(split(auto.count, cap=500, dims=[PRICE]))
    list(split(halving.count, cap=500, dims=[PRICE], fanout=2))
    assert len(auto.calls) < len(halving.calls)


def test_falls_back_to_next_dimension_when_first_is_exhausted():
    items = make_items(3_000)
    for it in items:
        it["price"] = 42  # every item has the same price: price can't separate them
    s = FakeSearch(items, [PRICE, LISTED])
    _, windows = run(s, cap=200, dims=[PRICE, LISTED])
    assert_partition(s, windows, cap=200)
    assert all(w.filters["price"] == (42, 42) for w in windows)
    assert all("listed" in w.filters for w in windows)


def test_values_dimension_fans_out_one_value_at_a_time():
    types = Values("type", ["apartment", "villa", "townhouse", "office"])
    s = FakeSearch(make_items(4_000), [types, PRICE])
    _, windows = run(s, cap=600, dims=[types, PRICE])
    assert_partition(s, windows, cap=600)
    assert {w.filters["type"] for w in windows} == set(types.values)
    assert list(windows[0].filters) == ["type", "price"]  # dimension order is kept


def test_values_multi_receives_tuples():
    types = Values("type", ["apartment", "villa", "townhouse", "office"], multi=True)
    s = FakeSearch(make_items(1_500), [types])
    _, windows = run(s, cap=1000, dims=[types])
    assert_partition(s, windows, cap=1000)
    assert all(isinstance(w.filters["type"], tuple) for w in windows)


def test_window_still_over_cap_warns_and_is_marked():
    items = [{"id": i, "price": 7} for i in range(50)]
    s = FakeSearch(items, [Range("price", 0, 10)])
    with pytest.warns(UncappedWarning, match="still have more than 10 results"):
        r, windows = run(s, cap=10, dims=[Range("price", 0, 10)])
    assert [(w.filters, w.count, w.complete) for w in windows] == [({"price": (7, 7)}, 50, False)]
    assert r.stats.capped_windows == 1
    assert r.stats.fetchable == 10
    assert r.stats.coverage == pytest.approx(0.2)


def test_on_capped_raise():
    items = [{"id": i, "price": 7} for i in range(50)]
    s = FakeSearch(items, [Range("price", 0, 10)])
    with pytest.raises(CapExceeded) as exc:
        list(split(s.count, cap=10, dims=[Range("price", 0, 10)], on_capped="raise"))
    assert exc.value.window.filters == {"price": (7, 7)}


def test_on_capped_ignore_is_silent():
    items = [{"id": i, "price": 7} for i in range(50)]
    s = FakeSearch(items, [Range("price", 0, 10)])
    r, windows = run(s, cap=10, dims=[Range("price", 0, 10)], on_capped="ignore")
    assert not windows[0].complete


def test_no_dimensions_and_over_cap():
    s = FakeSearch(make_items(50), [])
    with pytest.warns(UncappedWarning):
        _, windows = run(s, cap=10, dims=[])
    assert [(w.filters, w.complete) for w in windows] == [({}, False)]


def test_items_the_dimensions_cannot_reach_are_reported():
    items = make_items(3_000, none_price=120)  # 120 listings have no price
    s = FakeSearch(items, [PRICE])
    with pytest.warns(UncappedWarning, match="120 result"):
        r, windows = run(s, cap=1000, dims=[PRICE])
    reachable = [it for it in items if it["price"] is not None]
    assert_partition(s, windows, cap=1000, reachable=reachable)
    assert r.stats.unaccounted == 120
    assert r.stats.fetchable == 2_880


def still_mergeable(windows, cap):
    """Neighbours that differ only in their last filter and would still fit together."""
    return [
        (a.filters, b.filters)
        for a, b in zip(windows, windows[1:])
        if a.complete
        and b.complete
        and a.count + b.count <= cap
        and a.filters
        and a.filters.keys() == b.filters.keys()
        and all(a.filters[k] == b.filters[k] for k in list(a.filters)[:-1])
    ]


def test_merging_removes_the_tiny_windows_around_a_spike():
    items = make_items(4_000)
    for it in items[1_500:]:
        it["price"] = 42_000  # 2,500 listings at one price, like a new development
    merged_search = FakeSearch(items, [PRICE, LISTED])
    plain_search = FakeSearch(items, [PRICE, LISTED])
    r, merged = run(merged_search, cap=1000, dims=[PRICE, LISTED])
    _, plain = run(plain_search, cap=1000, dims=[PRICE, LISTED], merge=False)

    assert_partition(merged_search, merged, cap=1000)  # merged counts are exact
    assert_partition(plain_search, plain, cap=1000)
    assert len(merged) <= len(plain) // 2
    assert not still_mergeable(merged, cap=1000)
    assert still_mergeable(plain, cap=1000)
    assert len(merged_search.calls) == len(plain_search.calls)  # merging costs no counts
    assert r.stats.windows == len(merged)
    assert r.stats.fetchable == 4_000


def test_never_merges_across_results_that_could_not_be_reached():
    # 1,200 listings at one price have no listing date, so neither filter can
    # reach them. The windows on either side are small enough to merge, but a
    # window spanning both would also return those 1,200 and be over the cap.
    items = make_items(1_800)
    for it in items[:300]:
        it["price"] = it["price"] % 30_000
    for it in items[300:600]:
        it["price"] = 70_000 + it["price"] % 30_000
    for it in items[600:]:
        it["price"], it["listed"] = 50_000, None
    s = FakeSearch(items, [PRICE, LISTED])
    with pytest.warns(UncappedWarning, match="1,200 result"):
        r, windows = run(s, cap=1000, dims=[PRICE, LISTED])
    assert_partition(s, windows, cap=1000, reachable=items[:600])
    assert all(not (lo <= 50_000 <= hi) for lo, hi in (w.filters["price"] for w in windows))
    assert r.stats.unaccounted == 1_200


def test_single_values_are_never_merged():
    types = Values("type", ["apartment", "villa", "townhouse", "office"])
    s = FakeSearch(make_items(1_500), [types])
    _, windows = run(s, cap=1000, dims=[types])  # each type alone is ~375: merging would fit
    assert [w.filters["type"] for w in windows] == list(types.values)


def test_cache_makes_a_rerun_free(tmp_path):
    s = FakeSearch(make_items(8_000), [PRICE, LISTED])
    with shelve.open(str(tmp_path / "counts")) as cache:
        first = split(s.count, cap=1000, dims=[PRICE, LISTED], cache=cache)
        windows = list(first)
    calls = len(s.calls)
    with shelve.open(str(tmp_path / "counts")) as cache:
        again = split(s.count, cap=1000, dims=[PRICE, LISTED], cache=cache)
        assert list(again) == windows
    assert len(s.calls) == calls  # nothing was counted twice
    assert again.stats.queries == 0
    assert again.stats.cache_hits == first.stats.queries


def test_count_receives_a_copy():
    seen = []

    def count(filters):
        seen.append(filters)
        filters["junk"] = 1
        return 1_000 if "price" in filters else 2_000

    list(split(count, cap=1000, dims=[Range("price", 0, 9)], fanout=2))
    assert seen[1] == {"price": (0, 4), "junk": 1}


def test_iterating_twice_resets_stats():
    s = FakeSearch(make_items(3_000), [PRICE])
    r = split(s.count, cap=1000, dims=PRICE)  # a single dimension is fine too
    a, b = list(r), list(r)
    assert a == b
    assert r.stats.windows == len(a)


@pytest.mark.parametrize("bad", [None, -1, 1.5, True, "3"])
def test_count_must_return_an_int(bad):
    with pytest.raises(TypeError):
        list(split(lambda f: bad, cap=10, dims=[PRICE]))


@pytest.mark.parametrize(
    "kw",
    [
        {"cap": 0},
        {"cap": 1.5},
        {"dims": [PRICE, Range("price", 0, 1)]},
        {"fanout": 1},
        {"fanout": "fast"},
        {"max_fanout": 1},
        {"on_capped": "explode"},
        {"merge": "yes"},
    ],
)
def test_invalid_settings(kw):
    settings = {"cap": 10, "dims": [PRICE], **kw}
    with pytest.raises(ValueError):
        split(lambda f: 0, **settings)


def test_github_style_query_building():
    """The README example: dates rendered straight into a query string."""
    created = DateRange("created", "2024-01-01", "2024-01-31")
    queries = []

    def count(f):
        lo, hi = f.get("created", (created.start, created.end))
        queries.append(f"language:python created:{lo}..{hi}")
        days = (hi - lo).days + 1
        return days * 100

    windows = list(split(count, cap=1000, dims=[created]))
    assert queries[0] == "language:python created:2024-01-01..2024-01-31"
    assert sum(w.count for w in windows) == 3_100
    assert windows[0].filters["created"][0] == date(2024, 1, 1)
    assert windows[-1].filters["created"][1] == date(2024, 1, 31)
