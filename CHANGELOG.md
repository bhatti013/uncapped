# Changelog

## Unreleased

- Agent Skill in `skills/uncapped/`, which teaches coding agents when and how to use uncapped. It comes with tested recipes for GitHub, Elasticsearch, Algolia, and APIs without a total count.
- `llms.txt`, a short summary of the API for AI tools.
- `context7.json`, so Context7 indexes the docs and gives coding agents the usage rules.

## 0.1.0

First release.

- `split()` and `asplit()`: cut a capped search into windows that each fit under the cap.
- Dimensions: `Range` (numbers, any step), `DateRange` (dates and datetimes), `Values` (categories, one at a time or in groups).
- Falls back to the next dimension when one can't be narrowed any further.
- `fanout="auto"` sizes each split from how far over the cap a window is, to save count calls.
- `merge=True` (default) joins neighbouring windows that together fit under the cap, so there are fewer windows to page through. It never merges across results no dimension could reach, so counts stay exact.
- `cache=` keeps counts in any dict-like store (e.g. `shelve`) so an interrupted run resumes for free.
- `run.stats` and `UncappedWarning` report windows still over the cap and results no dimension could reach.
