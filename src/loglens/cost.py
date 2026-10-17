"""Turn per-line usage (events, bytes) into an estimated monthly cost.

Log vendors bill on ingested volume (per GB), indexed events (per million), or both, so
both prices are configurable. Usage measured over the configured window is scaled to a
30-day month.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .models import LogSite, Usage

HOURS_PER_MONTH = 30 * 24
GB = 1_000_000_000  # vendors bill decimal gigabytes


@dataclass
class CostConfig:
    price_per_gb: float = 0.0  # e.g. 0.50 for CloudWatch Logs ingestion
    price_per_million_events: float = 0.0  # e.g. indexing charges on Datadog-style plans
    currency: str = "$"

    @property
    def priced(self) -> bool:
        return bool(self.price_per_gb or self.price_per_million_events)

    @classmethod
    def from_dict(cls, data: Mapping) -> CostConfig:
        cfg = cls(
            price_per_gb=float(data.get("price_per_gb", 0)),
            price_per_million_events=float(data.get("price_per_million_events", 0)),
            currency=str(data.get("currency", "$")),
        )
        if cfg.price_per_gb < 0 or cfg.price_per_million_events < 0:
            raise ValueError("[cost] prices can't be negative")
        return cfg


@dataclass(frozen=True)
class LineCost:
    site: LogSite
    events_per_month: float
    bytes_per_month: float
    cost_per_month: float
    avg_bytes: float
    estimated: bool

    @property
    def gb_per_month(self) -> float:
        return self.bytes_per_month / GB


def monthly_costs(
    sites: Iterable[LogSite],
    usage: Mapping[str, Usage],
    window_hours: float,
    cfg: CostConfig,
) -> list[LineCost]:
    """Cost per log call, most expensive first (by volume when no price is configured)."""
    scale = HOURS_PER_MONTH / window_hours
    by_id = {s.id: s for s in sites}
    out: list[LineCost] = []
    for sid, u in usage.items():
        if sid not in by_id or not u.count:
            continue
        events, size = u.count * scale, u.bytes * scale
        cost = size / GB * cfg.price_per_gb + events / 1e6 * cfg.price_per_million_events
        out.append(
            LineCost(
                site=by_id[sid],
                events_per_month=events,
                bytes_per_month=size,
                cost_per_month=cost,
                avg_bytes=u.bytes / u.count,
                estimated=u.estimated,
            )
        )
    out.sort(key=lambda c: (c.cost_per_month, c.bytes_per_month), reverse=True)
    return out


def format_money(amount: float, currency: str = "$") -> str:
    if amount >= 1000:
        return f"{currency}{amount:,.0f}"
    if amount >= 1:
        return f"{currency}{amount:,.2f}"
    return f"{currency}{amount:.2f}" if amount >= 0.01 else f"<{currency}0.01"


def format_bytes(n: float) -> str:
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n:.0f} B"
