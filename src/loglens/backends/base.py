"""The one interface every backend implements. Nothing else in loglens knows the vendor."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..matching import Matcher
from ..models import LogRecord, LogSite


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime

    @classmethod
    def last(cls, hours: float = 24) -> TimeRange:
        end = datetime.now(timezone.utc)
        return cls(start=end - timedelta(hours=hours), end=end)

    def previous(self) -> TimeRange:
        """The range of the same length that ends where this one starts."""
        return TimeRange(start=self.start - (self.end - self.start), end=self.start)


class LogBackend(ABC):
    """Adapter for a log store (Loki, OpenSearch, CloudWatch, a local file, ...)."""

    name: str = "base"

    @abstractmethod
    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        """Return raw log records in ``time_range``, optionally filtered by substring."""

    def count_by_line(
        self, sites: Iterable[LogSite], time_range: TimeRange, limit: int = 5000
    ) -> dict[str, int]:
        """Return ``{site.id: count}`` for the given sites.

        The default implementation fetches records and matches them client-side. Backends
        that can aggregate server-side (e.g. on a ``lineno`` label) should override this.
        """
        return tally(Matcher(sites), {}, self.query_logs(time_range, limit=limit))


def tally(
    matcher: Matcher,
    locations: Mapping[tuple[str, int], int],
    records: Iterable[LogRecord] = (),
) -> dict[str, int]:
    """Turn server-side ``{(file, line): count}`` plus raw records into ``{site.id: count}``."""
    counts: Counter[str] = Counter()
    for (src, line), n in locations.items():
        site = matcher.match_location(src, line)
        if site is not None:
            counts[site.id] += n
    for record in records:
        site = matcher.match(record)
        if site is not None:
            counts[site.id] += 1
    return dict(counts)


def parse_line(raw: str, timestamp: datetime | None = None) -> LogRecord:
    """Parse a log line: JSON objects become structured records, anything else is plain text."""
    raw = raw.rstrip("\n")
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            message = next(
                (str(data[k]) for k in ("message", "msg", "event", "log") if k in data), raw
            )
            ts = timestamp or _parse_ts(data.get("timestamp") or data.get("time") or data.get("ts"))
            return LogRecord(timestamp=ts, message=message, fields=data)
    return LogRecord(timestamp=timestamp, message=raw)


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
