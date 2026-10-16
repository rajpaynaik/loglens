"""Command-line interface: ``loglens index | lint | counts | spikes | history | serve``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .backends import TimeRange, from_config
from .config import Config, load_config
from .git import site_history
from .index import DEFAULT_EXCLUDES, index_project
from .lint import RULES, describe_trend, is_spike, lint_paths, lint_volume


def _sites(cfg: Config, path: str | None):
    root = Path(path).resolve() if path else cfg.root
    return index_project(root, DEFAULT_EXCLUDES | set(cfg.excludes))


def _backend_or_exit(cfg: Config):
    backend = from_config(cfg.backend)
    if backend is None:
        sys.exit("loglens: no [backend] configured in loglens.toml or [tool.loglens]")
    return backend


def cmd_index(args: argparse.Namespace, cfg: Config) -> int:
    sites = _sites(cfg, args.path)
    if args.json:
        json.dump([asdict(s) for s in sites], sys.stdout, indent=2)
        print()
    else:
        for s in sites:
            print(f"{s.id:<50} {s.level:<9} {s.template!r}")
        print(f"\n{len(sites)} log call(s)", file=sys.stderr)
    return 0


def cmd_lint(args: argparse.Namespace, cfg: Config) -> int:
    diags = lint_paths(args.paths or [cfg.root], cfg.lint, root=cfg.root)
    if args.volume:
        backend = _backend_or_exit(cfg)
        sites = _sites(cfg, None)
        current = TimeRange.last(cfg.window_hours)
        counts = backend.count_by_line(sites, current)
        previous = (
            backend.count_by_line(sites, current.previous()) if cfg.lint.enabled("LL102") else None
        )
        diags += lint_volume(
            sites, counts, cfg.lint, window=f"{cfg.window_hours:g}h", previous=previous
        )
    diags.sort(key=lambda d: (d.path, d.line, d.code))
    if args.format == "json":
        json.dump([asdict(d) for d in diags], sys.stdout, indent=2)
        print()
    else:
        for d in diags:
            print(d.format())
        if diags:
            print(f"\nFound {len(diags)} issue(s).", file=sys.stderr)
    return 1 if diags else 0


def cmd_counts(args: argparse.Namespace, cfg: Config) -> int:
    backend = _backend_or_exit(cfg)
    sites = _sites(cfg, args.path)
    hours = args.hours or cfg.window_hours
    counts = backend.count_by_line(sites, TimeRange.last(hours))
    by_id = {s.id: s for s in sites}
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[: args.top]
    for site_id, n in ranked:
        site = by_id[site_id]
        print(f"{n:>10,}  {site_id:<50} {site.template!r}")
    if not ranked:
        print(f"no matching logs in the last {hours:g}h", file=sys.stderr)
    return 0


def cmd_spikes(args: argparse.Namespace, cfg: Config) -> int:
    backend = _backend_or_exit(cfg)
    sites = _sites(cfg, None)  # site paths must be relative to the root for git
    hours = args.hours or cfg.window_hours
    if args.ratio is not None:
        cfg.lint.spike_ratio = args.ratio
    if args.min_count is not None:
        cfg.lint.spike_min_count = args.min_count
    current = TimeRange.last(hours)
    now = backend.count_by_line(sites, current)
    before = backend.count_by_line(sites, current.previous())
    spikes = [(sid, n, before.get(sid, 0)) for sid, n in now.items()]
    spikes = [t for t in spikes if is_spike(t[1], t[2], cfg.lint)]
    spikes.sort(key=lambda t: t[1] / max(t[2], 1), reverse=True)
    by_id = {s.id: s for s in sites}
    for i, (sid, n, prev) in enumerate(spikes):
        site = by_id[sid]
        if i:
            print()
        print(f"{sid}  {prev:,} -> {n:,} ({describe_trend(n, prev)})  {site.template!r}")
        if args.commits:
            # Commits since the start of the previous window are the likely causes.
            commits = site_history(
                cfg.root,
                site.path,
                site.line,
                since=current.previous().start,
                max_count=args.commits,
            )
            for c in commits:
                print(f"    {c.format()}")
            if not commits:
                print(f"    no commits touched this code in the last {2 * hours:g}h")
    if not spikes:
        print(f"no spikes in the last {hours:g}h vs the {hours:g}h before", file=sys.stderr)
    return 0


def cmd_history(args: argparse.Namespace, cfg: Config) -> int:
    path, _, line = args.target.rpartition(":")
    if not path or not line.isdigit():
        sys.exit("loglens: expected PATH:LINE, e.g. app/checkout.py:42")
    try:
        rel = Path(path).resolve().relative_to(cfg.root)
    except ValueError:
        sys.exit(f"loglens: {path} is outside the project root {cfg.root}")
    commits = site_history(cfg.root, rel.as_posix(), int(line), max_count=args.max)
    for c in commits:
        print(c.format())
    if not commits:
        print(
            f"no git history for {args.target} (not a git repo, or not committed?)", file=sys.stderr
        )
    return 0


def cmd_rules(args: argparse.Namespace, cfg: Config) -> int:
    for code, desc in RULES.items():
        print(f"{code}  {desc}")
    return 0


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    try:
        from .server import serve
    except ImportError:
        sys.exit("loglens: the language server needs extra deps: pip install 'loglens[lsp]'")
    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loglens", description=__doc__)
    parser.add_argument("--version", action="version", version=f"loglens {__version__}")
    parser.add_argument("--config-dir", help="directory to search for loglens.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="list every log call in the project")
    p.add_argument("path", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("lint", help="check log quality")
    p.add_argument("paths", nargs="*")
    p.add_argument("--volume", action="store_true", help="also run backend volume rules")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("counts", help="show production log counts per line")
    p.add_argument("path", nargs="?")
    p.add_argument("--hours", type=float)
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_counts)

    p = sub.add_parser("spikes", help="lines that spiked vs the previous window, with commits")
    p.add_argument("--hours", type=float)
    p.add_argument("--ratio", type=float, help="minimum growth factor (default: lint.spike_ratio)")
    p.add_argument("--min-count", type=int, help="minimum emits (default: lint.spike_min_count)")
    p.add_argument("--commits", type=int, default=3, help="commits to show per line (0: none)")
    p.set_defaults(func=cmd_spikes)

    p = sub.add_parser("history", help="commits that touched the code around a log line")
    p.add_argument("target", metavar="PATH:LINE")
    p.add_argument("--max", type=int, default=10)
    p.set_defaults(func=cmd_history)

    sub.add_parser("rules", help="list lint rules").set_defaults(func=cmd_rules)
    sub.add_parser("serve", help="run the language server on stdio").set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    cfg = load_config(args.config_dir)
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
