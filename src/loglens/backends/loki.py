"""Grafana Loki adapter using the HTTP ``query_range`` API (stdlib only)."""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..models import LogRecord
from .base import LogBackend, TimeRange, parse_line


class LokiBackend(LogBackend):
    name = "loki"

    def __init__(
        self,
        url: str,
        selector: str,
        *,
        username: str | None = None,
        password: str | None = None,
        tenant_id: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.selector = selector  # e.g. '{app="checkout"}'
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        if username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"
        if tenant_id:
            self.headers["X-Scope-OrgID"] = tenant_id

    def build_query(self, contains: str | None = None) -> str:
        query = self.selector
        if contains:
            query += " |= " + json.dumps(contains)
        return query

    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        params = {
            "query": self.build_query(contains),
            "start": str(int(time_range.start.timestamp() * 1e9)),
            "end": str(int(time_range.end.timestamp() * 1e9)),
            "limit": str(limit),
            "direction": "backward",
        }
        url = f"{self.url}/loki/api/v1/query_range?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.load(resp)
        return self.parse_response(payload)

    @staticmethod
    def parse_response(payload: dict) -> list[LogRecord]:
        records: list[LogRecord] = []
        for stream in payload.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts_ns, line in stream.get("values", []):
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
                record = parse_line(line, timestamp=ts)
                record.fields = {**labels, **record.fields}
                records.append(record)
        return records
