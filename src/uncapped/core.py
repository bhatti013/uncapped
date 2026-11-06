"""The splitting engine."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import warnings
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .dims import Dimension, Slice

Filters = Dict[str, Any]
CountFn = Callable[[Filters], int]
AsyncCountFn = Callable[[Filters], Union[int, Awaitable[int]]]

_States = Dict[str, Slice]


class UncappedWarning(UserWarning):
    """Some results cannot be fetched. See the message and :class:`Stats`."""


class CapExceeded(Exception):
    """Raised with ``on_capped="raise"`` when a window cannot be narrowed under the cap."""

    def __init__(self, window: "Window", cap: int) -> None:
        super().__init__(
            f"{window.count:,} results for {window.filters} and no dimension can narrow it "
            f"further (cap is {cap:,}). Add another dimension or a finer step."
        )
        self.window = window


@dataclass(frozen=True)
class Window:
    """One slice of the search that returns at most ``cap`` results.

    Attributes:
        filters: Filter values for this window, keyed by dimension name, in the
            order the dimensions were given. Dimensions that were never needed
            are left out, so the dict is empty when the whole search fits.
        count: What your ``count`` function returned for these filters.
        complete: ``False`` when ``count`` is still over the cap because every
            dimension is down to a single point. Only ``cap`` results of such a
            window can be fetched.
        depth: How many splits it took to get here (for a merged window, the
            deepest of its parts).
    """

    filters: Filters
    count: int
    complete: bool = True
    depth: int = 0


@dataclass
class Stats:
    """What happened during a run. Read it from ``run.stats`` after iterating.

    Attributes:
        total: Count for the unfiltered search.
        windows: Windows yielded.
        fetchable: Results you can fetch by paging through every window
            (``min(count, cap)`` summed over windows).
        capped_windows: Windows still over the cap (``complete=False``).
        unaccounted: Results the dimensions did not reach: the children of a
            split added up to less than their parent. Usually items with no
            value for that filter (a listing without a price) or values outside
            the range you gave.
        queries: Calls made to your ``count`` function.
        cache_hits: Counts answered from the cache instead.
        max_depth: Deepest split.
    """

    total: int = 0
    windows: int = 0
    fetchable: int = 0
    capped_windows: int = 0
    unaccounted: int = 0
    queries: int = 0
    cache_hits: int = 0
    max_depth: int = 0

    @property
    def coverage(self) -> float:
        """``fetchable / total``, between 0 and 1 (1.0 when there is nothing to fetch)."""
        return 1.0 if self.total == 0 else min(1.0, self.fetchable / self.total)


@dataclass
class _Plan:
    """Settings and helpers shared by the sync and async runners."""

    cap: int
    dims: Sequence[Dimension]
    fanout: Union[int, str]
    max_fanout: int
    cache: Optional[MutableMapping[str, int]]
    on_capped: str
    merge: bool
    stats: Stats = field(default_factory=Stats)
    # The newest window, held back so the next one can be merged into it:
    # (states, window, stats.unaccounted when it was held).
    _held: Optional[Tuple[_States, Window, int]] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cap, int) or self.cap < 1:
            raise ValueError(f"cap must be a positive integer, got {self.cap!r}")
        if isinstance(self.dims, Dimension):
            self.dims = [self.dims]
        self.dims = list(self.dims)
        names = [d.name for d in self.dims]
        if len(set(names)) != len(names):
            raise ValueError(f"dimension names must be unique, got {names}")
        if self.fanout != "auto" and not (isinstance(self.fanout, int) and self.fanout >= 2):
            raise ValueError(f'fanout must be "auto" or an integer >= 2, got {self.fanout!r}')
        if self.max_fanout < 2:
            raise ValueError(f"max_fanout must be >= 2, got {self.max_fanout!r}")
        if self.on_capped not in ("warn", "raise", "ignore"):
            raise ValueError(f'on_capped must be "warn", "raise" or "ignore", got {self.on_capped!r}')
        if not isinstance(self.merge, bool):
            raise ValueError(f"merge must be True or False, got {self.merge!r}")

    # -- building filters -------------------------------------------------

    def render(self, states: _States) -> Filters:
        return {d.name: d.render(states[d.name]) for d in self.dims if d.name in states}

    @staticmethod
    def key(filters: Filters) -> str:
        return json.dumps(filters, sort_keys=True, default=str)

    def children(self, states: _States, count: int) -> Optional[List[_States]]:
        """Split along the first dimension that still has room, or None if none has."""
        for dim in self.dims:
            first, last = states.get(dim.name, dim.full)
            if first == last:
                continue
            k = self._fanout(count, last - first + 1)
            return [{**states, dim.name: part} for part in dim.parts((first, last), k)]
        return None

    def _fanout(self, count: int, width: int) -> int:
        if self.fanout == "auto":
            # Aim for children at ~80% of the cap if results were spread evenly;
            # uneven ones are simply split again.
            k = min(self.max_fanout, math.ceil(count * 1.25 / self.cap))
        else:
            k = int(self.fanout)
        return max(2, min(k, width))

    # -- counting ---------------------------------------------------------

    def cached(self, filters: Filters) -> Optional[int]:
        if self.cache is None:
            return None
        hit = self.cache.get(self.key(filters))
        if hit is not None:
            self.stats.cache_hits += 1
        return hit

    def record(self, filters: Filters, n: Any) -> int:
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise TypeError(f"count() must return a non-negative int, got {n!r} for {filters}")
        self.stats.queries += 1
        if self.cache is not None:
            self.cache[self.key(filters)] = n
        return n

    # -- results ----------------------------------------------------------

    def account(self, parent: int, children: Sequence[int]) -> None:
        self.stats.unaccounted += max(0, parent - sum(children))

    def emit(self, states: _States, n: int, depth: int) -> Window:
        complete = n <= self.cap
        window = Window(self.render(states), n, complete, depth)
        if not complete:
            if self.on_capped == "raise":
                raise CapExceeded(window, self.cap)
            self.stats.capped_windows += 1
        s = self.stats
        s.windows += 1
        s.fetchable += min(n, self.cap)
        s.max_depth = max(s.max_depth, depth)
        return window

    # -- merging ----------------------------------------------------------
    #
    # Splitting a window that sits next to a dense spot leaves many tiny
    # neighbours (1, 3, 2... results), and each one costs at least one page
    # request to fetch. Neighbours that together still fit under the cap are
    # sent as one wider window instead.
    #
    # Two windows are merged only when they differ in nothing but their last
    # split dimension, and the merged count is exact: windows come out in
    # order, so everything between the two (in that dimension) was counted as
    # empty. The one exception is results no dimension could reach (items with
    # no value for a filter): they sit inside such a gap and a wider window
    # would include them. If any were found since the held window, it is not
    # merged.

    def hold(self, states: _States, window: Window) -> Optional[Window]:
        """Take a new window; return the previous one if it can no longer grow."""
        held = self._held
        if held is not None:
            merged = self._merged(held, states, window)
            if merged is not None:
                self._held = merged
                self.stats.windows -= 1
                return None
        self._held = (states, window, self.stats.unaccounted)
        return held[1] if held is not None else None

    def release(self) -> Optional[Window]:
        """The last window, at the end of a run."""
        held, self._held = self._held, None
        return held[1] if held is not None else None

    def _merged(
        self, held: Tuple[_States, Window, int], states: _States, window: Window
    ) -> Optional[Tuple[_States, Window, int]]:
        prev_states, prev, unaccounted = held
        if not (self.merge and prev.complete and window.complete):
            return None
        if prev.count + window.count > self.cap or self.stats.unaccounted != unaccounted:
            return None
        if not states or prev_states.keys() != states.keys():
            return None
        last = next(d for d in reversed(self.dims) if d.name in states)
        if not last.mergeable:
            return None
        if any(prev_states[name] != states[name] for name in states if name != last.name):
            return None
        span = {**states, last.name: (prev_states[last.name][0], states[last.name][1])}
        count = prev.count + window.count
        merged = Window(self.render(span), count, True, max(prev.depth, window.depth))
        return span, merged, unaccounted

    def finish(self) -> None:
        s = self.stats
        if self.on_capped == "warn" and s.capped_windows:
            warnings.warn(
                f"{s.capped_windows} window(s) still have more than {self.cap:,} results; only "
                f"the first {self.cap:,} of each can be fetched. Add another dimension or use a "
                f"finer step. Coverage: {s.coverage:.1%}.",
                UncappedWarning,
                stacklevel=3,
            )
        if s.unaccounted:
            warnings.warn(
                f"{s.unaccounted:,} result(s) were not reachable through the dimensions (for "
                f"example items with no value for a filter, or values outside its range). "
                f"Widen the ranges or fetch those items separately.",
                UncappedWarning,
                stacklevel=3,
            )


class Split(_Plan):
    """A planned run. Iterate it to get :class:`Window` objects; read ``.stats`` after."""

    def __init__(self, count: CountFn, **settings: Any) -> None:
        super().__init__(**settings)
        self.count = count

    def _count(self, states: _States) -> int:
        filters = self.render(states)
        hit = self.cached(filters)
        if hit is not None:
            return hit
        return self.record(filters, self.count(dict(filters)))

    def __iter__(self) -> Iterator[Window]:
        self.stats = Stats()
        self._held = None
        root: _States = {}
        total = self._count(root)
        self.stats.total = total
        stack: List[Tuple[_States, int, int]] = [(root, total, 0)]
        while stack:
            states, n, depth = stack.pop()
            if n == 0:
                continue
            kids = None if n <= self.cap else self.children(states, n)
            if kids is None:
                done = self.hold(states, self.emit(states, n, depth))
                if done is not None:
                    yield done
                continue
            counts = [self._count(kid) for kid in kids]
            self.account(n, counts)
            stack.extend((kid, c, depth + 1) for kid, c in reversed(list(zip(kids, counts))))
        done = self.release()
        if done is not None:
            yield done
        self.finish()


class AsyncSplit(_Plan):
    """Async version of :class:`Split`. Use ``async for window in run``."""

    def __init__(self, count: AsyncCountFn, *, concurrency: int = 4, **settings: Any) -> None:
        super().__init__(**settings)
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency!r}")
        self.count = count
        self.concurrency = concurrency

    async def _count(self, states: _States, gate: asyncio.Semaphore) -> int:
        filters = self.render(states)
        hit = self.cached(filters)
        if hit is not None:
            return hit
        async with gate:
            n = self.count(dict(filters))
            if inspect.isawaitable(n):
                n = await n
        return self.record(filters, n)

    def __aiter__(self) -> AsyncIterator[Window]:
        return self._run()

    async def _run(self) -> AsyncIterator[Window]:
        self.stats = Stats()
        self._held = None
        gate = asyncio.Semaphore(self.concurrency)
        root: _States = {}
        total = await self._count(root, gate)
        self.stats.total = total
        stack: List[Tuple[_States, int, int]] = [(root, total, 0)]
        while stack:
            states, n, depth = stack.pop()
            if n == 0:
                continue
            kids = None if n <= self.cap else self.children(states, n)
            if kids is None:
                done = self.hold(states, self.emit(states, n, depth))
                if done is not None:
                    yield done
                continue
            counts = await asyncio.gather(*(self._count(kid, gate) for kid in kids))
            self.account(n, counts)
            stack.extend((kid, c, depth + 1) for kid, c in reversed(list(zip(kids, counts))))
        done = self.release()
        if done is not None:
            yield done
        self.finish()


def split(
    count: CountFn,
    *,
    cap: int,
    dims: Union[Dimension, Sequence[Dimension]],
    fanout: Union[int, str] = "auto",
    max_fanout: int = 16,
    cache: Optional[MutableMapping[str, int]] = None,
    on_capped: str = "warn",
    merge: bool = True,
) -> Split:
    """Cut a search into windows that each return at most ``cap`` results.

    Args:
        count: ``count(filters) -> int``. Return how many results the search has
            with these filters. ``filters`` maps dimension names to values;
            dimensions not in it should not be filtered on.
        cap: The most results the source will return for one search.
        dims: What to split on, in order of preference. The first dimension is
            split until it is down to a single point, then the next one is used.
        fanout: How many parts to cut a window into. ``"auto"`` (default) picks
            it from how far over the cap the window is, which saves calls;
            ``2`` always halves.
        max_fanout: Upper bound for ``"auto"``.
        cache: Any dict-like object (``dict``, ``shelve.open(...)``) to keep
            counts in. Pass a persistent one to resume a run without spending
            API calls on counts you already have.
        on_capped: What to do when a window is still over the cap and cannot be
            narrowed: ``"warn"`` (default; it is yielded with
            ``complete=False``), ``"raise"`` :class:`CapExceeded`, or
            ``"ignore"``.
        merge: Merge neighbouring windows that together still fit under the
            cap (default ``True``), so fetching them takes fewer requests. Each
            window then comes out one step later. ``False`` keeps every window
            as it was split.

    Returns:
        A :class:`Split`. Iterate it for :class:`Window` objects in dimension
        order; read ``.stats`` afterwards.
    """
    return Split(
        count,
        cap=cap,
        dims=dims,
        fanout=fanout,
        max_fanout=max_fanout,
        cache=cache,
        on_capped=on_capped,
        merge=merge,
    )


def asplit(
    count: AsyncCountFn,
    *,
    cap: int,
    dims: Union[Dimension, Sequence[Dimension]],
    fanout: Union[int, str] = "auto",
    max_fanout: int = 16,
    cache: Optional[MutableMapping[str, int]] = None,
    on_capped: str = "warn",
    merge: bool = True,
    concurrency: int = 4,
) -> AsyncSplit:
    """Async :func:`split`. ``count`` may be async; sibling counts run concurrently.

    ``concurrency`` limits how many ``count`` calls run at the same time.
    Use ``async for window in asplit(...)``.
    """
    return AsyncSplit(
        count,
        cap=cap,
        dims=dims,
        fanout=fanout,
        max_fanout=max_fanout,
        cache=cache,
        on_capped=on_capped,
        merge=merge,
        concurrency=concurrency,
    )
