"""Log-quality linter.

Static rules (offline, LL0xx) look at the code alone. Volume rules (LL1xx) also need
per-line counts from a backend.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .index import index_calls, iter_python_files
from .models import Diagnostic, LogSite

RULES: dict[str, str] = {
    "LL001": "eager string formatting in log call",
    "LL002": "possible PII or secret in log call",
    "LL003": "error logged in except block without traceback",
    "LL004": "info/debug log inside a loop",
    "LL005": "dynamic log message cannot be traced to production",
    "LL006": "log call missing required context field",
    "LL100": "noisy log line",
    "LL101": "log line never emitted",
    "LL102": "log line spiked compared with the previous window",
}

DEFAULT_PII_PATTERN = (
    r"passw(or)?d|passphrase|^pass$|secret|token|api_?key|auth(?!or)|credential|^ssn$|social_security"
    r"|credit_?card|card_?(number|no)|cvv|iban|email|phone|dob|date_of_birth"
)


@dataclass
class LintConfig:
    select: set[str] = field(default_factory=lambda: set(RULES))
    ignore: set[str] = field(default_factory=set)
    pii_pattern: str = DEFAULT_PII_PATTERN
    # LL006 is off unless you list fields, e.g. ["trace_id"]
    required_fields: list[str] = field(default_factory=list)
    noisy_threshold: int = 100_000  # LL100: emits per window
    spike_ratio: float = 5.0  # LL102: current window >= ratio x previous window ...
    spike_min_count: int = 1_000  # ... and at least this many emits
    dead_levels: set[str] = field(default_factory=lambda: {"warning", "error", "critical"})

    def enabled(self, code: str) -> bool:
        return code in self.select and code not in self.ignore

    @classmethod
    def from_dict(cls, data: Mapping) -> LintConfig:
        cfg = cls()
        if "select" in data:
            cfg.select = set(data["select"])
        cfg.ignore = set(data.get("ignore", []))
        cfg.pii_pattern = data.get("pii_pattern", cfg.pii_pattern)
        cfg.required_fields = list(data.get("required_fields", []))
        cfg.noisy_threshold = int(data.get("noisy_threshold", cfg.noisy_threshold))
        cfg.spike_ratio = float(data.get("spike_ratio", cfg.spike_ratio))
        cfg.spike_min_count = int(data.get("spike_min_count", cfg.spike_min_count))
        if "dead_levels" in data:
            cfg.dead_levels = set(data["dead_levels"])
        return cfg


def is_spike(current: int, previous: int, cfg: LintConfig) -> bool:
    """True if a line went from ``previous`` to ``current`` emits fast enough to flag."""
    return current >= cfg.spike_min_count and current >= cfg.spike_ratio * max(previous, 1)


def find_spikes(
    current: Mapping[str, int], previous: Mapping[str, int], cfg: LintConfig
) -> list[tuple[str, int, int]]:
    """``(site_id, current, previous)`` for every spiking line, biggest jump first."""
    spikes = [(sid, n, previous.get(sid, 0)) for sid, n in current.items()]
    spikes = [t for t in spikes if is_spike(t[1], t[2], cfg)]
    spikes.sort(key=lambda t: t[1] / max(t[2], 1), reverse=True)
    return spikes


def describe_trend(current: int, previous: int) -> str:
    """``"8.0x"`` or ``"new"`` (no emits in the previous window)."""
    return f"{current / previous:.1f}x" if previous else "new"


def _diag(site: LogSite, code: str, message: str, severity: str = "warning") -> Diagnostic:
    return Diagnostic(
        code=code,
        message=message,
        path=site.path,
        line=site.line,
        col=site.col,
        end_line=site.end_line,
        end_col=site.end_col,
        severity=severity,
    )


def _message_arg(site: LogSite, call: ast.Call) -> ast.expr | None:
    idx = 1 if site.level == "log" else 0
    if len(call.args) > idx:
        return call.args[idx]
    return next((k.value for k in call.keywords if k.arg == "msg"), None)


def _names_in(node: ast.AST) -> Iterable[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            yield sub.id
        elif isinstance(sub, ast.Attribute):
            yield sub.attr
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            # dict keys in extra={"password": ...}
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sub.value):
                yield sub.value


def _is_eager(node: ast.expr) -> str | None:
    if isinstance(node, ast.JoinedStr) and any(
        isinstance(v, ast.FormattedValue) for v in node.values
    ):
        return "f-string"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            return "str.format()"
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mod) and isinstance(node.left, (ast.Constant, ast.JoinedStr)):
            return "% formatting"
        if isinstance(node.op, ast.Add):
            return "string concatenation"
    return None


def _extra_keys(call: ast.Call) -> set[str] | None:
    """Keys passed via ``extra={...}``; None if ``extra`` isn't a literal dict."""
    for kw in call.keywords:
        if kw.arg == "extra":
            if isinstance(kw.value, ast.Dict):
                return {k.value for k in kw.value.keys if isinstance(k, ast.Constant)}
            return None  # extra=ctx: can't tell statically, don't flag
    return set()


