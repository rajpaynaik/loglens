"""Find every log call in a Python codebase using the standard-library ``ast`` module."""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from .models import LogSite

LEVEL_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "fatal", "exception"}
DEFAULT_EXCLUDES = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox"}


def _receiver_text(node: ast.expr) -> str | None:
    """Return dotted text for Name/Attribute chains (``self.logger``), else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _receiver_text(node.value)
        return f"{inner}.{node.attr}" if inner else None
    if isinstance(node, ast.Call):  # logging.getLogger(__name__).info(...)
        inner = _receiver_text(node.func)
        return f"{inner}()" if inner else None
    return None


def _looks_like_logger(receiver: str) -> bool:
    last = receiver.rsplit(".", 1)[-1].lower()
    return "log" in last or receiver.endswith("getLogger()")


def extract_template(node: ast.expr) -> str | None:
    """Turn the message argument of a log call into a template string.

    ``"user %s"`` -> ``"user %s"``; ``f"user {uid}"`` -> ``"user {}"``;
    ``"user {}".format(uid)`` -> ``"user {}"``. Anything else -> None.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value.replace("%", "%%"))
            else:
                out.append("{}")
        return "".join(out)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        return re.sub(r"\{[^{}]*\}", "{}", node.func.value.value.replace("%", "%%"))
    return None


_BROAD = {"Exception", "BaseException"}


def _is_broad(node: ast.expr | None) -> bool:
    """``except:``, ``except Exception``, ``except (ValueError, Exception)``..."""
    if node is None:
        return True
    if isinstance(node, ast.Tuple):
        return any(_is_broad(elt) for elt in node.elts)
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    return name in _BROAD


def _raises(handler: ast.ExceptHandler) -> bool:
    """Whether the handler raises (re-raise or translate), ignoring nested functions."""
    stack: list[ast.AST] = list(handler.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.sites: list[LogSite] = []
        self.calls: list[tuple[LogSite, ast.Call]] = []
        self._except_depth = 0
        self._loop_depth = 0
        self._func_depth = 0
        self._swallowing: list[bool] = []  # one entry per enclosing except handler

    # Track context so the linter can reason about it later.
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._except_depth += 1
        self._swallowing.append(_is_broad(node.type) and not _raises(node))
        self.generic_visit(node)
        self._swallowing.pop()
        self._except_depth -= 1

    def _visit_loop(self, node: ast.AST) -> None:
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    visit_For = visit_AsyncFor = visit_While = _visit_loop

    def _visit_func(self, node: ast.AST) -> None:
        # A loop outside a function doesn't make a log inside a nested def "in a loop".
        saved = self._loop_depth, self._except_depth, self._swallowing
        self._loop_depth = self._except_depth = 0
        self._swallowing = []
        self.generic_visit(node)
        self._loop_depth, self._except_depth, self._swallowing = saved

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        site = self._as_log_site(node)
        if site:
            self.sites.append(site)
            self.calls.append((site, node))
        self.generic_visit(node)

    def _as_log_site(self, node: ast.Call) -> LogSite | None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        receiver = _receiver_text(func.value)
        if receiver is None or not _looks_like_logger(receiver):
            return None

        if func.attr in LEVEL_METHODS:
            level = {"warn": "warning", "fatal": "critical"}.get(func.attr, func.attr)
            msg_arg = node.args[0] if node.args else None
        elif func.attr == "log" and len(node.args) >= 2:
            level = "log"
            msg_arg = node.args[1]
        else:
            return None

        if msg_arg is None:
            msg_kw = next((k.value for k in node.keywords if k.arg == "msg"), None)
            msg_arg = msg_kw
        template = extract_template(msg_arg) if msg_arg is not None else None

        return LogSite(
            path=self.path,
            line=node.lineno,
            col=node.col_offset,
            end_line=node.end_lineno or node.lineno,
            end_col=node.end_col_offset or node.col_offset,
            level=level,
            template=template,
            logger=receiver,
            in_except=self._except_depth > 0,
            in_loop=self._loop_depth > 0,
            in_swallowing_except=bool(self._swallowing) and self._swallowing[-1],
        )


def index_calls(source: str, path: str = "<string>") -> list[tuple[LogSite, ast.Call]]:
    """Like :func:`index_source` but also returns the AST node of each call."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []
    visitor = _Visitor(path)
    visitor.visit(tree)
    return visitor.calls


def index_source(source: str, path: str = "<string>") -> list[LogSite]:
    """Return all log sites in a Python source string. Syntax errors yield an empty list."""
    return [site for site, _ in index_calls(source, path)]


def iter_python_files(root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> Iterator[Path]:
    excludes = set(excludes)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excludes and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def index_project(root: str | Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> list[LogSite]:
    """Index every ``.py`` file under ``root``."""
    root = Path(root).resolve()
    sites: list[LogSite] = []
    for file in iter_python_files(root, excludes):
        rel = file.relative_to(root).as_posix()
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sites.extend(index_source(text, rel))
    return sites
