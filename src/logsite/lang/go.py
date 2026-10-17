"""Find log calls in Go: log/slog, zap (and its sugared logger), zerolog, logrus, logr, klog and
the standard library's log package, including chains like ``log.Info().Str("k", v).Msg("m")``
and ``logrus.WithField("k", v).Error("m")``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..models import LogSite
from .base import CallFacts, parse, text

LIBRARIES = {
    "log/slog": "slog",
    "golang.org/x/exp/slog": "slog",
    "go.uber.org/zap": "zap",
    "github.com/rs/zerolog": "zerolog",
    "github.com/rs/zerolog/log": "zerolog",
    "github.com/sirupsen/logrus": "logrus",
    "github.com/go-logr/logr": "logr",
    "k8s.io/klog/v2": "klog",
    "k8s.io/klog": "klog",
    "github.com/charmbracelet/log": "charm",
    "github.com/apex/log": "apex",
    "log": "log",
}
# Info, Infof, Infow, Infoln, InfoS, InfoContext, InfoDepth ...
LEVEL_METHOD = re.compile(
    r"^(Trace|Debug|Info|Print|Warn|Warning|Error|Fatal|Panic|DPanic)"
    r"(f|w|ln|S|Context|Ctx|Depth|Fn)?$"
)
LEVELS = {
    "Trace": "debug", "Debug": "debug", "Info": "info", "Print": "info", "Warn": "warning",
    "Warning": "warning", "Error": "error", "Fatal": "critical", "Panic": "critical",
    "DPanic": "critical",
}  # fmt: skip
ZEROLOG_LEVELS = {"Trace", "Debug", "Info", "Warn", "Error", "Fatal", "Panic", "Err", "Log"}
ZEROLOG_SEND = {"Msg", "Msgf", "Send"}
# Methods that return a logger with fields: slog With, logrus WithField(s)/WithError, zap With.
WITH = {"With", "WithField", "WithFields", "WithError", "WithContext", "WithGroup", "Named"}
LOGGER_TYPES = {"Logger", "SugaredLogger", "Entry", "Event"}
PREFERENCE = ("slog", "zap", "logrus", "zerolog", "logr", "klog", "charm", "apex", "log")
LOGGER_NAME = re.compile(r"(?i)(^|\.)(l|lg|log|logger|.*logger|.*log|sugar|zl|zlog|slogger)$")
ERR_NAME = re.compile(r"^(err|e|error|\w*Err|\w*Error)$")
ERR_CHECK = re.compile(r"\b(\w*[eE]rr\w*)\s*!=\s*nil\b")
SCOPES = {"function_declaration", "method_declaration", "func_literal"}
# Calls that return a logger: zap.L(), logger.Sugar(), zerolog.Ctx(ctx), c.Logger() ...
LOGGER_GETTERS = {"L", "S", "Sugar", "Default", "Ctx", "FromContext", "Logger", "GetLogger"}


@dataclass(frozen=True)
class _Ctx:
    in_loop: bool = False
    err: str | None = None  # inside `if err != nil { ... }`: the error's name
    swallowing: bool = False  # ... and the block doesn't return, panic or exit


def _args(call: Any) -> list[Any]:
    node = call.child_by_field_name("arguments")
    return [c for c in node.named_children if c.type != "comment"] if node else []


def string_value(node: Any) -> str | None:
    if node.type == "raw_string_literal":
        return text(node)[1:-1]
    if node.type != "interpreted_string_literal":
        return None
    out = []
    for c in node.named_children:
        if c.type == "interpreted_string_literal_content":
            out.append(text(c))
        elif c.type == "escape_sequence":
            out.append(bytes(text(c), "utf-8").decode("unicode_escape", errors="replace"))
    return "".join(out)


def _concat(node: Any) -> list[Any]:
    if node.type == "binary_expression" and text(node.child_by_field_name("operator")) == "+":
        return _concat(node.child_by_field_name("left")) + _concat(
            node.child_by_field_name("right")
        )
    if node.type == "parenthesized_expression" and node.named_child_count == 1:
        return _concat(node.named_children[0])
    return [node]


def message_template(node: Any) -> tuple[str | None, str | None]:
    """``(template, eager kind)`` for a message argument."""
    value = string_value(node)
    if value is not None:
        return value, None
    if node.type == "binary_expression":
        parts = [string_value(p) for p in _concat(node)]
        if any(v is not None for v in parts):
            if all(v is not None for v in parts):
                return "".join(parts), None
            return "".join("{}" if v is None else v for v in parts), "string concatenation"
    if node.type == "call_expression":
        fn = text(node.child_by_field_name("function"))
        args = _args(node)
        if fn in ("fmt.Sprintf", "fmt.Sprint", "fmt.Sprintln") and args:
            value = string_value(args[0])
            if value is not None and fn == "fmt.Sprintf":
                return value, "fmt.Sprintf(...)"
            return None, f"{fn}(...)"
    return None, None


def _identifiers(node: Any) -> set[str]:
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("identifier", "field_identifier"):
            out.add(text(n))
        if n.type not in ("interpreted_string_literal", "raw_string_literal"):
            stack.extend(n.named_children)
    return out


class _Indexer:
    def __init__(self, path: str, root: Any) -> None:
        self.path = path
        self.out: list[tuple[LogSite, CallFacts]] = []
        self.packages: dict[str, str] = {}  # import alias -> library
        for spec in _all(root, "import_spec"):
            path_node = spec.child_by_field_name("path")
            import_path = string_value(path_node) if path_node else None
            if import_path not in LIBRARIES:
                continue
            alias = spec.child_by_field_name("name")
            name = text(alias) if alias else import_path.rstrip("/").split("/")[-1]
            if re.fullmatch(r"v\d+", name):  # k8s.io/klog/v2 -> klog
                name = import_path.split("/")[-2]
            self.packages[name] = LIBRARIES[import_path]
        # For receivers like s.logger, whose type we can't see: the likeliest library.
        found = set(self.packages.values())
        self.library = next((lib for lib in PREFERENCE if lib in found), "slog")
        self.typed = self._declared_loggers(root)  # name -> library, from declared types

    def _declared_loggers(self, root: Any) -> dict[str, str]:
        """Fields, parameters and variables declared as loggers (``Zap *zap.Logger``,
        ``log logr.Logger``) or assigned from a constructor (``lg := slog.New(h)``)."""
        out: dict[str, str] = {}
        kinds = ("field_declaration", "parameter_declaration", "var_spec", "short_var_declaration")
        for kind in kinds:
            for decl in _all(root, kind):
                if kind == "short_var_declaration":
                    left, right = (
                        decl.child_by_field_name("left"),
                        decl.child_by_field_name("right"),
                    )
                    names = [text(n) for n in left.named_children] if left else []
                    values = right.named_children if right else []
                    for name, value in zip(names, values, strict=False):
                        lib = self._constructed(value)
                        if lib:
                            out[name] = lib
                    continue
                names = [
                    text(c)
                    for c in decl.named_children
                    if c.type in ("identifier", "field_identifier")
                ]
                lib = self._logger_type(decl.child_by_field_name("type"))
                if lib is None and kind == "var_spec":
                    value = decl.child_by_field_name("value")
                    lib = (
                        self._constructed(value.named_children[0])
                        if value and value.named_children
                        else None
                    )
                if lib:
                    out.update(dict.fromkeys(names, lib))
        return out

    def _logger_type(self, node: Any | None) -> str | None:
        while node is not None and node.type == "pointer_type":
            node = node.named_children[0] if node.named_children else None
        if node is None or node.type != "qualified_type":
            return None
        package = text(node.child_by_field_name("package"))
        name = text(node.child_by_field_name("name"))
        if package in self.packages and name in LOGGER_TYPES:
            return self.packages[package]
        return None

    def _constructed(self, value: Any | None) -> str | None:
        """The library a ``slog.New(h)`` / ``zap.NewProduction()`` /
        ``zerolog.New(w).With().Logger()`` value comes from."""
        node = value
        while node is not None and node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "selector_expression":
                return None
            operand = fn.child_by_field_name("operand")
            if operand.type == "identifier" and text(operand) in self.packages:
                return self.packages[text(operand)]
            node = operand
        return None

    def logger_library(self, node: Any | None) -> str | None:
        """The library behind a receiver (``slog``, ``s.logger``, ``zap.L()``,
        ``logrus.WithField(...)``), or None if it doesn't look like a logger."""
        if node is None:
            return None
        if node.type == "identifier":
            name = text(node)
            if name in self.packages:
                return self.packages[name]
            if name in self.typed:
                return self.typed[name]
            return self.library if LOGGER_NAME.search(name) else None
        if node.type == "selector_expression":
            field = text(node.child_by_field_name("field"))
            if field in self.typed:
                return self.typed[field]
            return self.library if LOGGER_NAME.search(text(node)) else None
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "selector_expression":
                method = text(fn.child_by_field_name("field"))
                inner = fn.child_by_field_name("operand")
                if method in WITH or method in LOGGER_GETTERS:
                    return self.logger_library(inner)
        return None

    def visit(self, node: Any, ctx: _Ctx) -> None:
        kind = node.type
        if kind in SCOPES:
            ctx = _Ctx()
        elif kind == "for_statement":
            ctx = _Ctx(True, ctx.err, ctx.swallowing)
        elif kind == "if_statement":
            cond = node.child_by_field_name("condition")
            m = ERR_CHECK.search(text(cond)) if cond is not None else None
            body = node.child_by_field_name("consequence")
            if m and body is not None:
                handles = _handles(body)
                for child in node.named_children:
                    sub = _Ctx(ctx.in_loop, m.group(1), not handles) if child == body else ctx
                    self.visit(child, sub)
                return
        if kind == "call_expression":
            self.call(node, ctx)
        for child in node.named_children:
            self.visit(child, ctx)

    def call(self, node: Any, ctx: _Ctx) -> None:
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "selector_expression":
            return
        method = text(fn.child_by_field_name("field"))
        operand = fn.child_by_field_name("operand")
        args = _args(node)
        if method in ZEROLOG_SEND:
            self.zerolog(node, method, operand, args, ctx)
            return
        m = LEVEL_METHOD.match(method)
        if not m:
            return
        base, suffix = m.group(1), m.group(2) or ""
        if not args and self._is_zerolog_level(node):
            return  # log.Info() in log.Info().Msg(...): the Msg call is the log call
        library = self.logger_library(operand)
        if library is None:
            return
        if library == "zap" and operand.type == "identifier" and text(operand) in self.packages:
            return  # zap.Error(err), zap.String(...): field constructors, not log calls
        if library == "zerolog" and base in ZEROLOG_LEVELS and not args:
            return
        level = LEVELS[base]
        rest = list(args)
        if suffix in ("Context", "Ctx") and rest:
            rest = rest[1:]  # slog.InfoContext(ctx, msg, ...)
        exc = False
        if base == "Error" and len(rest) >= 2 and rest[0].type != "interpreted_string_literal":
            if ERR_NAME.match(text(rest[0])):  # logr / klog: Error(err, msg, kv...)
                exc, rest = True, rest[1:]
        message = rest[0] if rest else None
        keys, chain_exc = self._chain_fields(operand)
        facts = CallFacts(language="go", printf=suffix == "f")
        facts.has_exception = exc or chain_exc
        self.add(node, level, library, message, rest[1:], keys, facts, ctx)

    def zerolog(self, node: Any, method: str, operand: Any, args: list[Any], ctx: _Ctx) -> None:
        """``log.Info().Str("k", v).Err(err).Msg("m")``: the level is down the chain."""
        level, keys, exc = None, set(), False
        chain = operand
        while chain is not None and chain.type == "call_expression":
            fn = chain.child_by_field_name("function")
            if fn is None or fn.type != "selector_expression":
                break
            step = text(fn.child_by_field_name("field"))
            step_args = _args(chain)
            if step in ("Err", "AnErr"):
                exc = True
            if step_args and string_value(step_args[0]) is not None:
                keys.add(string_value(step_args[0]) or "")
            if step in ZEROLOG_LEVELS and self.logger_library(fn.child_by_field_name("operand")):
                level = "error" if step == "Err" else LEVELS.get(step, "info")
                break
            chain = fn.child_by_field_name("operand")
        if level is None:
            return
        facts = CallFacts(language="go", printf=method == "Msgf", has_exception=exc)
        message = args[0] if args else None
        self.add(node, level, "zerolog", message, args[1:], keys, facts, ctx)

    def _is_zerolog_level(self, node: Any) -> bool:
        parent = node.parent
        return parent is not None and parent.type == "selector_expression"

    def _chain_fields(self, operand: Any) -> tuple[set[str], bool]:
        """Fields added by With/WithField/WithError calls before the level method."""
        keys: set[str] = set()
        exc = False
        chain = operand
        while chain is not None and chain.type == "call_expression":
            fn = chain.child_by_field_name("function")
            if fn is None or fn.type != "selector_expression":
                break
            step = text(fn.child_by_field_name("field"))
            step_args = _args(chain)
            if step == "WithError":
                exc = True
            elif step in WITH:
                keys |= _keys(step_args)
                exc = exc or any(_mentions_err(a) for a in step_args)
            chain = fn.child_by_field_name("operand")
        return keys, exc

    def add(
        self,
        node: Any,
        level: str,
        library: str,
        message: Any,
        values: list[Any],
        keys: set[str],
        facts: CallFacts,
        ctx: _Ctx,
    ) -> None:
        template = None
        if message is not None:
            template, eager = message_template(message)
            facts.eager = eager
            facts.dynamic = template is None
            if eager:
                facts.names |= _identifiers(message) - {"fmt", "Sprintf", "Sprint", "Sprintln"}
        facts.names |= keys | _keys(values)
        for v in values:
            facts.names |= _identifiers(v) - {"slog", "zap", "logrus"}
            if _mentions_err(v, ctx.err):
                facts.has_exception = True
        site = LogSite(
            path=self.path,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            end_col=node.end_point[1],
            level=level,
            template=template,
            logger=text(node.child_by_field_name("function")).rsplit(".", 1)[0][:80],
            in_except=ctx.err is not None,
            in_loop=ctx.in_loop,
            in_swallowing_except=ctx.swallowing,
            library=library,
            bound_fields=tuple(sorted(keys)),
        )
        self.out.append((site, facts))


