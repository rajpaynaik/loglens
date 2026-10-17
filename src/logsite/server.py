"""Language server: lint diagnostics, production counts as code lenses above log calls, and
a hover with the trend and the commits that recently touched the surrounding code."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from . import __version__
from .backends import TimeRange, from_config
from .config import Config, load_config
from .context import detect_in_source
from .cost import LineCost, format_bytes, format_money, monthly_costs
from .fixes import structured_rewrite
from .git import Commit, site_history
from .index import DEFAULT_EXCLUDES, index_calls, index_project, index_source, iter_python_files
from .lint import describe_trend, is_spike, lint_source, lint_volume
from .models import Diagnostic

log = logging.getLogger(__name__)

SEVERITY = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
    "info": types.DiagnosticSeverity.Information,
    "hint": types.DiagnosticSeverity.Hint,
}
REFRESH_SECONDS = 300
HOVER_COMMITS = 3


def humanize(n: int) -> str:
    for unit, div in (("M", 1_000_000), ("k", 1_000)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(n)


class LogsiteServer(LanguageServer):
    def __init__(self) -> None:
        super().__init__("logsite", __version__)
        self.cfg: Config | None = None
        self.counts: dict[str, int] = {}
        self.costs: dict[str, LineCost] = {}  # only when [cost] has a price
        self.previous_counts: dict[str, int] = {}
        self.counts_ready = False
        self._history: dict[str, list[Commit]] = {}
        self._injected: dict[
            str, dict[str, list[str]]
        ] = {}  # file -> injected context  # site id -> commits; reset on refresh
        self._stop = threading.Event()

    # --- helpers ---------------------------------------------------------------
    def rel_path(self, uri: str) -> str | None:
        fs = to_fs_path(uri)
        if not fs or not self.cfg:
            return None
        try:
            return Path(fs).resolve().relative_to(self.cfg.root).as_posix()
        except ValueError:
            return Path(fs).name

    def to_lsp(self, d: Diagnostic) -> types.Diagnostic:
        return types.Diagnostic(
            range=types.Range(
                start=types.Position(line=d.line - 1, character=d.col),
                end=types.Position(line=d.end_line - 1, character=d.end_col),
            ),
            message=d.message,
            code=d.code,
            source="logsite",
            severity=SEVERITY.get(d.severity, types.DiagnosticSeverity.Warning),
        )

    def publish(self, uri: str) -> None:
        if not self.cfg:
            return
        path = self.rel_path(uri)
        if path is None:
            return
        source = self.workspace.get_text_document(uri).source
        diags = lint_source(source, path, self.cfg.lint)
        if self.counts_ready:
            window = f"{self.cfg.window_hours:g}h"
            diags += lint_volume(
                index_source(source, path),
                self.counts,
                self.cfg.lint,
                window,
                previous=self.previous_counts,
            )
        self.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(uri=uri, diagnostics=[self.to_lsp(d) for d in diags])
        )

    def refresh_counts_forever(self) -> None:
        assert self.cfg is not None
        try:
            backend = from_config(self.cfg.backend)
        except (ValueError, RuntimeError) as exc:
            self.window_show_message(
                types.ShowMessageParams(
                    type=types.MessageType.Error, message=f"logsite: bad [backend] config: {exc}"
                )
            )
            return
        if backend is None:
            return
        while not self._stop.is_set():
            try:
                sites = index_project(self.cfg.root, DEFAULT_EXCLUDES | set(self.cfg.excludes))
                current = TimeRange.last(self.cfg.window_hours)
                if self.cfg.cost.priced:
                    usage = backend.usage_by_line(sites, current)
                    self.counts = {sid: u.count for sid, u in usage.items()}
                    costs = monthly_costs(sites, usage, self.cfg.window_hours, self.cfg.cost)
                    self.costs = {c.site.id: c for c in costs}
                else:
                    self.counts = backend.count_by_line(sites, current)
                self.previous_counts = backend.count_by_line(sites, current.previous())
                self._history.clear()
                self.counts_ready = True
                self.workspace_code_lens_refresh(None)
                for uri in list(self.workspace.text_documents):
                    self.publish(uri)
            except Exception as exc:  # keep the server alive if the backend is down
                log.warning("logsite: failed to fetch counts from %s: %s", backend.name, exc)
                self.window_show_message(
                    types.ShowMessageParams(
                        type=types.MessageType.Warning,
                        message=f"logsite: couldn't reach {backend.name} backend: {exc}",
                    )
                )
            self._stop.wait(REFRESH_SECONDS)

    def scan_injected(self, path: str | None = None, source: str | None = None) -> None:
        """Refresh auto-injected context for one file (on save) or the whole project."""
        assert self.cfg is not None
        rules = self.cfg.lint.context
        if not (rules.active and rules.detect):
            return
        if path is None:
            self._injected = {}
            for file in iter_python_files(self.cfg.root, DEFAULT_EXCLUDES | set(self.cfg.excludes)):
                try:
                    text = file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                rel = file.relative_to(self.cfg.root).as_posix()
                self._injected[rel] = detect_in_source(text, rel)
        else:
            self._injected[path] = detect_in_source(source or "", path)
        merged: dict[str, list[str]] = {}
        for found in self._injected.values():
            for f, where in found.items():
                merged.setdefault(f, []).extend(where)
        rules.detected = merged

    def history(self, path: str, line: int) -> list[Commit]:
        assert self.cfg is not None
        key = f"{path}:{line}"
        commits = self._history.get(key)  # the refresh thread may clear the cache meanwhile
        if commits is None:
            commits = site_history(self.cfg.root, path, line, max_count=HOVER_COMMITS)
            self._history[key] = commits
        return commits


server = LogsiteServer()


@server.feature(types.INITIALIZED)
def on_initialized(ls: LogsiteServer, params: types.InitializedParams) -> None:
    root = ls.workspace.root_path or str(Path.cwd())
    try:
        ls.cfg = load_config(root)
    except ValueError as exc:
        ls.window_show_message(
            types.ShowMessageParams(
                type=types.MessageType.Error, message=f"logsite: bad config: {exc}"
            )
        )
        return
    ls.scan_injected()
    threading.Thread(target=ls.refresh_counts_forever, daemon=True).start()


@server.feature(types.SHUTDOWN)
def on_shutdown(ls: LogsiteServer, params: None) -> None:
    ls._stop.set()


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LogsiteServer, params: types.DidOpenTextDocumentParams) -> None:
    ls.publish(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LogsiteServer, params: types.DidChangeTextDocumentParams) -> None:
    ls.publish(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: LogsiteServer, params: types.DidSaveTextDocumentParams) -> None:
    uri = params.text_document.uri
    path = ls.rel_path(uri)
    if ls.cfg and path:
        before = ls.cfg.lint.context.detected
        ls.scan_injected(path, ls.workspace.get_text_document(uri).source)
        if ls.cfg.lint.context.detected.keys() != before.keys():
            for open_uri in list(ls.workspace.text_documents):  # a new filter changes every file
                ls.publish(open_uri)
            return
    ls.publish(uri)


@server.feature(types.TEXT_DOCUMENT_CODE_LENS)
def code_lens(ls: LogsiteServer, params: types.CodeLensParams) -> list[types.CodeLens]:
    if not ls.cfg or not ls.counts_ready:
        return []
    uri = params.text_document.uri
    path = ls.rel_path(uri)
    if path is None:
        return []
    window = f"{ls.cfg.window_hours:g}h"
    lenses = []
    for site in index_source(ls.workspace.get_text_document(uri).source, path):
        n = ls.counts.get(site.id, 0)
        title = f"$(graph) {humanize(n)} in {window}" if n else f"no logs in {window}"
        before = ls.previous_counts.get(site.id, 0)
        if is_spike(n, before, ls.cfg.lint):
            title += f" $(arrow-up) {describe_trend(n, before)}"
        cost = ls.costs.get(site.id)
        if cost is not None:
            title += f" · {format_money(cost.cost_per_month, ls.cfg.cost.currency)}/mo"
        pos = types.Position(line=site.line - 1, character=site.col)
        lenses.append(
            types.CodeLens(
                range=types.Range(start=pos, end=pos),
                command=types.Command(title=title, command=""),
            )
        )
    return lenses


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LogsiteServer, params: types.HoverParams) -> types.Hover | None:
    if not ls.cfg:
        return None
    uri = params.text_document.uri
    path = ls.rel_path(uri)
    if path is None:
        return None
    line = params.position.line + 1
    site = next(
        (
            s
            for s in index_source(ls.workspace.get_text_document(uri).source, path)
            if s.line <= line <= s.end_line
        ),
        None,
    )
    if site is None:
        return None

    parts: list[str] = []
    if ls.counts_ready:
        window = f"{ls.cfg.window_hours:g}h"
        n, before = ls.counts.get(site.id, 0), ls.previous_counts.get(site.id, 0)
        parts.append(f"**logsite**: {n:,} in the last {window} (previous {window}: {before:,})")
        if is_spike(n, before, ls.cfg.lint):
            parts[-1] += f", **spiked {describe_trend(n, before)}**"
        cost = ls.costs.get(site.id)
        if cost is not None:
            approx = "≈" if cost.estimated else ""
            parts.append(
                f"Cost: {approx}{format_money(cost.cost_per_month, ls.cfg.cost.currency)}/month "
                f"({format_bytes(cost.bytes_per_month)}/month, "
                f"{format_bytes(cost.avg_bytes)} per line)"
            )
    commits = ls.history(site.path, site.line)
    if commits:
        parts.append("Recent changes to this code:")
        parts.append(
            "\n".join(
                f"- `{c.short_sha}` {c.date:%Y-%m-%d} {c.author}: {c.summary}" for c in commits
            )
        )
    if not parts:
        return None
    return types.Hover(
        contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value="\n\n".join(parts)),
        range=types.Range(
            start=types.Position(line=site.line - 1, character=site.col),
            end=types.Position(line=site.end_line - 1, character=site.end_col),
        ),
    )


@server.feature(
    types.TEXT_DOCUMENT_CODE_ACTION,
    types.CodeActionOptions(code_action_kinds=[types.CodeActionKind.QuickFix]),
)
def code_action(ls: LogsiteServer, params: types.CodeActionParams) -> list[types.CodeAction]:
    """LL008 quick fix: move formatted values into extra=."""
    wanted = [d for d in params.context.diagnostics if d.code == "LL008"]
    if not ls.cfg or not wanted:
        return []
    uri = params.text_document.uri
    path = ls.rel_path(uri)
    if path is None:
        return []
    source = ls.workspace.get_text_document(uri).source
    calls = {site.line: (site, call) for site, call in index_calls(source, path)}
    actions = []
    for diag in wanted:
        found = calls.get(diag.range.start.line + 1)
        new_text = structured_rewrite(source, *found) if found else None
        if new_text is None:
            continue
        site, _ = found
        edit = types.TextEdit(
            range=types.Range(
                start=types.Position(line=site.line - 1, character=site.col),
                end=types.Position(line=site.end_line - 1, character=site.end_col),
            ),
            new_text=new_text,
        )
        actions.append(
            types.CodeAction(
                title="logsite: move values into extra= (constant message)",
                kind=types.CodeActionKind.QuickFix,
                diagnostics=[diag],
                is_preferred=True,
                edit=types.WorkspaceEdit(changes={uri: [edit]}),
            )
        )
    return actions


def serve() -> None:
    server.start_io()
