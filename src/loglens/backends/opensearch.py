"""OpenSearch / Elasticsearch adapter using the ``_search`` REST API (stdlib only)."""

from __future__ import annotations

import base64
import json
import urllib.request
from collections.abc import Mapping
from typing import Any

from ..models import LogRecord
from .base import LogBackend, TimeRange, _parse_ts, parse_line


class OpenSearchBackend(LogBackend):
    name = "opensearch"

    def __init__(
        self,
        url: str,
        index: str,
        *,
        query: str | None = None,
        timestamp_field: str = "@timestamp",
        message_field: str = "message",
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.index = index  # e.g. "logs-checkout-*"
        self.query = query  # optional Lucene query_string, e.g. 'service.name:checkout'
        self.timestamp_field = timestamp_field
        self.message_field = message_field
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"ApiKey {api_key}"
        elif username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def build_query(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> dict[str, Any]:
        filters: list[dict[str, Any]] = [
            {
                "range": {
                    self.timestamp_field: {
                        "gte": time_range.start.isoformat(),
                        "lte": time_range.end.isoformat(),
                    }
                }
            }
        ]
        if self.query:
            filters.append({"query_string": {"query": self.query}})
        if contains:
            filters.append({"match_phrase": {self.message_field: contains}})
        return {
            "size": limit,
            "sort": [{self.timestamp_field: {"order": "desc"}}],
            "query": {"bool": {"filter": filters}},
        }

    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        body = json.dumps(self.build_query(time_range, contains, limit)).encode()
        req = urllib.request.Request(
            f"{self.url}/{self.index}/_search", data=body, headers=self.headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.load(resp)
        return self.parse_response(payload, self.timestamp_field, self.message_field)

    @staticmethod
    def parse_response(
        payload: dict, timestamp_field: str = "@timestamp", message_field: str = "message"
    ) -> list[LogRecord]:
        records: list[LogRecord] = []
        for hit in payload.get("hits", {}).get("hits", []):
            fields = flatten(hit.get("_source", {}))
            ts = _parse_ts(fields.get(timestamp_field))
            message = fields.get(message_field)
            if message is None:
                continue
            record = parse_line(str(message), timestamp=ts)
            record.fields = {**fields, **record.fields}
            records.append(record)
        return records


def flatten(doc: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """``{"log": {"origin": {"file": {"line": 3}}}}`` -> ``{"log.origin.file.line": 3}``.

    Documents can mix nested objects and dotted keys (ECS does); both end up dotted.
    """
    out: dict[str, Any] = {}
    for key, value in doc.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(flatten(value, f"{name}."))
        else:
            out[name] = value
    return out
