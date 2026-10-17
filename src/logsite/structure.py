"""Structure score: how much of a codebase (and its production volume) logs a constant
message with values in fields, rather than values formatted into the text."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .index import DEFAULT_EXCLUDES, index_calls, iter_python_files
from .lint import _message_arg, interpolated_values
from .models import LogSite


def is_structured(site: LogSite, call: ast.Call) -> bool:
    """A literal message with nothing formatted into it (values, if any, go in extra=)."""
    msg = _message_arg(site, call)
    return site.template is not None and (msg is None or not interpolated_values(site, call, msg))


@dataclass
class Score:
    structured: int = 0
    total: int = 0

    @property
    def ratio(self) -> float:
        return self.structured / self.total if self.total else 1.0


@dataclass
class StructureReport:
    overall: Score = field(default_factory=Score)
    by_area: dict[str, Score] = field(default_factory=dict)  # top-level directory -> score
    unstructured: list[LogSite] = field(default_factory=list)
    structured_ids: set[str] = field(default_factory=set)


def area(path: str) -> str:
    """``app/api/users.py`` -> ``app/``; files at the root are grouped as ``./``."""
    head, sep, _ = path.partition("/")
    return f"{head}/" if sep else "./"


def score_project(root: str | Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> StructureReport:
    root = Path(root).resolve()
    report = StructureReport()
    for file in iter_python_files(root, excludes):
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = file.relative_to(root).as_posix()
        for site, call in index_calls(text, rel):
            ok = is_structured(site, call)
            for score in (report.overall, report.by_area.setdefault(area(rel), Score())):
                score.total += 1
                score.structured += ok
            if ok:
                report.structured_ids.add(site.id)
            else:
                report.unstructured.append(site)
    return report


def production_score(report: StructureReport, counts: Mapping[str, int]) -> Score:
    """Share of production log lines that come from structured calls."""
    return Score(
        structured=sum(n for sid, n in counts.items() if sid in report.structured_ids),
        total=sum(counts.values()),
    )