def _all(root: Any, kind: str) -> list[Any]:
    out, stack = [], [root]
    while stack:
        n = stack.pop()
        if n.type == kind:
            out.append(n)
        stack.extend(n.named_children)
    return out


def _keys(args: list[Any]) -> set[str]:
    """Field names in key-value arguments: slog/logr ("k", v) pairs, zap.String("k", v),
    slog.Any("k", v), logrus.Fields{"k": v}."""
    keys: set[str] = set()
    for a in args:
        value = string_value(a)
        if value is not None:
            keys.add(value)
        elif a.type == "call_expression":
            inner = _args(a)
            if inner and string_value(inner[0]) is not None:
                keys.add(string_value(inner[0]) or "")
        elif a.type == "composite_literal":
            for kv in _all(a, "keyed_element"):
                key = kv.named_children[0] if kv.named_children else None
                literal = key.named_children[0] if key is not None and key.named_children else key
                if literal is not None and string_value(literal) is not None:
                    keys.add(string_value(literal) or "")
    return keys


def _mentions_err(node: Any, err: str | None = None) -> bool:
    """The value carries the error: ``err``, ``err.Error()``, ``zap.Error(err)``..."""
    return any(n == err or ERR_NAME.match(n) for n in _identifiers(node) - {"Error"})


def _handles(block: Any) -> bool:
    """Whether an ``if err != nil`` block passes the error on: returns, panics or exits."""
    if _all(block, "return_statement"):
        return True
    for call in _all(block, "call_expression"):
        fn = text(call.child_by_field_name("function"))
        if fn in ("panic", "os.Exit", "log.Fatal", "log.Fatalf", "log.Panic", "log.Panicf"):
            return True
        if fn.endswith((".Fatal", ".Fatalf", ".Panic", ".Panicf", ".Fatalw")):
            return True
    return False


def index(source: str, path: str) -> list[tuple[LogSite, CallFacts]]:
    root = parse("go", source)
    indexer = _Indexer(path, root)
    indexer.visit(root, _Ctx())
    return indexer.out
