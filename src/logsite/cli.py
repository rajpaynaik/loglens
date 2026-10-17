"""Command-line interface: ``logsite index | lint | context | structure | counts | cost | spikes |
errors | history | trace | leaks | serve | mcp``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from . import __version__
from .backends import TimeRange, from_config
from .config import Config, load_config
from .context import detect_injected, measure_coverage, overall_coverage, satisfied
from .cost import format_bytes, format_money, monthly_costs
from .deploys import compare_deploy, resolve_deploy
from .errors import AppFiles, ErrorGroup, error_window, summarize_errors, summary_json
from .git import parse_target, site_history
from .index import DEFAULT_EXCLUDES, index_project
from .leaks import KINDS, find_leaks
from .lint import RULES, SITUATIONS, describe_trend, find_spikes, lint_paths, lint_volume
from .matching import sample_records
from .models import parse_iso
from .structure import production_score, score_project
from .timeline import find_timeline, timeline_json


def _sites(cfg: Config, path: str | None):
    root = Path(path).resolve() if path else cfg.root
    return index_project(root, DEFAULT_EXCLUDES | set(cfg.excludes))


def _backend_or_exit(cfg: Config, settings: dict | None = None):
    try:
        backend = from_config(cfg.backend if settings is None else settings)
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"logsite: bad [backend] config: {exc}")
    if backend is None:
        sys.exit("logsite: no [backend] configured in logsite.toml or [tool.logsite]")
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
        samples = None
        if cfg.lint.needs_samples():
            samples = sample_records(sites, backend.query_logs(current))
        diags += lint_volume(
            sites,
            counts,
            cfg.lint,
            window=f"{cfg.window_hours:g}h",
            previous=previous,
            samples=samples,
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


def cmd_context(args: argparse.Namespace, cfg: Config) -> int:
    rules = cfg.lint.context
    rules.detected = detect_injected(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
    print("Required fields ([context] in logsite.toml):")
    required = {"all": [*cfg.lint.required_fields, *rules.all]} | {
        k: getattr(rules, k) for k in SITUATIONS if k != "all"
    }
    for situation, fields in required.items():
        print(f"  {SITUATIONS[situation]:<17} {', '.join(fields) or '-'}")
    print("\nAdded to every log automatically:")
    for f in sorted(rules.injected):
        print(f"  {f:<17} declared in [context] injected")
    for f, where in sorted(rules.detected.items()):
        print(f"  {f:<17} {where[0]}" + (f" (+{len(where) - 1} more)" if len(where) > 1 else ""))
    if not (rules.injected or rules.detected):
        print("  nothing found (logging filters, structlog contextvars, OpenTelemetry)")

    # Only LL006, and reuse the scan above instead of detecting again.
    only = replace(cfg.lint, select={"LL006"}, ignore=set(), context=replace(rules, detect=False))
    diags = lint_paths([cfg.root], only, root=cfg.root)
    sites = _sites(cfg, None)
    missing: dict[str, int] = {}
    for d in diags:
        for part in d.message.removeprefix("missing context: ").split(";")[0].split(", "):
            missing[part] = missing.get(part, 0) + 1
    print(f"\n{len(diags)} of {len(sites)} log call(s) are missing context:")
    for part, n in sorted(missing.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>5}  {part}")
    if diags and not args.quiet:
        print("\n`logsite lint` lists each call site (LL006)")
    if args.production:
        return _production_coverage(args, cfg, sites) or (1 if diags else 0)
    return 1 if diags else 0


def _production_coverage(args: argparse.Namespace, cfg: Config, sites) -> int:
    backend = _backend_or_exit(cfg)
    hours = args.hours or cfg.window_hours
    rules = cfg.lint.context
    samples = sample_records(sites, backend.query_logs(TimeRange.last(hours), limit=args.limit))
    per_site = measure_coverage(sites, samples, cfg.lint.required_context)
    scanned = sum(len(v) for v in samples.values())
    print(f"\nIn production (last {hours:g}h, {scanned:,} sampled record(s)):")
    if not per_site:
        print("  no sampled records for log calls that require context")
        return 0
    for c in overall_coverage(per_site):
        auto = " (added automatically)" if satisfied(c.field, rules.available()) else ""
        print(f"  {c.field:<17} {c.ratio:>5.0%} of {c.total:,} record(s){auto}")
    gaps = sorted(
        (
            (sid, c)
            for sid, cov in per_site.items()
            for c in cov
            if c.ratio < rules.min_coverage and c.total >= rules.min_records
        ),
        key=lambda t: (t[1].ratio, -t[1].total),
    )
    if gaps:
        print(f"\nLog calls below {rules.min_coverage:.0%} (with >= {rules.min_records} records):")
        for sid, c in gaps[: args.top]:
            print(f"  {c.ratio:>5.0%}  {c.field:<15} {sid}  ({c.present}/{c.total})")
    return 1 if gaps else 0


def cmd_structure(args: argparse.Namespace, cfg: Config) -> int:
    report = score_project(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
    o = report.overall
    print(
        f"Structure score: {o.ratio:.0%} of log calls keep a constant message "
        f"({o.structured}/{o.total})"
    )
    for name, s in sorted(report.by_area.items(), key=lambda kv: kv[1].ratio):
        print(f"  {name:<24} {s.ratio:>5.0%}  ({s.structured}/{s.total})")
    if args.production:
        backend = _backend_or_exit(cfg)
        hours = args.hours or cfg.window_hours
        counts = backend.count_by_line(_sites(cfg, None), TimeRange.last(hours))
        p = production_score(report, counts)
        print(
            f"\nIn production (last {hours:g}h): {p.ratio:.0%} of {p.total:,} log line(s) come "
            "from constant-message calls"
        )
        worst = sorted(
            ((counts.get(s.id, 0), s) for s in report.unstructured if counts.get(s.id)),
            key=lambda t: -t[0],
        )[: args.top]
        if worst:
            print("Biggest unstructured lines by volume:")
            for n, site in worst:
                print(f"  {n:>10,}  {site.id}  {site.template!r}")
    if report.unstructured and not args.production:
        print("\nenable LL008 (extend_select) to flag each one; the editor can fix them")
    return 0


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


def cmd_cost(args: argparse.Namespace, cfg: Config) -> int:
    backend = _backend_or_exit(cfg)
    sites = _sites(cfg, None)
    hours = args.hours or cfg.window_hours
    if args.price_per_gb is not None:
        cfg.cost.price_per_gb = args.price_per_gb
    if args.price_per_million_events is not None:
        cfg.cost.price_per_million_events = args.price_per_million_events
    usage = backend.usage_by_line(sites, TimeRange.last(hours), limit=args.limit)
    costs = monthly_costs(sites, usage, hours, cfg.cost)
    total_cost = sum(c.cost_per_month for c in costs)
    total_bytes = sum(c.bytes_per_month for c in costs)
    cur = cfg.cost.currency
    if args.format == "json":
        rows = [
            {
                "id": c.site.id,
                "template": c.site.template,
                "level": c.site.level,
                "events_per_month": round(c.events_per_month),
                "bytes_per_month": round(c.bytes_per_month),
                "cost_per_month": round(c.cost_per_month, 2),
                "avg_bytes": round(c.avg_bytes),
                "estimated": c.estimated,
            }
            for c in costs[: args.top]
        ]
        json.dump(
            {
                "currency": cur,
                "priced": cfg.cost.priced,
                "total_cost_per_month": round(total_cost, 2),
                "total_bytes_per_month": round(total_bytes),
                "lines": rows,
            },
            sys.stdout,
            indent=2,
        )
        print()
        return 0
    if not costs:
        print(f"no matching logs in the last {hours:g}h", file=sys.stderr)
        return 0
    print(f"{'per month':>10}  {'volume':>9}  {'events':>7}  {'avg':>7}  line")
    for c in costs[: args.top]:
        price = format_money(c.cost_per_month, cur) if cfg.cost.priced else "-"
        flag = "~" if c.estimated else " "
        print(
            f"{price:>10}  {format_bytes(c.bytes_per_month):>9}  "
            f"{_humanize(c.events_per_month):>7}  {format_bytes(c.avg_bytes):>7}{flag} "
            f"{c.site.id}  {c.site.template!r}"
        )
    sys.stdout.flush()  # keep the table above the stderr summary
    top = costs[: args.top]
    share = sum(c.bytes_per_month for c in top) / total_bytes if total_bytes else 0
    total = format_money(total_cost, cur) + "/month, " if cfg.cost.priced else ""
    print(
        f"\n{total}{format_bytes(total_bytes)}/month across {len(costs)} log call(s); "
        f"the top {len(top)} write {share:.0%} of the volume (projected from the last {hours:g}h)",
        file=sys.stderr,
    )
    if any(c.estimated for c in top):
        print("~ bytes estimated from a sample of each line's records", file=sys.stderr)
    if not cfg.cost.priced:
        print(
            "set [cost] price_per_gb (or --price-per-gb) to see spend, e.g. 0.50 for "
            "CloudWatch Logs ingestion",
            file=sys.stderr,
        )
    return 0


def _humanize(n: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def cmd_trace(args: argparse.Namespace, cfg: Config) -> int:
    tcfg = cfg.timeline
    backend = _backend_or_exit(cfg, tcfg.backend or None)
    field_name = args.field or tcfg.field_name
    hours = args.hours or cfg.window_hours
    tl = find_timeline(
        backend,
        _sites(cfg, None),
        field_name,
        args.value,
        TimeRange.last(hours),
        limit=args.limit,
        here=tcfg.service,
        leaks=cfg.leaks,
    )
    if args.format == "json":
        json.dump(timeline_json(tl), sys.stdout, indent=2)
        print()
        return 0 if tl.entries else 1
    if not tl.entries:
        print(f"no logs with {field_name}={args.value} in the last {hours:g}h", file=sys.stderr)
        return 1
    took = f" over {tl.duration.total_seconds():.3f}s" if tl.duration else ""
    print(
        f"{field_name}={args.value}: {len(tl.entries)} line(s) from "
        f"{len(tl.services)} service(s){took}\n"
    )
    start = next((e.timestamp for e in tl.entries if e.timestamp), None)
    width = max(len(e.service or "?") for e in tl.entries)
    for e in tl.entries:
        offset = (
            f"+{(e.timestamp - start).total_seconds() * 1000:>8.1f}ms"
            if start and e.timestamp
            else " " * 11
        )
        mark = "!" if e.is_error else ("~" if e.is_problem else " ")
        where = f"  {e.site.id}" if e.site else ""
        print(
            f"{offset} {mark} {(e.service or '?'):<{width}}  {(e.level or '-'):<7} "
            f"{e.message[:120]}{where}"
        )
    sys.stdout.flush()
    summary = []
    if tl.first_problem is not None and tl.first_problem is not tl.first_error:
        summary.append(("first problem", tl.first_problem))  # a warning before the failure
    if tl.first_error is not None:
        summary.append(("first error", tl.first_error))
    for i, (label, e) in enumerate(summary):
        where = f" at {e.site.id}" if e.site else ""
        print(
            f"{'' if i else chr(10)}{label}: {e.service or '?'}{where} ({e.level}): "
            f"{e.message[:120]}",
            file=sys.stderr,
        )
    return 0


def cmd_leaks(args: argparse.Namespace, cfg: Config) -> int:
    backend = _backend_or_exit(cfg)
    sites = _sites(cfg, None)
    hours = args.hours or cfg.window_hours
    records = backend.query_logs(TimeRange.last(hours), limit=args.limit)
    leaks, scanned = find_leaks(sites, records, cfg.leaks)
    if args.format == "json":
        out = [
            {
                "id": e.id,
                "template": e.site.template if e.site else None,
                "records": e.records,
                "kinds": e.kinds,
                "samples": {k: asdict(f) for k, f in e.samples.items()},
            }
            for e in leaks
        ]
        json.dump({"scanned": scanned, "leaks": out}, sys.stdout, indent=2)
        print()
    else:
        for e in leaks:
            template = f"  {e.site.template!r}" if e.site else ""
            print(f"{e.id}  {e.records:,} record(s){template}")
            for kind, n in sorted(e.kinds.items(), key=lambda kv: -kv[1]):
                f = e.samples[kind]
                where = "" if f.where == "message" else f" in field {f.where!r}"
                print(f"    {KINDS[kind]:<6} {kind:<15} x{n:<6,} e.g. {f.masked}{where}")
        sys.stdout.flush()
        total = sum(e.records for e in leaks)
        capped = " (the --limit cap; raise it to scan more)" if scanned >= args.limit else ""
        print(
            f"\n{total:,} of {scanned:,} record(s) in the last {hours:g}h leaked secrets or "
            f"personal data{capped}",
            file=sys.stderr,
        )
    return 1 if leaks else 0


def cmd_spikes(args: argparse.Namespace, cfg: Config) -> int:
    backend = _backend_or_exit(cfg)
    sites = _sites(cfg, None)  # site paths must be relative to the root for git
    hours = args.hours or cfg.window_hours
    if args.ratio is not None:
        cfg.lint.spike_ratio = args.ratio
    if args.min_count is not None:
        cfg.lint.spike_min_count = args.min_count
    if args.since_deploy:
        if args.hours:
            sys.exit("logsite: --hours and --since-deploy can't be combined")
        return _deploy_spikes(args, cfg, backend, sites)
    if args.base or args.deployed_at:
        sys.exit("logsite: --base and --deployed-at need --since-deploy")
    current = TimeRange.last(hours)
    now = backend.count_by_line(sites, current)
    before = backend.count_by_line(sites, current.previous())
    spikes = find_spikes(now, before, cfg.lint)
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


def _deploy_spikes(args: argparse.Namespace, cfg: Config, backend, sites) -> int:
    try:
        at = parse_iso(args.deployed_at) if args.deployed_at else None
        deploy = resolve_deploy(cfg.root, args.since_deploy, at=at, base=args.base)
        report = compare_deploy(cfg.root, sites, backend, deploy, cfg.lint, commits=args.commits)
    except ValueError as exc:
        sys.exit(f"logsite: {exc}")
    hours = (report.after.end - report.after.start).total_seconds() / 3600
    release = f"{deploy.base}..{deploy.ref}" if deploy.base else f"{deploy.ref} (no earlier tag)"
    print(
        f"{deploy.ref} ({deploy.short_sha}) deployed {deploy.at:%Y-%m-%d %H:%M} UTC: "
        f"{hours:.1f}h after vs {hours:.1f}h before; release {release}, "
        f"{report.release_size} commit(s)"
    )
    titles = {"spike": "Spikes", "new_error": "New errors (never seen before the deploy)"}
    for kind, title in titles.items():
        changes = [c for c in report.changes if c.kind == kind]
        if not changes:
            continue
        print(f"\n{title}:")
        for c in changes:
            print(f"  {c.site.id}  {c.before:,} -> {c.after:,} ({c.trend})  {c.site.template!r}")
            for commit in c.commits:
                print(f"      {commit.format()}")
            if args.commits and not c.commits:
                print("      no commit in this release touched this code")
    if not report.changes:
        print("\nno spikes or new errors since the deploy")
    return 0


def _when(ts) -> str:
    return f"{ts:%Y-%m-%d %H:%M}" if ts else "?"


def _print_group(i: int, g: ErrorGroup, args: argparse.Namespace, cfg: Config) -> None:
    trend = "" if g.trend is None else ("new" if g.is_new else g.trend)
    print(f"{i:>2}  {g.count:>7,}  {trend:<6}  {g.title[:110]}  [{g.id}]")
    rows = []
    for cause in g.causes:
        rows.append(("caused by", cause[:110]))
    if g.raised_at:
        origin = f" (in {g.origin})" if g.origin else ""
        rows.append(("raised", f"{g.raised_at} in {g.raised_in}(){origin}"))
    elif g.origin:
        rows.append(("raised", f"in {g.origin}, no frame from this project"))
    if g.logged_at:
        rows.append(("logged", f"{g.logged_at.id}  {g.logged_at.template!r}"[:120]))
    if len(g.path) > 1:
        rows.append(("path", " → ".join(g.path)))
    seen = []
    if g.first_seen:
        seen.append(f"{_when(g.first_seen)} → {_when(g.last_seen)} UTC")
    if g.services:
        seen.append(", ".join(s for s, _ in g.services.most_common(3)))
    if seen:
        rows.append(("seen", " · ".join(seen)))
    ids = dict(g.sample.get("ids") or {})
    trace = ids.pop(cfg.timeline.field_name, None)
    if trace:
        rows.append(("example", f"logsite trace {trace}"))
    if ids:
        rows.append(("ids", " ".join(f"{k}={v}" for k, v in ids.items())))
    for label, text in rows:
        print(f"{'':>19}{label:<10}{text}")
    if args.traceback and g.sample.get("traceback"):
        for line in g.sample["traceback"].splitlines():
            print(f"{'':>19}| {line}")


def cmd_errors(args: argparse.Namespace, cfg: Config) -> int:
    backend = _backend_or_exit(cfg)
    sites = _sites(cfg, None)
    app = AppFiles.scan(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
    if args.since_deploy and args.hours:
        sys.exit("logsite: --hours and --since-deploy can't be combined")
    try:
        window, label = error_window(
            cfg.root, args.hours or cfg.window_hours, args.since_deploy, args.deployed_at
        )
    except ValueError as exc:
        sys.exit(f"logsite: {exc.args[0].replace('deployed_at', '--deployed-at')}")
    try:
        summary = summarize_errors(
            backend,
            sites,
            app,
            window,
            window.previous(),
            cfg.lint,
            leaks=cfg.leaks,
            limit=args.limit,
            sample_fields=dict.fromkeys([cfg.timeline.field_name, "trace_id", "request_id"]),
        )
    except Exception as exc:
        sys.exit(f"logsite: couldn't query the {backend.name} backend: {exc}")
    if args.format == "json":
        json.dump(summary_json(summary, args.top), sys.stdout, indent=2)
        print()
        return 0
    if not summary.groups:
        print(f"no errors {label}", file=sys.stderr)
        return 0
    before = ""
    if summary.previous_events is not None:
        before = f" (before: {summary.previous_events:,}"
        if summary.previous_events:
            before += f", {summary.events / summary.previous_events:.1f}x"
        before += ")"
    new = sum(g.is_new for g in summary.groups)
    print(
        f"{len(summary.groups)} error group(s), {summary.events:,} event(s) {label}{before}"
        + (f"; {new} new" if new else "")
        + (f"; {summary.resolved} gone since the window before" if summary.resolved else "")
        + "\n"
    )
    print(f"{'#':>2}  {'COUNT':>7}  {'TREND':<6}  ERROR")
    for i, g in enumerate(summary.groups[: args.top], 1):
        _print_group(i, g, args, cfg)
    sys.stdout.flush()
    rest = len(summary.groups) - args.top
    if rest > 0:
        print(f"\n{rest} more group(s); raise --top to see them", file=sys.stderr)
    if summary.truncated:
        print(
            f"stopped at {args.limit:,} error records per window; counts are a lower bound "
            "(raise --limit)",
            file=sys.stderr,
        )
    return 0


def cmd_history(args: argparse.Namespace, cfg: Config) -> int:
    try:
        path, line = parse_target(args.target, cfg.root)
    except ValueError as exc:
        sys.exit(f"logsite: {exc}")
    commits = site_history(cfg.root, path, line, max_count=args.max)
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
        sys.exit("logsite: the language server needs extra deps: pip install 'logsite[lsp]'")
    serve()
    return 0


def cmd_mcp(args: argparse.Namespace, cfg: Config) -> int:
    try:
        from .mcp_server import serve
    except ImportError:
        sys.exit("logsite: the MCP server needs extra deps: pip install 'logsite[mcp]'")
    serve(cfg)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="logsite", description=__doc__)
    parser.add_argument("--version", action="version", version=f"logsite {__version__}")
    parser.add_argument("--config-dir", help="directory to search for logsite.toml")
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

    p = sub.add_parser("context", help="required context fields and what's added automatically")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument(
        "--production", action="store_true", help="also measure coverage in production records"
    )
    p.add_argument("--hours", type=float)
    p.add_argument("--limit", type=int, default=5000, help="records to sample (default 5000)")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("structure", help="share of log calls with a constant message + fields")
    p.add_argument("--production", action="store_true", help="also weight by production volume")
    p.add_argument("--hours", type=float)
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_structure)

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
    p.add_argument(
        "--since-deploy",
        metavar="REF",
        help="compare before vs after a release (git tag/sha) instead of a rolling window",
    )
    p.add_argument(
        "--base", metavar="REF", help="previous release (default: the tag before --since-deploy)"
    )
    p.add_argument(
        "--deployed-at",
        metavar="TIME",
        help="real deploy time, ISO 8601 (default: the tag or commit date)",
    )
    p.set_defaults(func=cmd_spikes)

    p = sub.add_parser("errors", help="production errors grouped by cause, newest problems first")
    p.add_argument("--hours", type=float)
    p.add_argument(
        "--since-deploy", metavar="REF", help="errors since a release (git tag/sha), vs before"
    )
    p.add_argument(
        "--deployed-at",
        metavar="TIME",
        help="real deploy time, ISO 8601 (default: the tag or commit date)",
    )
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--limit", type=int, default=5000, help="error records per window (5000)")
    p.add_argument("--traceback", action="store_true", help="show a sample traceback per group")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_errors)

    p = sub.add_parser("cost", help="bytes and estimated monthly spend per log call")
    p.add_argument("--hours", type=float)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--price-per-gb", type=float, help="default: [cost] price_per_gb")
    p.add_argument(
        "--price-per-million-events", type=float, help="default: [cost] price_per_million_events"
    )
    p.add_argument("--limit", type=int, default=5000, help="records to sample when not aggregated")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("trace", help="every log line for one request, across services, in order")
    p.add_argument("value", help="the trace / request id")
    p.add_argument("--field", help="field to match (default: [timeline] field, trace_id)")
    p.add_argument("--hours", type=float)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("leaks", help="secrets and personal data in production logs, per log call")
    p.add_argument("--hours", type=float)
    p.add_argument("--limit", type=int, default=5000, help="log records to scan (default 5000)")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_leaks)

    p = sub.add_parser("history", help="commits that touched the code around a log line")
    p.add_argument("target", metavar="PATH:LINE")
    p.add_argument("--max", type=int, default=10)
    p.set_defaults(func=cmd_history)

    sub.add_parser("rules", help="list lint rules").set_defaults(func=cmd_rules)
    sub.add_parser("serve", help="run the language server on stdio").set_defaults(func=cmd_serve)
    sub.add_parser("mcp", help="run the MCP server on stdio").set_defaults(func=cmd_mcp)

    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config_dir)
    except ValueError as exc:
        sys.exit(f"logsite: bad config: {exc}")
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
