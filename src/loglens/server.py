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
from .git import Commit, site_history
from .index import DEFAULT_EXCLUDES, index_project, index_source
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


class LoglensServer(LanguageServer):
    def __init__(self) -> None:
        super().__init__("loglens", __version__)
        self.cfg: Config | None = None
        self.counts: dict[str, int] = {}
        self.previous_counts: dict[str, int] = {}
        self.counts_ready = False
        self._history: dict[str, list[Commit]] = {}  # site id -> commits; reset on refresh
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
            source="loglens",
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
        backend = from_config(self.cfg.backend)
        if backend is None:
            return
        while not self._stop.is_set():
            try:
                sites = index_project(self.cfg.root, DEFAULT_EXCLUDES | set(self.cfg.excludes))
                current = TimeRange.last(self.cfg.window_hours)
                self.counts = backend.count_by_line(sites, current)
                self.previous_counts = backend.count_by_line(sites, current.previous())
                self._history.clear()
                self.counts_ready = True
                self.workspace_code_lens_refresh(None)
                for uri in list(self.workspace.text_documents):
                    self.publish(uri)
            except Exception as exc:  # keep the server alive if the backend is down
                log.warning("loglens: failed to fetch counts from %s: %s", backend.name, exc)
                self.window_show_message(
                    types.ShowMessageParams(
                        type=types.MessageType.Warning,
                        message=f"loglens: couldn't reach {backend.name} backend: {exc}",
                    )
                )
            self._stop.wait(REFRESH_SECONDS)

    def history(self, path: str, line: int) -> list[Commit]:
        assert self.cfg is not None
        key = f"{path}:{line}"
        commits = self._history.get(key)  # the refresh thread may clear the cache meanwhile
        if commits is None:
            commits = site_history(self.cfg.root, path, line, max_count=HOVER_COMMITS)
            self._history[key] = commits
        return commits


server = LoglensServer()


@server.feature(types.INITIALIZED)
def on_initialized(ls: LoglensServer, params: types.InitializedParams) -> None:
    root = ls.workspace.root_path or str(Path.cwd())
    ls.cfg = load_config(root)
    threading.Thread(target=ls.refresh_counts_forever, daemon=True).start()


@server.feature(types.SHUTDOWN)
def on_shutdown(ls: LoglensServer, params: None) -> None:
    ls._stop.set()


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LoglensServer, params: types.DidOpenTextDocumentParams) -> None:
    ls.publish(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LoglensServer, params: types.DidChangeTextDocumentParams) -> None:
    ls.publish(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: LoglensServer, params: types.DidSaveTextDocumentParams) -> None:
    ls.publish(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_CODE_LENS)
def code_lens(ls: LoglensServer, params: types.CodeLensParams) -> list[types.CodeLens]:
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
        pos = types.Position(line=site.line - 1, character=site.col)
        lenses.append(
            types.CodeLens(
                range=types.Range(start=pos, end=pos),
                command=types.Command(title=title, command=""),
            )
        )
    return lenses


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LoglensServer, params: types.HoverParams) -> types.Hover | None:
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
        parts.append(f"**loglens**: {n:,} in the last {window} (previous {window}: {before:,})")
        if is_spike(n, before, ls.cfg.lint):
            parts[-1] += f", **spiked {describe_trend(n, before)}**"
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


def serve() -> None:
    server.start_io()
