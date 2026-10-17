"""Find and parse the Python exception in a log record, and tell error records apart.

Tracebacks reach log stores in many shapes:

- text in a field: python-json-logger's ``exc_info``, ECS ``error.stack_trace``,
  OpenTelemetry ``exception.stacktrace``, loguru's serialized ``text``...
- structlog's ``dict_tracebacks``: a list of ``{"exc_type", "exc_value", "frames"}``
- type and message fields without a stack (``error.type`` + ``error.message``)
- a plain-text ``Traceback (most recent call last):`` block in the message itself
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from .context import _keys
from .models import LogRecord

# Where log stores and shippers usually put the level.
LEVEL_FIELDS = ("level", "levelname", "severity", "log.level", "level.name")
ERROR_WORDS = {"error", "err", "critical", "fatal", "exception", "crit", "alert", "emergency"}
WARNING_WORDS = {"warning", "warn"}

TRACEBACK_FIELDS = (
    "exc_info", "exc_text", "exception", "stack_trace", "stacktrace", "error.stack_trace",
    "exception.stacktrace", "error.stack", "traceback", "stack",
)  # fmt: skip
TYPE_FIELDS = ("exc_type", "exception.type", "error.type", "error.kind")
MESSAGE_FIELDS = ("exc_message", "exception.message", "error.message")

TRACEBACK_START = "Traceback (most recent call last):"
CHAIN_MARKERS = (
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
)
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?\s*$')
_EXC_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$")
_TEXT_LEVEL = re.compile(r"\b(ERROR|CRITICAL|FATAL|EXCEPTION)\b")
_LIBRARY_PARTS = ("site-packages", "dist-packages")


@dataclass(frozen=True)
class Frame:
    file: str
    line: int
    function: str
    code: str | None = None

    @property
    def is_library(self) -> bool:
        """Installed packages and the standard library, never the project's own code."""
        path = self.file.replace("\\", "/")
        return (
            any(f"/{p}/" in path for p in _LIBRARY_PARTS)
            or path.startswith("<")  # <frozen runpy>, <string>
            or re.search(r"/lib/python\d", path) is not None
        )

    @property
    def library(self) -> str | None:
        """``httpx`` for ``.../site-packages/httpx/_client.py``."""
        path = self.file.replace("\\", "/")
        for p in _LIBRARY_PARTS:
            if f"/{p}/" in path:
                rest = path.split(f"/{p}/", 1)[1]
                return rest.split("/", 1)[0].removesuffix(".py")
        m = re.search(r"/lib/python[\d.]+/([^/]+)", path)
        return m.group(1).removesuffix(".py") if m else None


@dataclass
class Exc:
    """One exception in a chain."""

    type: str
    message: str = ""
    frames: list[Frame] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"{self.type}: {self.message}" if self.message else self.type


@dataclass
class ErrorInfo:
    """The exception attached to a record: ``chain[0]`` is the root cause, ``chain[-1]`` the
    exception that was finally raised and logged."""

    chain: list[Exc]
    text: str | None = None  # the traceback as text, when the record had one

    @property
    def final(self) -> Exc:
        return self.chain[-1]

    @property
    def root(self) -> Exc:
        return self.chain[0]


def parse_traceback(text: str) -> list[Exc]:
    """Every exception in a traceback text, root cause first. Empty if there's none."""
    chain: list[Exc] = []
    current: Exc | None = None
    done = True  # the current exception's type line has been read
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.endswith(TRACEBACK_START):
            current, done = Exc(type=""), False
            chain.append(current)
            continue
        if current is None or done:
            continue
        m = _FRAME.match(line)
        if m:
            current.frames.append(Frame(m["file"], int(m["line"]), (m["func"] or "?").strip()))
            continue
        if not stripped:
            continue
        if line[:1].isspace():  # the source line under a frame, or 3.11+ ^^^^ markers
            last = current.frames[-1] if current.frames else None
            if last and last.code is None and not set(stripped) <= set("^~ "):
                current.frames[-1] = Frame(last.file, last.line, last.function, stripped)
            continue
        m = _EXC_LINE.match(line)
        if m:
            current.type, current.message, done = m["type"], (m["msg"] or "").strip(), True
    return [e for e in chain if e.type]


