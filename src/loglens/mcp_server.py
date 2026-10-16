"""MCP server: lets AI assistants query the log index, lint results, production counts,
spikes and git history, e.g. "which log lines spiked after the last deploy?"

Run it with ``loglens mcp`` (stdio). Every tool is read-only.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .backends import LogBackend, TimeRange, from_config
from .config import Config
from .git import Commit, parse_target, site_history
from .index import DEFAULT_EXCLUDES, index_project
from .lint import RULES, describe_trend, find_spikes, lint_paths, lint_volume
from .models import LogSite

INSTRUCTIONS = """\
loglens maps production logs back to the Python log calls that emitted them.
Log calls are identified as "PATH:LINE" (PATH relative to the project root).
Start with `spikes` to find lines whose volume jumped, then `log_history` on one of them
to see which commits changed the surrounding code. `lint` finds log-quality problems
(rules: `list_rules`). Tools that need production data fail if no backend is configured.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True)


def _commit(c: Commit) -> dict[str, str]:
    return {"sha": c.sha, "author": c.author, "date": c.date.isoformat(), "summary": c.summary}


def _site(s: LogSite) -> dict[str, Any]:
    return {"id": s.id, "level": s.level, "template": s.template, "logger": s.logger}


def build_server(cfg: Config) -> MCPServer:
    mcp = MCPServer("loglens", version=__version__, instructions=INSTRUCTIONS)
    window_default = cfg.window_hours

    def sites() -> list[LogSite]:
        # Re-indexed on every call so answers follow the code as it changes.
        return index_project(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))

    def backend() -> LogBackend:
        try:
            b = from_config(cfg.backend)
        except (ValueError, RuntimeError) as exc:
            raise ToolError(f"bad [backend] config: {exc}") from exc
        if b is None:
            raise ToolError("no [backend] configured in loglens.toml or [tool.loglens]")
        return b

    def counts(b: LogBackend, all_sites: list[LogSite], time_range: TimeRange) -> dict[str, int]:
        try:
            return b.count_by_line(all_sites, time_range)
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc

    @mcp.tool(annotations=READ_ONLY)
    def list_log_calls(
        path_contains: str | None = None, level: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """List log calls in the codebase with their level and message template.

        Placeholders in templates are normalised: %-style stays as is, f-string and
        .format() fields become "{}". template is null when the message is built at runtime.
        """
        out = [
            _site(s)
            for s in sites()
            if (path_contains is None or path_contains in s.path)
            and (level is None or s.level == level)
        ]
        return out[:limit]

    @mcp.tool(annotations=READ_ONLY)
    def list_rules() -> dict[str, str]:
        """loglens lint rules: LL0xx are static, LL1xx need production counts."""
        return dict(RULES)

    @mcp.tool(annotations=READ_ONLY)
    def lint(paths: list[str] | None = None, volume: bool = False) -> list[dict[str, Any]]:
        """Run the log-quality linter.

        paths are relative to the project root (default: the whole project). volume=True also
        runs the rules that need production counts (noisy, never emitted, spiked).
        """
        targets = [cfg.root / p for p in paths] if paths else [cfg.root]
        for t in targets:
            if not t.resolve().is_relative_to(cfg.root):
                raise ToolError(f"{t} is outside the project root")
        diags = lint_paths(targets, cfg.lint, root=cfg.root)
        if volume:
            b, all_sites = backend(), sites()
            current = TimeRange.last(window_default)
            now = counts(b, all_sites, current)
            before = counts(b, all_sites, current.previous()) if cfg.lint.enabled("LL102") else None
            diags += lint_volume(
                all_sites, now, cfg.lint, window=f"{window_default:g}h", previous=before
            )
        diags.sort(key=lambda d: (d.path, d.line, d.code))
        return [asdict(d) for d in diags]

    @mcp.tool(annotations=READ_ONLY)
    def log_counts(hours: float | None = None, top: int = 20) -> list[dict[str, Any]]:
        """The log calls that emitted the most lines in production over the last `hours`."""
        all_sites = sites()
        by_id = {s.id: s for s in all_sites}
        now = counts(backend(), all_sites, TimeRange.last(hours or window_default))
        ranked = sorted(now.items(), key=lambda kv: kv[1], reverse=True)[:top]
        return [{**_site(by_id[sid]), "count": n} for sid, n in ranked]

    @mcp.tool(annotations=READ_ONLY)
    def spikes(
        hours: float | None = None,
        ratio: float | None = None,
        min_count: int | None = None,
        commits: int = 3,
    ) -> list[dict[str, Any]]:
        """Log calls whose volume over the last `hours` grew by at least `ratio`x compared
        with the `hours` before, with the commits that touched the surrounding function in
        that time (the likely causes). Defaults come from the project's loglens config.
        """
        lint_cfg = replace(
            cfg.lint,
            spike_ratio=cfg.lint.spike_ratio if ratio is None else ratio,
            spike_min_count=cfg.lint.spike_min_count if min_count is None else min_count,
        )
        b, all_sites = backend(), sites()
        by_id = {s.id: s for s in all_sites}
        current = TimeRange.last(hours or window_default)
        now = counts(b, all_sites, current)
        before = counts(b, all_sites, current.previous())
        out = []
        for sid, n, prev in find_spikes(now, before, lint_cfg):
            site = by_id[sid]
            entry = {**_site(site), "count": n, "previous": prev, "trend": describe_trend(n, prev)}
            if commits:
                history = site_history(
                    cfg.root,
                    site.path,
                    site.line,
                    since=current.previous().start,
                    max_count=commits,
                )
                entry["commits"] = [_commit(c) for c in history]
            out.append(entry)
        return out

    @mcp.tool(annotations=READ_ONLY)
    def log_history(target: str, max_count: int = 10) -> list[dict[str, str]]:
        """Commits (newest first) that changed the function around a log call.

        target is "PATH:LINE" with PATH relative to the project root, e.g. "app/checkout.py:42".
        """
        try:
            path, line = parse_target(target, cfg.root, base=cfg.root)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return [_commit(c) for c in site_history(cfg.root, path, line, max_count=max_count)]

    return mcp


def serve(cfg: Config) -> None:
    build_server(cfg).run("stdio")
