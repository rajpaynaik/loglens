import json
from pathlib import Path

import pytest
from test_backends import CW_EVENTS, DEMO, FakeLogs, _hit, stub_server

from logsite.backends import (
    CloudWatchBackend,
    FileBackend,
    LokiBackend,
    OpenSearchBackend,
    TimeRange,
)
from logsite.cli import main
from logsite.cost import CostConfig, format_bytes, format_money, monthly_costs
from logsite.index import index_project, index_source
from logsite.models import Usage

ALL_TIME = TimeRange.last(24 * 365 * 10)


def test_monthly_costs_scale_price_and_rank():
    sites = index_source('logger.info("a")\nlogger.info("b")\nlogger.info("c")\n', "m.py")
    usage = {
        "m.py:1": Usage(count=1_000, bytes=2_000_000_000),  # 2 GB per 24h
        "m.py:2": Usage(count=10_000_000, bytes=1_000_000_000, estimated=True),
        "m.py:3": Usage(count=0, bytes=0),  # never logged: left out
    }
    cfg = CostConfig(price_per_gb=0.5, price_per_million_events=0.1)
    costs = monthly_costs(sites, usage, window_hours=24, cfg=cfg)
    # many small events can cost more than few big ones once events are priced
    assert [c.site.id for c in costs] == ["m.py:2", "m.py:1"]
    second, first = costs
    assert first.gb_per_month == pytest.approx(60)  # 2 GB/day x 30
    assert first.cost_per_month == pytest.approx(60 * 0.5 + 0.03 * 0.1)
    assert second.events_per_month == 300_000_000
    assert second.cost_per_month == pytest.approx(30 * 0.5 + 300 * 0.1)
    assert second.estimated and second.avg_bytes == 100
    # unpriced: still ranked, by volume
    unpriced = monthly_costs(sites, usage, 24, CostConfig())
    assert [c.cost_per_month for c in unpriced] == [0, 0]
    assert [c.site.id for c in unpriced] == ["m.py:1", "m.py:2"]


def test_cost_config_and_formatting():
    assert not CostConfig().priced and CostConfig.from_dict({"price_per_gb": 0.5}).priced
    with pytest.raises(ValueError, match="negative"):
        CostConfig.from_dict({"price_per_gb": -1})
    assert [format_money(x) for x in (1234.5, 12.345, 0.5, 0.001)] == [
        "$1,234",
        "$12.35",
        "$0.50",
        "<$0.01",
    ]
    assert [format_bytes(x) for x in (12, 1_500, 2_500_000_000)] == ["12 B", "1.5 KB", "2.5 GB"]


def test_file_backend_measures_stored_line_sizes():
    sizes: dict[int, int] = {}
    for raw in (DEMO / "sample.log").read_text().splitlines():
        if '"lineno": 15' in raw:
            sizes[15] = sizes.get(15, 0) + len(raw.encode())
    usage = FileBackend(DEMO / "sample.log").usage_by_line(index_project(DEMO), ALL_TIME)
    assert usage["app/checkout.py:15"] == Usage(count=3, bytes=sizes[15])
    assert not any(u.estimated for u in usage.values())


def test_cloudwatch_adds_per_event_overhead():
    b = CloudWatchBackend("/app", client=FakeLogs(CW_EVENTS))
    u = b.usage_by_line(index_project(DEMO), ALL_TIME)["app/checkout.py:15"]
    assert u.count == 5
    assert u.bytes == sum(len(e["message"].encode()) + 26 for e in CW_EVENTS)


def test_loki_aggregate_usage_uses_count_and_bytes_queries():
    def respond(path, body):
        if "/loki/api/v1/query?" in path:
            value = "580890" if "bytes_over_time" in path else "6000"
            metric = {"pathname": "/srv/app/checkout.py", "lineno": "7"}
            return {"data": {"result": [{"metric": metric, "value": [0, value]}]}}
        line = "2026-10-14 10:00:05 ERROR payment failed for order 42: card declined"
        return {"data": {"result": [{"stream": {}, "values": [["1760000000000000000", line]]}]}}

    with stub_server(respond) as (url, requests):
        b = LokiBackend(url, '{app="x"}', aggregate=True)
        usage = b.usage_by_line(index_project(DEMO), TimeRange.last(1))
    assert usage["app/checkout.py:7"] == Usage(count=6000, bytes=580890)
    assert usage["app/checkout.py:11"].count == 1 and usage["app/checkout.py:11"].bytes > 0
    assert sum("bytes_over_time" in p for p, _, _ in requests) == 1


def test_opensearch_aggregate_usage_estimates_from_sample():
    small = {
        "@timestamp": "2026-10-14T10:00:00Z",
        "message": "reserving item SKU-1",
        "log": {"origin": {"file": {"name": "checkout.py", "line": 15}}},
    }

    def respond(path, body):
        if "aggs" in body:
            key = {"file": "checkout.py", "line": 15}
            return {"aggregations": {"by_line": {"buckets": [{"key": key, "doc_count": 1000}]}}}
        if body["query"]["bool"].get("must_not"):  # documents without file/line: none here
            return {"hits": {"hits": []}}
        return {"hits": {"hits": [_hit(small)] * 2}}

    with stub_server(respond) as (url, _):
        b = OpenSearchBackend(url, "logs", aggregate=True)
        u = b.usage_by_line(index_project(DEMO), TimeRange.last(1))["app/checkout.py:15"]
    assert u.count == 1000 and u.estimated
    assert u.bytes == 1000 * len(json.dumps(small).encode())


def test_cli_cost(monkeypatch, capsys):
    monkeypatch.chdir(DEMO)
    assert main(["cost", "--price-per-gb", "0.5", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["priced"] and out["currency"] == "$"
    assert out["lines"][0]["id"] == "app/checkout.py:15"  # most bytes
    assert all(not line["estimated"] for line in out["lines"])
    assert main(["cost", "--top", "2"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].split() == [
        "per",
        "month",
        "volume",
        "events",
        "avg",
        "line",
    ]
    assert "set [cost] price_per_gb" in captured.err


def test_mcp_log_costs(tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import call

    project = tmp_path / "p"
    (project / "app").mkdir(parents=True)
    (project / "app" / "checkout.py").write_text((DEMO / "app" / "checkout.py").read_text())
    (project / "sample.log").write_text((DEMO / "sample.log").read_text())
    (project / "logsite.toml").write_text(
        'window_hours = 87600\n[backend]\ntype = "file"\npath = "sample.log"\n'
        "[cost]\nprice_per_gb = 0.5\n"
    )
    err, out = call(Path(project), "log_costs", {"top": 1})
    assert not err, out
    assert out["priced"] and [line["id"] for line in out["lines"]] == ["app/checkout.py:15"]
