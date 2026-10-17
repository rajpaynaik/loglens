"""The one interface every backend implements. Nothing else in loglens knows the vendor."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..matching import Matcher
from ..models import LogRecord, LogSite, Usage, parse_iso


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

    def usage_by_line(
        self, sites: Iterable[LogSite], time_range: TimeRange, limit: int = 5000
    ) -> dict[str, Usage]:
        """Return ``{site.id: Usage(count, bytes)}``.

        The default measures the records it fetches, so it's exact but shares the ``limit``
        cap of :meth:`count_by_line`. Backends that aggregate server-side override this.
        """
        return measure(Matcher(sites), self.query_logs(time_range, limit=limit))


def measure(matcher: Matcher, records: Iterable[LogRecord]) -> dict[str, Usage]:
    """Count and sum the stored size of records per matched site."""
    usage: dict[str, Usage] = {}
    for record in records:
        site = matcher.match(record)
        if site is None:
            continue
        u = usage.setdefault(site.id, Usage())
        u.count += 1
        u.bytes += record.size if record.size is not None else len(record.message.encode())
    return usage


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
        if isinstance(data, dict) and isinstance(data.get("record"), dict):
            return _loguru_record(data["record"], raw, timestamp)
        if isinstance(data, dict):
            message = next(
                (str(data[k]) for k in ("message", "msg", "event", "log") if k in data), raw
            )
            ts = timestamp or _parse_ts(data.get("timestamp") or data.get("time") or data.get("ts"))
            return LogRecord(timestamp=ts, message=message, fields=data, size=_size(raw))
    return LogRecord(timestamp=timestamp, message=raw, size=_size(raw))


def _loguru_record(rec: dict, raw: str, timestamp: datetime | None) -> LogRecord:
    """loguru's ``serialize=True`` output: ``{"text": ..., "record": {"message", "file",
    "line", "level", "extra", "time", ...}}``. Flattened so file/line matching and context
    checks see the same fields as any other JSON log."""
    file = rec.get("file") if isinstance(rec.get("file"), dict) else {}
    level = rec.get("level") if isinstance(rec.get("level"), dict) else {}
    time = rec.get("time") if isinstance(rec.get("time"), dict) else {}
    extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
    fields = {
        **extra,
        "pathname": file.get("path") or file.get("name"),
        "filename": file.get("name"),
        "lineno": rec.get("line"),
        "levelname": level.get("name"),
        "funcName": rec.get("function"),
        "name": rec.get("name"),
    }
    ts = timestamp or _parse_ts(time.get("timestamp") or time.get("repr"))
    return LogRecord(
        timestamp=ts,
        message=str(rec.get("message", "")),
        fields={k: v for k, v in fields.items() if v is not None},
        size=_size(raw),
    )


def _size(raw: str) -> int:
    return len(raw.encode("utf-8", errors="replace"))


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            return None
    return None
