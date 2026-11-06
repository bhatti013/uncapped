from datetime import date, datetime, timedelta

import pytest

from uncapped import DateRange, Range, Values
from uncapped.dims import _chunks


@pytest.mark.parametrize("width,k", [(1, 2), (2, 2), (7, 3), (100, 16), (5, 10)])
def test_chunks_are_contiguous_and_cover_everything(width, k):
    parts = _chunks((10, 10 + width - 1), k)
    assert parts[0][0] == 10
    assert parts[-1][1] == 10 + width - 1
    for (a, b), (c, _) in zip(parts, parts[1:]):
        assert c == b + 1
    sizes = [b - a + 1 for a, b in parts]
    assert max(sizes) - min(sizes) <= 1
    assert len(parts) == min(k, width)


def test_int_range():
    r = Range("price", 100, 199, step=10)
    assert len(r) == 10
    assert r.render(r.full) == (100, 190)
    assert r.render((2, 3)) == (120, 130)


def test_float_range_points_are_exact():
    r = Range("rating", 0, 5, step=0.1)
    assert len(r) == 51
    assert r.render((3, 3)) == (0.3, 0.3)
    assert r.render(r.full) == (0.0, 5.0)


def test_date_range_from_strings():
    d = DateRange("created", "2024-01-01", "2024-01-31")
    assert len(d) == 31
    assert d.render(d.full) == (date(2024, 1, 1), date(2024, 1, 31))
    assert d.render((1, 1)) == (date(2024, 1, 2), date(2024, 1, 2))


def test_datetime_range_with_step():
    d = DateRange("t", datetime(2024, 1, 1), datetime(2024, 1, 1, 23), step=timedelta(hours=1))
    assert len(d) == 24
    assert d.render((5, 6)) == (datetime(2024, 1, 1, 5), datetime(2024, 1, 1, 6))
    assert len(DateRange("t", "2024-01-01T00:00:00", "2024-01-01T00:01:00")) == 61


def test_date_range_today():
    d = DateRange("created", "2024-01-01", "today")
    assert d.end == date.today()


def test_values_single_and_multi():
    single = Values("type", ["a", "b", "c"])
    assert single.parts(single.full, 2) == [(0, 0), (1, 1), (2, 2)]
    assert single.render((1, 1)) == "b"
    with pytest.raises(ValueError):
        single.render((0, 1))
    multi = Values("type", ["a", "b", "c"], multi=True)
    assert multi.parts(multi.full, 2) == [(0, 1), (2, 2)]
    assert multi.render((0, 1)) == ("a", "b")


@pytest.mark.parametrize(
    "make",
    [
        lambda: Range("x", 0, 10, step=0),
        lambda: Range("x", 10, 0),
        lambda: DateRange("x", "2024-02-01", "2024-01-01"),
        lambda: DateRange("x", date(2024, 1, 1), datetime(2024, 1, 2)),
        lambda: DateRange("x", "2024-01-01", "2024-02-01", step=timedelta(hours=12)),
        lambda: DateRange("x", "2024-01-01", "2024-02-01", step=timedelta(0)),
        lambda: Values("x", []),
        lambda: Values("x", ["a", "a"]),
    ],
)
def test_invalid_dimensions(make):
    with pytest.raises((ValueError, TypeError)):
        make()
