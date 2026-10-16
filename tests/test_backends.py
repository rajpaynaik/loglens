import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from loglens.backends import (
    CloudWatchBackend,
    FileBackend,
    LokiBackend,
    OpenSearchBackend,
    TimeRange,
    from_config,
)
from loglens.index import index_project

DEMO = Path(__file__).parent.parent / "examples" / "demo_app"


def test_file_backend_counts_demo_app():
    sites = index_project(DEMO)
    counts = FileBackend(DEMO / "sample.log").count_by_line(sites, TimeRange.last(24 * 365 * 10))
    assert counts == {"app/checkout.py:7": 2, "app/checkout.py:15": 3, "app/checkout.py:11": 1}


def test_loki_parse_response_merges_labels():
    payload = {
        "data": {
            "result": [
                {
                    "stream": {"app": "checkout"},
                    "values": [
                        ["1760000000000000000", '{"msg": "hi", "lineno": 3}'],
                        ["1760000000000000001", "plain"],
                    ],
                }
            ]
        }
    }
    recs = LokiBackend.parse_response(payload)
    assert recs[0].message == "hi" and recs[0].fields["app"] == "checkout"
    assert recs[0].source_line == 3
    assert recs[1].message == "plain"


def test_loki_query_builder():
    b = LokiBackend("http://loki:3100/", '{app="x"}')
    assert b.build_query('say "hi"') == '{app="x"} |= "say \\"hi\\""'


def _hit(source):
    return {"_index": "logs", "_source": source}


OS_PAYLOAD = {
    "hits": {
        "hits": [
            # ECS (ecs-logging-python): nested objects, line info under log.origin
            _hit(
                {
                    "@timestamp": "2026-10-14T10:00:00Z",
                    "message": "charging order 41 for 1999 cents",
                    "log": {"origin": {"file": {"name": "checkout.py", "line": 7}}},
                    "service": {"name": "checkout"},
                }
            ),
            # The message itself is a JSON log line
            _hit({"@timestamp": "2026-10-14T10:00:01Z", "message": '{"msg": "hi", "lineno": 3}'}),
            _hit({"@timestamp": "2026-10-14T10:00:02Z"}),  # no message: skipped
        ]
    }
}


def test_opensearch_parse_response():
    recs = OpenSearchBackend.parse_response(OS_PAYLOAD)
    assert len(recs) == 2
    assert recs[0].source_file == "checkout.py" and recs[0].source_line == 7
    assert recs[0].fields["service.name"] == "checkout"
    assert recs[0].timestamp.year == 2026
    assert recs[1].message == "hi" and recs[1].source_line == 3


def test_opensearch_query_against_a_live_http_server():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = json.dumps(OS_PAYLOAD).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        b = from_config(
            {
                "type": "elasticsearch",
                "url": f"http://127.0.0.1:{httpd.server_port}/",
                "index": "logs-*",
                "query": "service.name:checkout",
                "api_key": "k",
            }
        )
        recs = b.query_logs(TimeRange.last(1), contains="charging", limit=10)
    finally:
        httpd.shutdown()
    assert len(recs) == 2
    assert seen["path"] == "/logs-*/_search" and seen["auth"] == "ApiKey k"
    body = seen["body"]
    assert body["size"] == 10 and body["sort"] == [{"@timestamp": {"order": "desc"}}]
    filters = body["query"]["bool"]["filter"]
    assert "range" in filters[0] and filters[1] == {
        "query_string": {"query": "service.name:checkout"}
    }
    assert filters[2] == {"match_phrase": {"message": "charging"}}


class FakeLogs:
    """Stands in for a boto3 ``logs`` client; serves events in pages of two."""

    def __init__(self, events):
        self.events, self.calls = events, []

    def filter_log_events(self, **params):
        self.calls.append(params)
        start = int(params.get("nextToken", 0))
        page = {"events": self.events[start : start + 2]}
        if start + 2 < len(self.events):
            page["nextToken"] = str(start + 2)
        return page


CW_EVENTS = [
    {
        "timestamp": 1760436000000 + i,
        "logStreamName": "web-1",
        "message": json.dumps({"message": f"reserving item SKU-{i}", "lineno": 15}),
    }
    for i in range(5)
]


def test_cloudwatch_paginates_and_parses():
    fake = FakeLogs(CW_EVENTS)
    b = CloudWatchBackend("/app/checkout", filter_pattern="ERROR", client=fake)
    recs = b.query_logs(TimeRange.last(24 * 365 * 10), limit=4)
    assert [r.message for r in recs] == [f"reserving item SKU-{i}" for i in range(4)]
    assert recs[0].fields["logStreamName"] == "web-1" and recs[0].source_line == 15
    assert len(fake.calls) == 2 and fake.calls[1]["nextToken"] == "2"
    assert fake.calls[0]["logGroupName"] == "/app/checkout"
    assert fake.calls[0]["filterPattern"] == "ERROR"


def test_cloudwatch_counts_demo_app():
    sites = index_project(DEMO)
    b = CloudWatchBackend("/app/checkout", client=FakeLogs(CW_EVENTS))
    assert b.count_by_line(sites, TimeRange.last(24 * 365 * 10)) == {"app/checkout.py:15": 5}


def test_cloudwatch_filter_patterns():
    def cw(pattern=None):
        return CloudWatchBackend("g", filter_pattern=pattern, client=FakeLogs([]))

    assert cw().build_pattern("a b") == '"a b"'
    assert cw("ERROR").build_pattern("x") == 'ERROR "x"'
    json_pattern = '{ $.service = "checkout" }'
    # JSON patterns can't take an extra text term; the backend filters those client-side
    assert cw(json_pattern).build_pattern("x") == json_pattern


def test_from_config_errors():
    assert from_config({}) is None
    with pytest.raises(ValueError, match="supported: file, loki, opensearch"):
        from_config({"type": "splunk"})
    with pytest.raises(ValueError, match="'loki' needs 'url'"):
        from_config({"type": "loki"})
