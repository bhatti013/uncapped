"""Dimensions: the filters a search can be narrowed by.

Every dimension is an ordered list of points (prices, dates, categories...).
While splitting, uncapped tracks a dimension's current slice as an inclusive
pair of point indices ``(first, last)`` and only turns it into real values
when it builds the filters it passes to your ``count`` function.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Hashable, Iterable, List, Sequence, Tuple, Union

#: An inclusive slice of a dimension, as indices into its points.
Slice = Tuple[int, int]

Number = Union[int, float]
DateLike = Union[date, datetime, str]


class Dimension:
    """Base class for something a search can be filtered by.

    Subclasses define how many points they have (``__len__``), how a slice of
    points is shown to your ``count`` function (:meth:`render`) and, if needed,
    how a slice is cut into smaller slices (:meth:`parts`).
    """

    name: str

    def __len__(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def full(self) -> Slice:
        """The slice covering every point of the dimension."""
        return (0, len(self) - 1)

    def render(self, part: Slice) -> Any:  # pragma: no cover - abstract
        """The filter value your ``count`` function receives for ``part``."""
        raise NotImplementedError

    def parts(self, part: Slice, k: int) -> List[Slice]:
        """Cut ``part`` into ``k`` contiguous, non-overlapping slices."""
        return _chunks(part, k)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r}, {len(self)} points)"


class Range(Dimension):
    """A numeric range filter such as ``price_min``/``price_max``.

    Your ``count`` function receives ``(lo, hi)``, both **inclusive**.

    Args:
        name: Key used in the filters dict.
        lo: Smallest value to cover.
        hi: Largest value to cover.
        step: The smallest difference the source can tell apart. Use ``1`` for
            whole numbers (the default) or e.g. ``0.01`` for prices in cents.

    Example::

        Range("price", 0, 5_000_000)          # -> filters["price"] == (0, 2_499_999)
        Range("rating", 0, 5, step=0.1)       # -> filters["rating"] == (0.0, 2.4)
    """

    def __init__(self, name: str, lo: Number, hi: Number, step: Number = 1) -> None:
        if step <= 0:
            raise ValueError(f"{name}: step must be positive, got {step!r}")
        if hi < lo:
            raise ValueError(f"{name}: hi ({hi!r}) is smaller than lo ({lo!r})")
        self.name = name
        self.lo, self.hi, self.step = lo, hi, step
        self._exact = all(isinstance(v, int) for v in (lo, hi, step))
        if self._exact:
            self._n = (hi - lo) // step + 1
        else:
            # Decimal keeps points like 0.1, 0.2, 0.3 exact instead of 0.30000000000000004.
            self._dlo, dhi, self._dstep = (Decimal(str(v)) for v in (lo, hi, step))
            self._n = int((dhi - self._dlo) // self._dstep) + 1

    def __len__(self) -> int:
        return self._n

    def point(self, i: int) -> Number:
        if self._exact:
            return self.lo + i * self.step
        return float(self._dlo + i * self._dstep)

    def render(self, part: Slice) -> Tuple[Number, Number]:
        return (self.point(part[0]), self.point(part[1]))


class DateRange(Dimension):
    """A date or datetime range filter such as ``created:2020-01-01..2020-06-30``.

    Your ``count`` function receives ``(start, end)``, both **inclusive**, as
    :class:`~datetime.date` or :class:`~datetime.datetime` objects (whichever
    you passed in).

    Args:
        name: Key used in the filters dict.
        start: First date to cover. ``date``, ``datetime`` or ISO string.
        end: Last date to cover. ``"today"`` and ``"now"`` are accepted.
        step: Smallest gap the source can tell apart. Defaults to one day for
            dates and one second for datetimes.

    Example::

        DateRange("created", "2008-01-01", "today")
        DateRange("posted", datetime(2024, 1, 1), "now", step=timedelta(minutes=1))
    """

    def __init__(
        self,
        name: str,
        start: DateLike,
        end: DateLike,
        step: "timedelta | None" = None,
    ) -> None:
        start, end = _as_datelike(start), _as_datelike(end)
        if isinstance(start, datetime) != isinstance(end, datetime):
            raise TypeError(f"{name}: start and end must both be dates or both be datetimes")
        is_datetime = isinstance(start, datetime)
        if step is None:
            step = timedelta(seconds=1) if is_datetime else timedelta(days=1)
        if step <= timedelta(0):
            raise ValueError(f"{name}: step must be positive, got {step!r}")
        if not is_datetime and step % timedelta(days=1):
            raise ValueError(f"{name}: with plain dates the step must be whole days")
        if end < start:
            raise ValueError(f"{name}: end ({end}) is before start ({start})")
        self.name = name
        self.start, self.end, self.step = start, end, step
        self._n = (end - start) // step + 1

    def __len__(self) -> int:
        return self._n

    def point(self, i: int) -> "date | datetime":
        return self.start + i * self.step

    def render(self, part: Slice) -> "Tuple[date | datetime, date | datetime]":
        return (self.point(part[0]), self.point(part[1]))


class Values(Dimension):
    """A categorical filter such as property type, language or country.

    Args:
        name: Key used in the filters dict.
        values: Every value to cover. Anything not listed here is not fetched
            (uncapped reports it as ``unaccounted``).
        multi: Set to ``True`` if the source accepts several values at once
            (``type in (a, b, c)``). Then values are split in groups and your
            ``count`` function receives a tuple. Otherwise (the default) it is
            tried one value at a time and receives a single value.

    Example::

        Values("type", ["apartment", "villa", "townhouse"])
        Values("language", ["python", "go", "rust"], multi=True)
    """

    def __init__(self, name: str, values: Iterable[Hashable], *, multi: bool = False) -> None:
        self.name = name
        self.values: Sequence[Hashable] = tuple(values)
        self.multi = multi
        if not self.values:
            raise ValueError(f"{name}: values is empty")
        if len(set(self.values)) != len(self.values):
            raise ValueError(f"{name}: values contains duplicates")

    def __len__(self) -> int:
        return len(self.values)

    def render(self, part: Slice) -> Any:
        first, last = part
        if self.multi:
            return tuple(self.values[first : last + 1])
        if first != last:
            raise ValueError(f"{self.name}: can only filter on one value at a time")
        return self.values[first]

    def parts(self, part: Slice, k: int) -> List[Slice]:
        if self.multi:
            return _chunks(part, k)
        return [(i, i) for i in range(part[0], part[1] + 1)]


def _chunks(part: Slice, k: int) -> List[Slice]:
    first, last = part
    width = last - first + 1
    k = max(1, min(k, width))
    size, extra = divmod(width, k)
    out = []
    start = first
    for i in range(k):
        end = start + size + (1 if i < extra else 0) - 1
        out.append((start, end))
        start = end + 1
    return out


def _as_datelike(value: DateLike) -> "date | datetime":
    if isinstance(value, (date, datetime)):
        return value
    if value == "today":
        return date.today()
    if value == "now":
        return datetime.now().replace(microsecond=0)
    if len(value) == 10:
        return date.fromisoformat(value)
    return datetime.fromisoformat(value)
