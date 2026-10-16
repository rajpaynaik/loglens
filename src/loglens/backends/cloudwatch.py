"""AWS CloudWatch Logs adapter using ``FilterLogEvents``.

Needs boto3: ``pip install 'loglens[cloudwatch]'``. Credentials come from the usual AWS
chain (env vars, ``~/.aws``, instance role), optionally narrowed to a named ``profile``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..models import LogRecord
from .base import LogBackend, TimeRange, parse_line


class CloudWatchBackend(LogBackend):
    name = "cloudwatch"

    def __init__(
        self,
        log_group: str,
        *,
        region: str | None = None,
        profile: str | None = None,
        filter_pattern: str | None = None,
        log_stream_prefix: str | None = None,
        client: Any = None,
    ) -> None:
        self.log_group = log_group
        self.filter_pattern = filter_pattern  # CloudWatch syntax, e.g. '{ $.service = "checkout" }'
        self.log_stream_prefix = log_stream_prefix
        if client is None:
            try:
                import boto3
            except ImportError:
                raise RuntimeError(
                    "the cloudwatch backend needs boto3: pip install 'loglens[cloudwatch]'"
                ) from None
            client = boto3.Session(profile_name=profile, region_name=region).client("logs")
        self.client = client

    def build_pattern(self, contains: str | None = None) -> str | None:
        if not contains:
            return self.filter_pattern
        term = json.dumps(contains)  # quoted term = exact phrase in CloudWatch syntax
        if self.filter_pattern and not self.filter_pattern.lstrip().startswith("{"):
            return f"{self.filter_pattern} {term}"  # space-separated terms are ANDed
        # JSON patterns can't be combined with a text term; filter client-side instead.
        return self.filter_pattern or term

    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        params: dict[str, Any] = {
            "logGroupName": self.log_group,
            "startTime": int(time_range.start.timestamp() * 1000),
            "endTime": int(time_range.end.timestamp() * 1000),
        }
        pattern = self.build_pattern(contains)
        if pattern:
            params["filterPattern"] = pattern
        if self.log_stream_prefix:
            params["logStreamNamePrefix"] = self.log_stream_prefix

        records: list[LogRecord] = []
        while len(records) < limit:
            page = self.client.filter_log_events(**params, limit=min(10_000, limit - len(records)))
            for record in self.parse_events(page.get("events", [])):
                if contains and contains not in record.message:
                    continue
                records.append(record)
            token = page.get("nextToken")
            if not token:
                break
            params["nextToken"] = token
        return records[:limit]

    @staticmethod
    def parse_events(events: list[dict[str, Any]]) -> list[LogRecord]:
        records: list[LogRecord] = []
        for event in events:
            ts = datetime.fromtimestamp(event["timestamp"] / 1000, tz=timezone.utc)
            record = parse_line(event.get("message", ""), timestamp=ts)
            if "logStreamName" in event:
                record.fields = {"logStreamName": event["logStreamName"], **record.fields}
            records.append(record)
        return records
