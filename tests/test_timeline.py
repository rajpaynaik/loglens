import json
from datetime import datetime, timedelta, timezone

import pytest
from test_backends import stub_server

from logsite.backends import FileBackend, LokiBackend, OpenSearchBackend, TimeRange, parse_line
from logsite.cli import main
from logsite.context import mentions
from logsite.index import index_source
from logsite.models import LogRecord
from logsite.timeline import build_timeline

TID = "4bf92f3577b34da6a3ce929d0e0e4703"
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

CODE = """
import structlog
log = structlog.get_logger()

def checkout(order, card):
    log.info("checkout_started", order_id=order.id)
    log.error("inventory_timeout", order_id=order.id)
    log.info("charging card", card=card)
"""


def rec(ms, message, **fields):
    return LogRecord(timestamp=T0 + timedelta(milliseconds=ms), message=message, fields=fields)


def test_mentions_matches_fields_synonyms_and_whole_words():
    assert mentions(rec(0, "x", trace_id=TID), "trace_id", TID)
    assert mentions(rec(0, "x", traceId=TID), "trace_id", TID)
    assert mentions(rec(0, "x", trace={"id": TID}), "trace_id", TID)
    assert not mentions(rec(0, "x", trace_id=TID + "9"), "trace_id", TID)  # a different id
    assert mentions(rec(0, f"handled trace={TID} ok"), "trace_id", TID)  # no field: message
    assert not mentions(rec(0, f"trace={TID}9"), "trace_id", TID)  # prefix of another id
    assert not mentions(rec(0, f"see {TID}", trace_id="other"), "trace_id", TID)


def records():
    return [
        rec(2015, "POST /checkout 504", trace_id=TID, app="gateway", level="error"),
        rec(0, "POST /checkout", trace_id=TID, app="gateway", level="info"),
        rec(3, "checkout_started", trace_id=TID, pathname="/srv/app.py", lineno=6),
        rec(5, "reserve request", traceId=TID, **{"service.name": "inventory"}, severity="INFO"),
        rec(2005, "db pool exhausted", traceId=TID, service={"name": "inventory"}, severity="WARN"),
        rec(2010, "inventory_timeout", trace_id=TID, pathname="/srv/app.py", lineno=7),
        rec(2011, "charging card 4111 1111 1111 1111", trace_id=TID, lineno=8, pathname="app.py"),
        LogRecord(timestamp=None, message=f"late {TID}", fields={"logStreamName": "batch-1"}),
    ]


def test_build_timeline_orders_attributes_and_redacts():
    tl = build_timeline(records(), index_source(CODE, "app.py"), "trace_id", TID, here="checkout")
    got = [(e.service, e.level, e.message, e.site.id if e.site else None) for e in tl.entries]
    assert got == [
        ("gateway", "info", "POST /checkout", None),
        ("checkout", "info", "checkout_started", "app.py:6"),  # no service field: ours
        ("inventory", "INFO", "reserve request", None),
        ("inventory", "WARN", "db pool exhausted", None),  # nested service.name
        ("checkout", "error", "inventory_timeout", "app.py:7"),  # level from the log call
        ("checkout", "info", "charging card ************1111", "app.py:8"),  # masked
        ("gateway", "error", "POST /checkout 504", None),
        ("batch-1", None, f"late {TID}", None),  # no timestamp: last
    ]
    assert tl.services == ["gateway", "checkout", "inventory", "batch-1"]
    assert tl.duration == timedelta(milliseconds=2015)
    assert tl.first_problem.message == "db pool exhausted"  # the cause shows up first
    assert tl.first_error.message == "inventory_timeout"


def test_default_find_records_filters_exactly(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        {"timestamp": now, "message": "a", "trace_id": TID},
        {"timestamp": now, "message": "b", "trace_id": TID + "9"},  # longer id, same prefix
        {"timestamp": now, "message": f"c {TID}"},  # only in the text
        {"timestamp": now, "message": "d", "trace_id": "other"},
    ]
    (tmp_path / "p.log").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    found = FileBackend(tmp_path / "p.log").find_records("trace_id", TID, TimeRange.last(1))
    assert [r.message for r in found] == ["a", f"c {TID}"]


