"""Map production log records back to the log call that produced them.

Strategy, most reliable first:

1. Structured fields: if the record carries ``pathname``/``filename`` + ``lineno``
   (Python's ``logging`` provides both for free), match on file suffix + line.
2. Template matching: turn each site's message template into a regex and pick the
   matching site with the most literal text, so ``"%s"`` never beats ``"user %s logged in"``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .models import LogRecord, LogSite


class Matcher:
    def __init__(self, sites: Iterable[LogSite]) -> None:
        self.sites = list(sites)
        self._by_basename_line: dict[tuple[str, int], list[LogSite]] = defaultdict(list)
        for site in self.sites:
            basename = site.path.rsplit("/", 1)[-1]
            self._by_basename_line[(basename, site.line)].append(site)

        compiled = [(s, s.template_regex()) for s in self.sites]
        # Longest literal text first: the first regex that matches is the most specific.
        self._templates = sorted(
            ((s, rx) for s, rx in compiled if rx is not None),
            key=lambda pair: pair[0].literal_len,
            reverse=True,
        )

    def match(self, record: LogRecord) -> LogSite | None:
        site = self._match_structured(record)
        if site is not None:
            return site
        return self._match_template(record.message)

    def _match_structured(self, record: LogRecord) -> LogSite | None:
        src, line = record.source_file, record.source_line
        if not src or line is None:
            return None
        return self.match_location(src, line)

    def match_location(self, src: str, line: int) -> LogSite | None:
        """The site at ``line`` of the file whose path ends like ``src`` (any OS separators)."""
        src = src.replace("\\", "/")
        candidates = self._by_basename_line.get((src.rsplit("/", 1)[-1], line), [])
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        # Several files share a basename: prefer the longest matching path suffix.
        return max(candidates, key=lambda s: _common_suffix_len(s.path, src))

    def _match_template(self, message: str) -> LogSite | None:
        for site, rx in self._templates:
            if rx.search(message):
                return site
        return None


def sample_messages(
    sites: Iterable[LogSite], records: Iterable[LogRecord], per_site: int = 50
) -> dict[str, list[str]]:
    """Up to ``per_site`` rendered messages for each site, from a batch of records."""
    matcher = Matcher(sites)
    out: dict[str, list[str]] = {}
    for record in records:
        site = matcher.match(record)
        if site is not None:
            messages = out.setdefault(site.id, [])
            if len(messages) < per_site:
                messages.append(record.message)
    return out


def _common_suffix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(reversed(a), reversed(b), strict=False):
        if x != y:
            break
        n += 1
    return n
