"""Command-line interface: ``loglens index | lint | counts | serve``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .backends import TimeRange, from_config
from .config import Config, load_config
from .index import DEFAULT_EXCLUDES, index_project
from .lint import RULES, lint_paths, lint_volume


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
        counts = backend.count_by_line(sites, TimeRange.last(cfg.window_hours))
        diags += lint_volume(sites, counts, cfg.lint, window=f"{cfg.window_hours:g}h")
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

    sub.add_parser("rules", help="list lint rules").set_defaults(func=cmd_rules)
    sub.add_parser("serve", help="run the language server on stdio").set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    cfg = load_config(args.config_dir)
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