def _structlog_chain(value: list) -> list[Exc]:
    """structlog's ``dict_tracebacks``: the raised exception first, then its causes."""
    chain = []
    for item in value:
        if not isinstance(item, dict) or "exc_type" not in item:
            return []
        frames = [
            Frame(str(f.get("filename", "?")), int(f.get("lineno") or 0), str(f.get("name", "?")))
            for f in item.get("frames") or []
            if isinstance(f, dict)
        ]
        chain.append(Exc(str(item["exc_type"]), str(item.get("exc_value") or ""), frames))
    return chain[::-1]


class _Fields:
    """A record's fields by dotted path (``error.type`` whether stored nested or flattened)
    and by last segment (``level`` also finds ``json.level``), built once per record."""

    def __init__(self, record: LogRecord) -> None:
        self.exact: dict[str, object] = {}
        self.last: dict[str, object] = {}
        for key, value in _keys(record.fields):
            if value is None:
                continue
            self.exact.setdefault(key, value)
            self.last.setdefault(key.rsplit(".", 1)[-1], value)

    def get(self, key: str) -> object | None:
        value = self.exact.get(key)
        if value is None and "." not in key:
            value = self.last.get(key)
        return value


def extract_error(record: LogRecord, fields: _Fields | None = None) -> ErrorInfo | None:
    """The exception attached to ``record``, from whichever shape it was logged in."""
    f = fields or _Fields(record)
    for key in TRACEBACK_FIELDS:
        value = f.get(key)
        if isinstance(value, list) and value:
            chain = _structlog_chain(value)
            if chain:
                return ErrorInfo(chain)
        if isinstance(value, str) and TRACEBACK_START in value:
            chain = parse_traceback(value)
            if chain:
                return ErrorInfo(chain, text=value)
    if TRACEBACK_START in record.message:
        chain = parse_traceback(record.message)
        if chain:
            return ErrorInfo(chain, text=record.message[record.message.index(TRACEBACK_START) :])
    exc_type = next((v for k in TYPE_FIELDS if isinstance(v := f.get(k), str) and v), None)
    if exc_type:
        message = next((str(v) for k in MESSAGE_FIELDS if (v := f.get(k)) is not None), "")
        return ErrorInfo([Exc(exc_type, message)])
    return None


def level_of(record: LogRecord, fields: _Fields | None = None) -> str | None:
    f = fields or _Fields(record)
    for key in LEVEL_FIELDS:
        value = f.get(key)
        if value not in (None, "") and not isinstance(value, dict):
            return str(value)
    return None


def is_error(record: LogRecord) -> bool:
    """An error-level record, or any record that carries an exception."""
    fields = _Fields(record)
    level = level_of(record, fields)
    if level is not None:
        if level.lower() in ERROR_WORDS:
            return True
    else:
        levelno = record.fields.get("levelno")
        if isinstance(levelno, int) and levelno >= 40:
            return True
        if not record.fields and _TEXT_LEVEL.search(record.message[:200]):
            return True  # plain text: "2026-10-17 12:00:01 ERROR app: payment failed"
    return extract_error(record, fields) is not None


def join_multiline(lines: Iterable[str]) -> Iterator[str]:
    """Join plain-text tracebacks that span several lines back into one event each.

    A traceback continues its event from ``Traceback (most recent call last):`` through the
    indented frame lines to the ``SomeError: message`` line, including chained tracebacks.
    JSON lines are always whole events.
    """
    event: list[str] = []
    in_frames = False  # between "Traceback ..." and the exception line
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not event or stripped.startswith("{"):
            continuation = False
        elif stripped.startswith(TRACEBACK_START):
            continuation, in_frames = True, True
        elif stripped in CHAIN_MARKERS:
            continuation = True
        elif in_frames and (not stripped or line[:1].isspace()):
            continuation = True
        elif in_frames and _EXC_LINE.match(line):
            continuation, in_frames = True, False
        elif not stripped:
            continue  # blank lines separate chained tracebacks; never an event of their own
        else:
            continuation = False
        if continuation:
            if stripped:
                event.append(line)
            continue
        if event:
            yield "\n".join(event)
        event = [line] if stripped else []
        in_frames = stripped.endswith(TRACEBACK_START)
    if event:
        yield "\n".join(event)
