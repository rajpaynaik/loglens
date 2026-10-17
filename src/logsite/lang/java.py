"""Find log calls in Java: SLF4J, Log4j 2, java.util.logging, Commons Logging, Flogger and
JBoss Logging, including Lombok's ``@Slf4j``-style annotations and the fluent APIs
(``log.atInfo().addKeyValue("k", v).log("msg")``)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..models import LogSite
from .base import CallFacts, numbered_to_braces, parse, text

LOGGING_PACKAGES = {
    "org.slf4j": "slf4j",
    "org.apache.logging.log4j": "log4j",
    "org.apache.log4j": "log4j",
    "java.util.logging": "jul",
    "org.apache.commons.logging": "commons",
    "com.google.common.flogger": "flogger",
    "org.jboss.logging": "jboss",
    "lombok.extern.slf4j": "slf4j",
    "lombok.extern.log4j": "log4j",
    "lombok.extern.java": "jul",
    "lombok.extern.apachecommons": "commons",
    "lombok.extern.flogger": "flogger",
    "lombok.extern.jbosslog": "jboss",
}
LOMBOK = {
    "Slf4j": "slf4j", "XSlf4j": "slf4j", "Log4j2": "log4j", "Log4j": "log4j", "Log": "jul",
    "CommonsLog": "commons", "Flogger": "flogger", "JBossLog": "jboss", "CustomLog": "slf4j",
}  # fmt: skip
CONVENTIONAL = {"log", "logger", "LOG", "LOGGER", "log_", "logger_", "LOG_", "sLog", "sLogger"}
LEVELS = {
    "trace": "debug", "debug": "debug", "info": "info", "warn": "warning", "error": "error",
    "fatal": "critical",
    # java.util.logging
    "severe": "error", "warning": "warning", "config": "info", "fine": "debug",
    "finer": "debug", "finest": "debug",
}  # fmt: skip
FLUENT = {f"at{name[0].upper()}{name[1:]}": level for name, level in LEVELS.items()}
JUL_LEVELS = {
    "SEVERE": "error", "WARNING": "warning", "INFO": "info", "CONFIG": "info", "FINE": "debug",
    "FINER": "debug", "FINEST": "debug", "ERROR": "error", "WARN": "warning", "DEBUG": "debug",
    "TRACE": "debug", "FATAL": "critical",
}  # fmt: skip
GET_LOGGER = {"getLogger", "getLog", "getFormatterLogger", "forEnclosingClass"}
EXCEPTION_NAME = re.compile(r"^(e|ex|exc|exception|t|th|throwable|cause|\w*(Exception|Error))$")
LOOPS = {"for_statement", "enhanced_for_statement", "while_statement", "do_statement"}
SCOPES = {"method_declaration", "constructor_declaration", "lambda_expression", "class_body"}
BROAD = {"Exception", "Throwable", "java.lang.Exception", "java.lang.Throwable"}


@dataclass(frozen=True)
class _Ctx:
    in_loop: bool = False
    catch_param: str | None = None  # the exception's name inside a catch block
    swallowing: bool = False  # a broad catch that never rethrows


def _named(node: Any) -> list[Any]:
    return [c for c in node.named_children if c.type not in ("line_comment", "block_comment")]


def string_value(node: Any) -> str | None:
    """The value of a string literal or text block, or None if ``node`` isn't one."""
    if node.type != "string_literal":
        return None
    out = []
    for c in node.named_children:
        if c.type in ("string_fragment", "multiline_string_fragment"):
            out.append(text(c))
        elif c.type == "escape_sequence":
            out.append(bytes(text(c), "utf-8").decode("unicode_escape", errors="replace"))
    return "".join(out)


def _concat_parts(node: Any) -> list[Any]:
    if node.type == "binary_expression" and text(node.child_by_field_name("operator")) == "+":
        return _concat_parts(node.child_by_field_name("left")) + _concat_parts(
            node.child_by_field_name("right")
        )
    if node.type == "parenthesized_expression" and node.named_child_count == 1:
        return _concat_parts(node.named_children[0])
    return [node]


