"""Quick fixes the editor can apply. LL008: move values out of the message into ``extra=``.

``logger.error("payment failed for order %s: %s", order.id, err)`` becomes
``logger.error("payment failed for order", extra={"order_id": order.id, "err": err})``.
The rewrite is only offered when it's safe: placeholders match the arguments, no
``%(name)s`` mappings, and any existing ``extra=`` is a literal dict to merge into.
"""

from __future__ import annotations

import ast
import json
import re

from .lint import interpolated_values
from .models import LogSite

# Attributes logging.LogRecord already has; extra={"name": ...} raises KeyError at runtime.
RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName", "levelname",
    "levelno", "lineno", "message", "module", "msecs", "msg", "name", "pathname", "process",
    "processName", "relativeCreated", "stack_info", "taskName", "thread", "threadName",
}  # fmt: skip
_SIMPLE_PERCENT = re.compile(r"%[-#0 +]*\d*(?:\.\d+)?[sdifrxXeEgGoc]")
_ANY_PERCENT = re.compile(r"%(?!%)")
_PUNCT = re.compile(r"[\s:=,;\-–(\[]+$")
# One connecting word right before a placeholder: "failed for %s" -> "failed".
_LINK_WORD = re.compile(r"\s+(?:for|of|to|with|from|by|on|at|is|was|are|as)$", re.IGNORECASE)


def _trim(text: str) -> str:
    return _PUNCT.sub("", _LINK_WORD.sub("", _PUNCT.sub("", text)))


def field_name(node: ast.expr) -> str | None:
    """``order.id`` -> ``order_id``, ``self.user_id`` -> ``user_id``, ``d["k"]`` -> ``k``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = node.value
        if isinstance(inner, ast.Name) and inner.id not in ("self", "cls"):
            return f"{inner.id}_{node.attr}"
        if isinstance(inner, ast.Attribute):
            return f"{inner.attr}_{node.attr}"
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        if isinstance(node.slice.value, str) and node.slice.value.isidentifier():
            return node.slice.value
    if isinstance(node, ast.Call):
        func = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        arg = field_name(node.args[0]) if node.args else None
        if func and arg:
            return f"{arg}_{func}" if func != "len" else f"{arg}_count"
    return None


def _unique(name: str, taken: set[str]) -> str:
    if name in RESERVED:
        name = f"{name}_value"
    base, n = name, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def constant_message(template: str) -> str:
    """Drop the placeholders and the dangling words/punctuation they leave behind."""
    escaped = "\x00"  # stands in for a literal "%%" so it isn't read as a placeholder
    template = template.replace("%%", escaped)
    parts = re.split(r"\{[^{}]*\}|%[-#0 +]*\d*(?:\.\d+)?[sdifrxXeEgGoc]", template)
    out: list[str] = []
    for part in parts:
        part = _trim(part) if part is not parts[-1] else part
        if part.strip():
            out.append(part.strip())
    text = " ".join(out).replace(escaped, "%")
    for opening, closing in ("()", "[]"):
        if text.count(opening) != text.count(closing):  # "(%s skipped)" lost its opener
            text = text.replace(opening, "").replace(closing, "")
    return _trim(re.sub(r"\s+", " ", text)).strip() or "event"


def structured_rewrite(source: str, site: LogSite, call: ast.Call) -> str | None:
    """Replacement source text for the whole call, or None if it can't be done safely."""
    idx = 1 if site.level == "log" else 0
    if len(call.args) <= idx or any(isinstance(a, ast.Starred) for a in call.args):
        return None
    msg = call.args[idx]
    values = interpolated_values(site, call, msg)
    if not values:
        return None

    if isinstance(msg, ast.Constant):
        template = msg.value
        bare = template.replace("%%", "")
        if len(_SIMPLE_PERCENT.findall(bare)) != len(values) or len(
            _ANY_PERCENT.findall(bare)
        ) != len(values):
            return None  # %(name)s mappings, odd specifiers, or a mismatch
    elif isinstance(msg, ast.JoinedStr):
        template = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in msg.values)
    elif isinstance(msg, ast.Call) and isinstance(msg.func.value, ast.Constant):  # type: ignore[attr-defined]
        if msg.keywords or "{" not in msg.func.value.value:  # type: ignore[attr-defined]
            return None
        template = msg.func.value.value  # type: ignore[attr-defined]
    else:
        return None  # "..." % args and other shapes: leave to a human

    extra: dict[str, str] = {}
    other_kw: list[str] = []
    for kw in call.keywords:
        if kw.arg == "extra":
            if not isinstance(kw.value, ast.Dict) or None in kw.value.keys:
                return None  # extra=ctx or {**ctx}: can't merge safely
            for k, v in zip(kw.value.keys, kw.value.values, strict=True):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    return None
                extra[k.value] = ast.get_source_segment(source, v) or ast.unparse(v)
        elif kw.arg is None:
            return None  # **kwargs
        else:
            other_kw.append(ast.get_source_segment(source, kw) or ast.unparse(kw))

    taken = set(extra)
    new_fields: dict[str, str] = {}
    for i, value in enumerate(values, 1):
        name = _unique(field_name(value) or f"value{i}", taken)
        new_fields[name] = ast.get_source_segment(source, value) or ast.unparse(value)

    func = ast.get_source_segment(source, call.func) or ast.unparse(call.func)
    lead = [ast.get_source_segment(source, a) or ast.unparse(a) for a in call.args[:idx]]
    items = {**extra, **new_fields}
    extra_src = "{" + ", ".join(f"{json.dumps(k)}: {v}" for k, v in items.items()) + "}"
    args = [*lead, json.dumps(constant_message(template)), *other_kw, f"extra={extra_src}"]
    return f"{func}({', '.join(args)})"
