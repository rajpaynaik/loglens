"""Language server: lint diagnostics + production counts as code lenses above log calls."""

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
from .index import DEFAULT_EXCLUDES, index_project, index_source
from .lint import lint_source, lint_volume
from .models import Diagnostic

log = logging.getLogger(__name__)

SEVERITY = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
    "info": types.DiagnosticSeverity.Information,
    "hint": types.DiagnosticSeverity.Hint,
}
REFRESH_SECONDS = 300


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
        self.counts_ready = False
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
            diags += lint_volume(index_source(source, path), self.counts, self.cfg.lint, window)
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
                self.counts = backend.count_by_line(sites, TimeRange.last(self.cfg.window_hours))
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
        pos = types.Position(line=site.line - 1, character=site.col)
        lenses.append(
            types.CodeLens(
                range=types.Range(start=pos, end=pos),
                command=types.Command(title=title, command=""),
            )
        )
    return lenses


def serve() -> None:
    server.start_io()