def message_template(node: Any) -> tuple[str | None, str | None]:
    """``(template, eager kind)`` for a message argument."""
    value = string_value(node)
    if value is not None:
        return numbered_to_braces(value), None
    if node.type == "binary_expression":
        parts = _concat_parts(node)
        literals = [string_value(p) for p in parts]
        if any(v is not None for v in literals):
            if all(v is not None for v in literals):
                return numbered_to_braces("".join(literals)), None  # "a" + "b": a constant
            template = "".join("{}" if v is None else v for v in literals)
            return numbered_to_braces(template), "string concatenation"
    if node.type == "method_invocation":
        name = text(node.child_by_field_name("name"))
        args = _named(node.child_by_field_name("arguments"))
        obj = node.child_by_field_name("object")
        if name == "format" and args and text(obj) in ("String", "MessageFormat"):
            value = string_value(args[0])
            if value is not None:
                return numbered_to_braces(value), f"{text(obj)}.format(...)"
        if name == "formatted" and obj is not None and string_value(obj) is not None:
            return numbered_to_braces(string_value(obj) or ""), '"...".formatted(...)'
    return None, None


class _Indexer:
    def __init__(self, path: str, root: Any) -> None:
        self.path = path
        self.out: list[tuple[LogSite, CallFacts]] = []
        self.loggers: set[str] = set()
        self.library = "slf4j"
        self.uses_logging = False
        self._scan_declarations(root)

    def _scan_declarations(self, root: Any) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type == "import_declaration":
                name = text(node).removeprefix("import").removeprefix(" static").strip(" ;")
                for package, library in LOGGING_PACKAGES.items():
                    if name.startswith(package):
                        self.uses_logging, self.library = True, library
            elif node.type in ("field_declaration", "local_variable_declaration"):
                type_name = text(node.child_by_field_name("type")).rsplit(".", 1)[-1]
                declared = node.child_by_field_name("declarator")
                value = declared.child_by_field_name("value") if declared else None
                from_factory = (
                    value is not None
                    and value.type == "method_invocation"
                    and (text(value.child_by_field_name("name")) in GET_LOGGER)
                )
                if type_name in ("Logger", "Log", "XLogger", "FluentLogger") or from_factory:
                    for d in node.children_by_field_name("declarator"):
                        self.loggers.add(text(d.child_by_field_name("name")))
            elif node.type == "marker_annotation" or node.type == "annotation":
                name = text(node.child_by_field_name("name")).rsplit(".", 1)[-1]
                if name in LOMBOK:
                    self.loggers.add("log")
                    self.uses_logging, self.library = True, LOMBOK[name]

    def is_logger(self, node: Any | None) -> bool:
        if node is None:
            return False
        if node.type == "identifier":
            name = text(node)
            return name in self.loggers or (self.uses_logging and name in CONVENTIONAL)
        if node.type == "field_access":  # this.log, Outer.LOG
            return self.is_logger(node.child_by_field_name("field"))
        if node.type == "method_invocation":  # LoggerFactory.getLogger(X.class).info(...)
            return text(node.child_by_field_name("name")) in GET_LOGGER
        return False

    # --- walking ---------------------------------------------------------------------------
    def visit(self, node: Any, ctx: _Ctx) -> None:
        kind = node.type
        if kind in SCOPES:
            ctx = _Ctx()
        elif kind in LOOPS:
            ctx = _Ctx(True, ctx.catch_param, ctx.swallowing)
        elif kind == "catch_clause":
            param = next(
                (c for c in node.named_children if c.type == "catch_formal_parameter"), None
            )
            name = text(param.child_by_field_name("name")) if param else None
            types = {
                text(t) for t in (param.named_children if param else []) if t.type == "catch_type"
            }
            caught = {x.strip() for t in types for x in t.split("|")}
            body = node.child_by_field_name("body")
            rethrows = body is not None and _contains(body, "throw_statement")
            ctx = _Ctx(ctx.in_loop, name, bool(caught & BROAD) and not rethrows)
        if kind == "method_invocation":
            self.call(node, ctx)
        for child in node.named_children:
            self.visit(child, ctx)

    def call(self, node: Any, ctx: _Ctx) -> None:
        name = text(node.child_by_field_name("name"))
        obj = node.child_by_field_name("object")
        args = _named(node.child_by_field_name("arguments"))
        level = None
        message = None
        keys: set[str] = set()
        if name in LEVELS and self.is_logger(obj):
            level = LEVELS[name]
            message = args[0] if args else None
            if len(args) >= 2 and args[0].type in ("identifier", "field_access"):
                if string_value(args[1]) is not None or args[1].type == "binary_expression":
                    message = args[1]  # log.info(marker, "msg", ...)
        elif name == "log" and self.is_logger(obj) and args:  # JUL: log(Level.INFO, "msg", ...)
            level = JUL_LEVELS.get(text(args[0]).rsplit(".", 1)[-1], "info")
            message = args[1] if len(args) > 1 else None
            args = args[1:]
        elif name == "log" and obj is not None and obj.type == "method_invocation":
            # Fluent: log.atInfo().addKeyValue("k", v).setMessage("m").log() / .log("m", x)
            chain, log_args = obj, args
            while chain is not None and chain.type == "method_invocation":
                step = text(chain.child_by_field_name("name"))
                step_args = _named(chain.child_by_field_name("arguments"))
                if step in ("addKeyValue", "addArgument", "with") and step_args:
                    key = string_value(step_args[0])
                    if key:
                        keys.add(key)
                    args = args + step_args[1:]
                elif step == "setMessage" and step_args and message is None and not log_args:
                    message = step_args[0]
                elif step == "setCause" or step == "withCause":
                    args = args + step_args
                if step in FLUENT and self.is_logger(chain.child_by_field_name("object")):
                    level = FLUENT[step]
                    break
                chain = chain.child_by_field_name("object")
            if level is not None and message is None and args:
                message = args[0]
        if level is None:
            return
        self.add(node, level, message, args, keys, ctx)

    def add(
        self, node: Any, level: str, message: Any, args: list[Any], keys: set[str], ctx: _Ctx
    ) -> None:
        facts = CallFacts(language="java", names=set(keys))
        template = None
        if message is not None:
            template, facts.eager = message_template(message)
            facts.dynamic = template is None
        for arg in args:
            if arg is message:
                continue
            facts.names |= _identifiers(arg)
            if _is_exception(arg, ctx.catch_param):
                facts.has_exception = True
            elif arg.type == "method_invocation" and _is_exception(
                arg.child_by_field_name("object"), ctx.catch_param
            ):
                facts.exception_message_only = True  # e.getMessage(), e.toString()
        if message is not None and facts.eager:
            facts.names |= _identifiers(message)
        site = LogSite(
            path=self.path,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            end_col=node.end_point[1],
            level=level,
            template=template,
            logger=text(node.child_by_field_name("object")).split("(")[0][:80],
            in_except=ctx.catch_param is not None,
            in_loop=ctx.in_loop,
            in_swallowing_except=ctx.swallowing,
            library=self.library,
            bound_fields=tuple(sorted(keys)),
        )
        self.out.append((site, facts))


def _contains(node: Any, kind: str) -> bool:
    stack = list(node.named_children)
    while stack:
        n = stack.pop()
        if n.type == kind:
            return True
        if n.type not in ("lambda_expression", "class_body"):
            stack.extend(n.named_children)
    return False


def _identifiers(node: Any) -> set[str]:
    """Names a value is built from: variables, fields and getters (``user.getEmail()``)."""
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            out.add(text(n))
        elif n.type == "field_access":
            out.add(text(n.child_by_field_name("field")))
        elif n.type == "method_invocation":
            out.add(text(n.child_by_field_name("name")))
        if n.type != "string_literal":
            stack.extend(n.named_children)
    return out


def _is_exception(node: Any | None, catch_param: str | None) -> bool:
    if node is None:
        return False
    if node.type == "identifier":
        name = text(node)
        return name == catch_param or bool(EXCEPTION_NAME.match(name))
    if node.type == "object_creation_expression":
        return bool(
            re.search(r"(Exception|Error|Throwable)$", text(node.child_by_field_name("type")))
        )
    return False


def index(source: str, path: str) -> list[tuple[LogSite, CallFacts]]:
    root = parse("java", source)
    indexer = _Indexer(path, root)
    indexer.visit(root, _Ctx())
    return indexer.out
