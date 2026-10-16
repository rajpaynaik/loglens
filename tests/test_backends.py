from pathlib import Path

from loglens.backends import FileBackend, LokiBackend, TimeRange
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
