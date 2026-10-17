"""Core data types shared by every loglens module."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Matches %-style placeholders (%s, %d, %(name)s, %.2f, %-5r ...) and str.format / f-string
# placeholders that the indexer normalises to "{}".
_PLACEHOLDER = re.compile(r"%(?:\([^)]*\))?[#0\- +]*\d*(?:\.\d+)?[sdifrxXeEgGcoau%]|\{\}")


def parse_iso(value: str) -> datetime:
    """``datetime.fromisoformat`` that also accepts a trailing ``Z`` on Python 3.10.

    Git >= 2.5x prints UTC dates as ``...Z``; 3.10's parser only knows ``+00:00``.
    """
    value = value.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class LogSite:
    """One log call in source code, e.g. ``logger.info("user %s logged in", uid)``."""

    path: str  # path relative to the project root, POSIX separators
    line: int  # 1-based
    col: int  # 0-based
    end_line: int
    end_col: int
    level: str  # debug | info | warning | error | critical | exception | log
    template: str | None  # message template, placeholders normalised; None if dynamic
    logger: str  # source text of the receiver, e.g. "logger", "self.log"
    in_except: bool = False
    in_loop: bool = False

    @property
    def id(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def literal_len(self) -> int:
        """Characters of fixed text in the template; used to rank competing matches."""
        if not self.template:
            return 0
        return len(_PLACEHOLDER.sub("", self.template))

    def template_regex(self) -> re.Pattern[str] | None:
        """Regex that matches a rendered message produced by this site.

        Not anchored at the start, because formatters usually prefix a timestamp/level.
        """
        if not self.template or self.literal_len == 0:
            return None
        parts: list[str] = []
        pos = 0
        for m in _PLACEHOLDER.finditer(self.template):
            parts.append(re.escape(self.template[pos : m.start()]))
            parts.append("%" if m.group() == "%%" else r".*?")
            pos = m.end()
        parts.append(re.escape(self.template[pos:]))
        return re.compile("".join(parts) + r"\s*$")


@dataclass
class LogRecord:
    """A single log event returned by a backend."""

    timestamp: datetime | None
    message: str
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def source_file(self) -> str | None:
        # Python logging, OpenTelemetry (code.*) and Elastic Common Schema (log.origin.*)
        for key in (
            "pathname",
            "filename",
            "file",
            "code.filepath",
            "log.origin.file.name",
            "source_file",
        ):
            value = self.fields.get(key)
            if value:
                return str(value)
        return None

    @property
    def source_line(self) -> int | None:
        for key in ("lineno", "line", "code.lineno", "log.origin.file.line", "source_line"):
            value = self.fields.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None


@dataclass(frozen=True)
class Diagnostic:
    """A log-quality finding."""

    code: str  # e.g. "LL001"
    message: str
    path: str
    line: int
    col: int
    end_line: int
    end_col: int
    severity: str = "warning"  # error | warning | info | hint

    def format(self) -> str:
        return f"{self.path}:{self.line}:{self.col + 1}: {self.code} {self.message}"
