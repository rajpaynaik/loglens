"""Git correlation: which commits touched the code around a log line (``git log -L``).

When a line spikes in production, the commits that recently changed the function it lives in
are the first suspects. Everything here shells out to ``git``; no extra dependencies.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_SEP = "\x1f"
_FORMAT = _SEP.join(["%H", "%an", "%aI", "%s"])


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    date: datetime
    summary: str

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    def format(self) -> str:
        return f"{self.short_sha}  {self.date:%Y-%m-%d}  {self.author}: {self.summary}"


def enclosing_block(source: str, line: int) -> tuple[int, int]:
    """Return the line range of the innermost function containing ``line``.

    A log line usually spikes because the code that calls it changed, not the log call
    itself, so the whole function is the useful unit. Falls back to the line alone.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return line, line
    best: tuple[int, int] = (line, line)
    best_size: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno or node.lineno
            if start <= line <= end and (best_size is None or end - start < best_size):
                best, best_size = (start, end), end - start
    return best


def parse_log(output: str) -> list[Commit]:
    """Parse ``git log --format=_FORMAT`` output (one commit per line)."""
    commits: list[Commit] = []
    for raw in output.splitlines():
        parts = raw.split(_SEP)
        if len(parts) != 4:
            continue  # blank separators or diff output
        sha, author, date, summary = parts
        try:
            when = datetime.fromisoformat(date)
        except ValueError:
            continue
        commits.append(Commit(sha=sha, author=author, date=when, summary=summary))
    return commits


def line_history(
    root: str | Path,
    path: str,
    start: int,
    end: int | None = None,
    since: datetime | None = None,
    max_count: int = 5,
) -> list[Commit]:
    """Commits (newest first) that touched lines ``start..end`` of ``path`` at ``HEAD``.

    Line numbers refer to ``HEAD``; uncommitted edits above the range shift it. Returns an
    empty list if ``root`` isn't a git repo, the file isn't committed, or git is missing.
    """
    cmd = [
        "git",
        "-C",
        str(root),
        "log",
        f"-L{start},{end or start}:{path}",
        "--no-patch",
        f"--format={_FORMAT}",
        f"--max-count={max_count}",
    ]
    if since is not None:
        cmd.append(f"--since={since.isoformat()}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return parse_log(proc.stdout)


def parse_target(target: str, root: str | Path, base: str | Path | None = None) -> tuple[str, int]:
    """Turn ``PATH:LINE`` into a root-relative ``(path, line)``.

    A relative PATH is resolved against ``base`` (default: the current directory).

    Raises ValueError with a user-facing message.
    """
    path, _, line = target.rpartition(":")
    if not path or not line.isdigit():
        raise ValueError(f"expected PATH:LINE, e.g. app/checkout.py:42, got {target!r}")
    try:
        rel = (Path(base or Path.cwd()) / path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        raise ValueError(f"{path} is outside the project root {root}") from None
    return rel.as_posix(), int(line)


def site_history(
    root: str | Path,
    path: str,
    line: int,
    since: datetime | None = None,
    max_count: int = 5,
) -> list[Commit]:
    """Commits that touched the function around the log call at ``path:line``."""
    try:
        source = (Path(root) / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    start, end = enclosing_block(source, line)
    return line_history(root, path, start, end, since=since, max_count=max_count)
