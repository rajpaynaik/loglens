"""Find secrets and personal data in production log contents, and the log call that leaked them.

LL002 guesses from variable names in the code; this looks at what was actually written.
Matched values are always masked in the output so the report itself doesn't leak them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from .matching import Matcher
from .models import LogRecord, LogSite


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


_CARD_CONTEXT = re.compile(
    r"card|\bcc|\bpan\b|credit|debit|visa|master|amex|payment|\bpay", re.IGNORECASE
)
_GROUPED = re.compile(r"^\d{4}([ -])\d{4}\1\d{4}\1\d{1,7}$|^\d{4}([ -])\d{6}\2\d{5}$")


def _card(value: str, context: str = "") -> bool:
    """Luhn-valid, plausible prefix, and either grouped like a card or near a card word.

    A bare 16-digit number is otherwise indistinguishable from an order or trace ID.
    """
    digits = re.sub(r"[ -]", "", value)
    if not (_GROUPED.match(value) or _CARD_CONTEXT.search(context)):
        return False
    # Card numbers start with 2-6 (Mastercard, Amex, Visa, Discover ...). Epoch timestamps
    # start with 1, and ~10% of random digit strings pass Luhn, so the prefix matters.
    return (
        13 <= len(digits) <= 19 and digits[0] in "23456" and len(set(digits)) > 1 and _luhn(digits)
    )


def _iban(value: str, context: str = "") -> bool:
    s = value.replace(" ", "").upper()
    s = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


@dataclass(frozen=True)
class Detector:
    kind: str
    category: str  # "secret" | "pii"
    pattern: re.Pattern[str]
    validate: Callable[[str, str], bool] | None = None  # (value, preceding text + field)
    group: int = 0  # regex group holding the sensitive value


def _rx(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


DETECTORS: list[Detector] = [
    Detector("private_key", "secret", _rx(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    Detector("aws_access_key", "secret", _rx(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Detector("github_token", "secret", _rx(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    Detector("slack_token", "secret", _rx(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    Detector("stripe_key", "secret", _rx(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    Detector("google_api_key", "secret", _rx(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Detector(
        "jwt", "secret", _rx(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
    ),
    Detector("bearer_token", "secret", _rx(r"\bBearer\s+([A-Za-z0-9._~+/-]{20,}=*)"), group=1),
    Detector(
        "password",
        "secret",
        _rx(
            r"\b(?:password|passwd|pwd|secret|api_?key|access_?token|client_secret)"
            r"""["']?\s*[:=]\s*["']?([^\s"',;&}]{4,})""",
            re.IGNORECASE,
        ),
        group=1,
    ),
    Detector("email", "pii", _rx(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    Detector("card_number", "pii", _rx(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"), _card),
    Detector(
        "iban", "pii", _rx(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"), _iban
    ),
    Detector("us_ssn", "pii", _rx(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
]
KINDS = {d.kind: d.category for d in DETECTORS}

# Fields that describe where/when a log came from; never scanned.
META_FIELDS = {
    "timestamp",
    "time",
    "ts",
    "@timestamp",
    "level",
    "levelname",
    "severity",
    "pathname",
    "filename",
    "file",
    "lineno",
    "line",
    "module",
    "funcName",
    "logger",
    "name",
    "logStreamName",
}
META_PREFIXES = ("code.", "log.origin.", "log.logger", "log.level")


def mask(kind: str, value: str) -> str:
    """Show just enough to recognise the value: ``a***@example.com``, ``************1111``."""
    if kind == "email":
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    if kind in ("card_number", "iban"):
        compact = re.sub(r"[ -]", "", value)
        return "*" * (len(compact) - 4) + compact[-4:]
    if kind == "us_ssn":
        return f"***-**-**{value[-2:]}"
    if kind == "private_key":
        return value  # just the header line
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-2:]}"


@dataclass
class LeakConfig:
    ignore: set[str] = field(default_factory=set)  # detector kinds to skip, e.g. {"email"}
    allow: list[str] = field(default_factory=list)  # regexes for values that are fine
    ignore_fields: set[str] = field(default_factory=set)

    @classmethod
    def from_dict(cls, data: Mapping) -> LeakConfig:
        unknown = set(data.get("ignore", [])) - set(KINDS)
        if unknown:
            raise ValueError(f"unknown leak kind(s) {sorted(unknown)}; known: {sorted(KINDS)}")
        return cls(
            ignore=set(data.get("ignore", [])),
            allow=list(data.get("allow", [])),
            ignore_fields=set(data.get("ignore_fields", [])),
        )


@dataclass(frozen=True)
class Finding:
    kind: str
    category: str
    where: str  # "message" or the field name
    masked: str


def _matches(text: str, cfg: LeakConfig, where: str) -> Iterable[tuple[Detector, re.Match]]:
    allow = [re.compile(a) for a in cfg.allow]
    for d in DETECTORS:
        if d.kind in cfg.ignore:
            continue
        for m in d.pattern.finditer(text):
            value = m.group(d.group)
            context = f"{where} {text[max(0, m.start() - 40) : m.start()]}"
            if d.validate and not d.validate(value, context):
                continue
            if not any(a.search(value) for a in allow):
                yield d, m


def scan_text(text: str, cfg: LeakConfig | None = None, where: str = "message") -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for d, m in _matches(text, cfg or LeakConfig(), where):
        value = m.group(d.group)
        if (d.kind, value) not in seen:
            seen.add((d.kind, value))
            out.append(Finding(d.kind, d.category, where, mask(d.kind, value)))
    return out


def redact(text: str, cfg: LeakConfig | None = None, where: str = "message") -> str:
    """``text`` with every detected secret / personal value replaced by its masked form."""
    spans = sorted(
        (m.start(d.group), m.end(d.group), mask(d.kind, m.group(d.group)))
        for d, m in _matches(text, cfg or LeakConfig(), where)
    )
    out, pos = [], 0
    for start, end, masked in spans:
        if start < pos:
            continue  # overlaps an earlier match
        out += [text[pos:start], masked]
        pos = end
    return "".join(out) + text[pos:]


def scan_record(record: LogRecord, cfg: LeakConfig | None = None) -> list[Finding]:
    """Scan the message and every non-metadata string field of a record."""
    cfg = cfg or LeakConfig()
    findings = scan_text(record.message, cfg)
    for key, value in record.fields.items():
        if (
            not isinstance(value, str)
            or value == record.message
            or key in META_FIELDS
            or key in cfg.ignore_fields
            or key.startswith(META_PREFIXES)
        ):
            continue
        findings += scan_text(value, cfg, where=key)
    return findings


@dataclass
class SiteLeaks:
    """Leaks attributed to one log call (``site`` is None when no call matched)."""

    site: LogSite | None
    records: int = 0  # records with at least one finding
    kinds: dict[str, int] = field(default_factory=dict)  # kind -> occurrences
    samples: dict[str, Finding] = field(default_factory=dict)  # kind -> first finding

    @property
    def id(self) -> str:
        return self.site.id if self.site else "<unknown source>"


def find_leaks(
    sites: Iterable[LogSite], records: Iterable[LogRecord], cfg: LeakConfig | None = None
) -> tuple[list[SiteLeaks], int]:
    """Group findings by log call, worst first. Also returns how many records were scanned."""
    matcher = Matcher(sites)
    by_site: dict[str, SiteLeaks] = {}
    scanned = 0
    for record in records:
        scanned += 1
        findings = scan_record(record, cfg)
        if not findings:
            continue
        site = matcher.match(record)
        entry = by_site.setdefault(site.id if site else "", SiteLeaks(site))
        entry.records += 1
        for f in findings:
            entry.kinds[f.kind] = entry.kinds.get(f.kind, 0) + 1
            entry.samples.setdefault(f.kind, f)
    ranked = sorted(
        by_site.values(),
        key=lambda e: (any(KINDS[k] == "secret" for k in e.kinds), e.records),
        reverse=True,
    )
    return ranked, scanned
