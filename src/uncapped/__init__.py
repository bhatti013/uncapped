"""uncapped: get every result from searches that stop at N results.

GitHub search stops at 1,000 results, Elasticsearch at 10,000, most listing
sites at a few hundred pages. uncapped cuts one big search into windows that
each fit under the cap, by splitting the filters you give it (price, date,
category...), so paging through every window returns everything.

    >>> from uncapped import split, DateRange
    >>> for window in split(count, cap=1000, dims=[DateRange("created", "2008-01-01", "today")]):
    ...     fetch_all(window.filters)
"""

from .core import (
    AsyncSplit,
    CapExceeded,
    Split,
    Stats,
    UncappedWarning,
    Window,
    asplit,
    split,
)
from .dims import DateRange, Dimension, Range, Values

__all__ = [
    "split",
    "asplit",
    "Range",
    "DateRange",
    "Values",
    "Dimension",
    "Window",
    "Stats",
    "Split",
    "AsyncSplit",
    "CapExceeded",
    "UncappedWarning",
]

__version__ = "0.1.0"