def check_call(site: LogSite, call: ast.Call, cfg: LintConfig, pii: re.Pattern) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    msg = _message_arg(site, call)

    if cfg.enabled("LL001") and msg is not None:
        kind = _is_eager(msg)
        if kind:
            out.append(
                _diag(
                    site,
                    "LL001",
                    f"{kind} is evaluated even when the level is disabled and breaks "
                    'log grouping; pass args instead: logger.info("x=%s", x)',
                )
            )

    if cfg.enabled("LL002"):
        values: list[ast.AST] = list(call.args[1 if site.level == "log" else 0 :])
        values += [k.value for k in call.keywords if k.arg not in ("exc_info", "stack_info")]
        hits = sorted({n for v in values for n in _names_in(v) if pii.search(n.lower())})
        if hits:
            out.append(_diag(site, "LL002", f"possible PII/secret logged: {', '.join(hits)}"))

    if cfg.enabled("LL003") and site.in_except and site.level in ("error", "critical"):
        if not any(k.arg == "exc_info" for k in call.keywords):
            out.append(
                _diag(
                    site,
                    "LL003",
                    "traceback is lost; use logger.exception(...) or pass exc_info=True",
                )
            )

    if cfg.enabled("LL004") and site.in_loop and site.level in ("debug", "info"):
        out.append(
            _diag(
                site,
                "LL004",
                f"{site.level} log inside a loop can flood your log store",
                severity="info",
            )
        )

    if cfg.enabled("LL005") and site.template is None and msg is not None:
        out.append(
            _diag(
                site,
                "LL005",
                "message is built at runtime, so loglens can't match production logs to this "
                "line; use a literal template",
                severity="hint",
            )
        )

    if cfg.enabled("LL006") and cfg.required_fields:
        keys = _extra_keys(call)
        if keys is not None:
            missing = [f for f in cfg.required_fields if f not in keys]
            if missing:
                out.append(
                    _diag(site, "LL006", f"missing context field(s) in extra: {', '.join(missing)}")
                )
    return out


def lint_source(
    source: str, path: str = "<string>", cfg: LintConfig | None = None
) -> list[Diagnostic]:
    cfg = cfg or LintConfig()
    pii = re.compile(cfg.pii_pattern)
    out: list[Diagnostic] = []
    for site, call in index_calls(source, path):
        out.extend(check_call(site, call, cfg, pii))
    return out


def lint_paths(
    paths: Iterable[str | Path], cfg: LintConfig | None = None, root: Path | None = None
) -> list[Diagnostic]:
    root = (root or Path.cwd()).resolve()
    out: list[Diagnostic] = []
    for p in paths:
        p = Path(p).resolve()
        files = [p] if p.is_file() else iter_python_files(p)
        for f in files:
            try:
                rel = f.relative_to(root).as_posix()
            except ValueError:
                rel = f.as_posix()
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.extend(lint_source(text, rel, cfg))
    return out


def lint_volume(
    sites: Iterable[LogSite],
    counts: Mapping[str, int],
    cfg: LintConfig | None = None,
    window: str = "24h",
    previous: Mapping[str, int] | None = None,
) -> list[Diagnostic]:
    """Rules that need production counts from a backend.

    ``previous`` holds counts for the window just before the current one; LL102 needs it.
    """
    cfg = cfg or LintConfig()
    out: list[Diagnostic] = []
    for site in sites:
        n = counts.get(site.id, 0)
        if cfg.enabled("LL100") and n >= cfg.noisy_threshold:
            out.append(
                _diag(
                    site,
                    "LL100",
                    f"emitted {n:,} times in the last {window}; lower the level or sample it",
                )
            )
        if (
            cfg.enabled("LL101")
            and n == 0
            and site.level in cfg.dead_levels
            and site.template is not None
        ):
            out.append(
                _diag(
                    site,
                    "LL101",
                    f"not seen in production in the last {window}",
                    severity="hint",
                )
            )
        if cfg.enabled("LL102") and previous is not None:
            before = previous.get(site.id, 0)
            if is_spike(n, before, cfg):
                out.append(
                    _diag(
                        site,
                        "LL102",
                        f"spiked to {n:,} in the last {window}, up from {before:,} the window "
                        f"before; see recent changes: loglens history {site.id}",
                        severity="info",
                    )
                )
    return out