def test_opensearch_find_records_searches_every_field_in_time_order():
    hit = {"@timestamp": "2026-10-17T12:00:00Z", "message": "x", "trace": {"id": TID}}

    with stub_server(lambda path, body: {"hits": {"hits": [{"_source": hit}]}}) as (url, reqs):
        found = OpenSearchBackend(url, "logs-*").find_records("trace_id", TID, TimeRange.last(1))
    assert len(found) == 1
    ((_, _, body),) = reqs
    assert {"multi_match": {"query": TID, "type": "phrase", "fields": ["*"], "lenient": True}} in (
        body["query"]["bool"]["filter"]
    )
    assert body["sort"] == [{"@timestamp": {"order": "asc"}}]


def test_loki_find_records_uses_a_line_filter():
    line = json.dumps({"msg": "x", "trace_id": TID})
    payload = {"data": {"result": [{"stream": {"app": "a"}, "values": [["1", line]]}]}}
    with stub_server(lambda path, body: payload) as (url, reqs):
        found = LokiBackend(url, '{env="prod"}').find_records("trace_id", TID, TimeRange.last(1))
    assert len(found) == 1 and found[0].fields["app"] == "a"
    assert f'|= "{TID}"' in reqs[0][0].replace("%7C", "|").replace("%3D", "=").replace(
        "%22", '"'
    ).replace("+", " ")


def project(tmp_path):
    now = datetime.now(timezone.utc)

    def line(ms, **f):
        return json.dumps(
            {
                "timestamp": (now - timedelta(seconds=60) + timedelta(milliseconds=ms)).isoformat(),
                **f,
            }
        )

    (tmp_path / "app.py").write_text(CODE)
    (tmp_path / "ours.log").write_text(
        line(3, message="checkout_started", trace_id=TID, lineno=6, pathname="app.py") + "\n"
    )
    (tmp_path / "all.log").write_text(
        "\n".join(
            [
                line(0, message="POST /checkout", trace_id=TID, app="gateway", level="info"),
                line(3, message="checkout_started", trace_id=TID, lineno=6, pathname="app.py"),
                line(9, message="inventory_timeout", trace_id=TID, lineno=7, pathname="app.py"),
                line(12, message="unrelated", trace_id="other", app="gateway"),
            ]
        )
        + "\n"
    )
    (tmp_path / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "ours.log"\n'
        '[timeline]\nservice = "checkout"\n'
        '[timeline.backend]\ntype = "file"\npath = "all.log"\n'
    )


def test_cli_trace_uses_the_wider_timeline_backend(tmp_path, monkeypatch, capsys):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["trace", TID]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == f"trace_id={TID}: 3 line(s) from 2 service(s) over 0.009s"
    assert "gateway" in lines[2] and "POST /checkout" in lines[2]
    assert "checkout_started  app.py:6" in lines[3]
    assert "! checkout" in lines[4] and "app.py:7" in lines[4]
    assert "first error: checkout at app.py:7 (error): inventory_timeout" in captured.err

    assert main(["trace", TID, "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["services"] == ["gateway", "checkout"] and len(out["entries"]) == 3

    assert main(["trace", "missing-id"]) == 1


def test_mcp_request_timeline(tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import call

    project(tmp_path)
    err, out = call(tmp_path, "request_timeline", {"value": TID})
    assert not err, out
    assert [e["site"] for e in out["entries"]] == [None, "app.py:6", "app.py:7"]
    assert out["first_error"]["site"] == "app.py:7"
    assert out["first_problem"] == out["first_error"]


def test_parse_line_keeps_trace_fields():
    r = parse_line(json.dumps({"msg": "x", "trace_id": TID}))
    assert mentions(r, "trace_id", TID)
