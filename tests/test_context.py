import json

import pytest

from logsite.cli import main
from logsite.context import (
    detect_in_source,
    has_field,
    measure_coverage,
    overall_coverage,
    satisfied,
)
from logsite.index import index_source, is_dependency_call
from logsite.lint import ContextRules, LintConfig, lint_paths, lint_source, lint_volume
from logsite.models import LogRecord

SITUATIONS_SRC = """
import time
def a(url):
    for attempt in range(3):
        try:
            return requests.get(url)
        except Exception:
            logger.warning("get failed, retrying")
    logger.error("gave up")

def b():
    while True:
        try:
            go()
            break
        except ValueError:
            logger.info("again")
            time.sleep(1)

@retry(stop=stop_after_attempt(3))
def c():
    logger.info("trying")

def d(cur, q):
    try:
        cur.execute(q)
    except TimeoutError:
        logger.error("database timeout")
    try:
        x = {}.get("a")
    except KeyError:
        logger.error("not a dependency")

def e(items):
    for i in items:
        try:
            go(i)
        except Exception:
            logger.error("item failed")
"""


def test_index_marks_retry_and_dependency_situations():
    got = {
        s.template: (s.in_retry, s.near_dependency) for s in index_source(SITUATIONS_SRC, "x.py")
    }
    assert got == {
        "get failed, retrying": (True, True),  # attempt loop + requests.get
        "gave up": (False, False),  # after the loop
        "again": (True, False),  # loop with try/except + sleep
        "trying": (True, False),  # @retry
        "database timeout": (False, True),  # cursor.execute
        "not a dependency": (False, False),  # dict.get
        "item failed": (False, False),  # loop over items: not a retry
    }


@pytest.mark.parametrize(
    "name, expected",
    [
        ("requests.post", True),
        ("httpx.AsyncClient().get", True),
        ("cur.execute", True),
        ("self.payment_client.charge", True),
        ("order.gateway.refund", True),
        ("s3_bucket.upload_file", True),
        ("config.get", False),
        ("items.append", False),
        ("json.loads", False),
    ],
)
def test_dependency_calls(name, expected):
    assert is_dependency_call(name) is expected


INJECTION_SRC = """
import logging, structlog
from opentelemetry.instrumentation.logging import LoggingInstrumentor

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = current_request_id.get()
        setattr(record, "tenant_id", tenant.get())
        return True

def add_user(logger, method_name, event_dict):
    event_dict["user_id"] = user.get()
    return event_dict

def middleware(req):
    structlog.contextvars.bind_contextvars(region=req.region)
    log = logging.LoggerAdapter(logging.getLogger(__name__), {"service": "checkout"})

LoggingInstrumentor().instrument(set_logging_format=True)

def unrelated(order, sock):
    order.status = "x"
    sock.bind(("0.0.0.0", 80))
"""


def test_detect_injected_context():
    found = detect_in_source(INJECTION_SRC, "app/obs.py")
    assert set(found) == {
        "request_id",
        "tenant_id",
        "user_id",
        "region",
        "service",
        "otelTraceID",
        "otelSpanID",
        "otelServiceName",
        "otelTraceSampled",
    }
    assert found["request_id"] == ["app/obs.py:7 sets it on every log record"]
    assert found["otelTraceID"] == ["app/obs.py:19 OpenTelemetry logging instrumentation"]
    assert detect_in_source("def (", "bad.py") == {}


def test_synonyms():
    assert satisfied("trace_id", {"otelTraceID"})
    assert satisfied("request_id", {"correlation_id"})
    assert not satisfied("tenant_id", {"trace_id"})


CALLS = """
def f(order, cur, ctx):
    logger.info("ok", extra={"request_id": 1})
    logger.info("no request id")
    logger.error("boom", extra={"request_id": 1})
    try:
        cur.execute("q")
    except Exception:
        logger.error("db down", extra={"requestId": 1, "tenant_id": 2})
    logger.error("dynamic", extra=ctx)
"""


def ll006(rules, src=CALLS):
    cfg = LintConfig(context=rules)
    return {d.line: d.message for d in lint_source(src, "a.py", cfg) if d.code == "LL006"}


def test_ll006_requires_fields_by_situation():
    rules = ContextRules(all=["request_id"], error=["tenant_id"], near_dependency=["dependency"])
    got = ll006(rules)
    assert set(got) == {4, 5, 9}  # line 3 is complete; extra=ctx (10) can't be checked
    assert got[4].startswith("missing context: request_id;")
    assert got[5].startswith("missing context: tenant_id (errors);")
    # requestId satisfies request_id; tenant_id given; only the dependency is missing
    assert got[9].startswith("missing context: dependency (dependency calls);")


def test_ll006_credits_injected_context():
    rules = ContextRules(all=["request_id"], error=["tenant_id"], injected={"tenant_id"})
    rules.detected = {"request_id": ["app/obs.py:7 sets it on every log record"]}
    assert ll006(rules) == {}


def test_required_fields_still_work_and_is_off_by_default():
    assert ll006(ContextRules()) == {}
    cfg = LintConfig(required_fields=["trace_id"])
    lines = {d.line for d in lint_source(CALLS, "a.py", cfg) if d.code == "LL006"}
    assert lines == {3, 4, 5, 9}


def test_lint_paths_detects_injection_across_the_project(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "obs.py").write_text(
        "class F(logging.Filter):\n    def filter(self, record):\n        record.request_id = 1\n"
    )
    (tmp_path / "app" / "main.py").write_text('logger.info("hello")\n')
    cfg = LintConfig(context=ContextRules(all=["request_id"]))
    assert not [d for d in lint_paths([tmp_path], cfg, root=tmp_path) if d.code == "LL006"]
    cfg = LintConfig(context=ContextRules(all=["request_id"], detect=False))
    assert [d.path for d in lint_paths([tmp_path], cfg, root=tmp_path) if d.code == "LL006"] == [
        "app/main.py"
    ]


def test_context_config_and_cli(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text(
        "class F(logging.Filter):\n"
        "    def filter(self, record):\n"
        "        record.request_id = 1\n\n"
        'logger.error("boom")\n'
        'logger.info("ok")\n'
    )
    (tmp_path / "logsite.toml").write_text(
        '[context]\nall = ["request_id"]\nerror = ["tenant_id"]\n'
    )
    monkeypatch.chdir(tmp_path)
    assert main(["context"]) == 1
    out = capsys.readouterr().out
    assert "request_id        app.py:3 sets it on every log record" in out
    assert "1 of 2 log call(s) are missing context:" in out
    assert "1  tenant_id (errors)" in out

    (tmp_path / "logsite.toml").write_text('[context]\nerrors = ["tenant_id"]\n')
    with pytest.raises(SystemExit, match=r"unknown \[context\] key\(s\) \['errors'\]"):
        main(["context"])


def _rec(**fields):
    return LogRecord(timestamp=None, message="m", fields=fields)


def test_has_field_handles_synonyms_nesting_and_placeholders():
    assert has_field(_rec(request_id="r-1"), "request_id")
    assert has_field(_rec(correlationId="c-1"), "request_id")
    assert has_field(_rec(ctx={"tenant_id": "t"}), "tenant_id")
    assert has_field(_rec(**{"trace.id": "abc"}), "trace_id")
    assert has_field(_rec(attempt=0), "attempt")  # 0 is a value
    for empty in ("", "-", "null", "None", None):
        assert not has_field(_rec(request_id=empty), "request_id")
    assert not has_field(_rec(other="x"), "request_id")


COVERAGE_SRC = """
def create(order):
    logger.info("order created %s", order.id)

def sync(order):
    logger.error("sync failed %s", order.id)
"""


def coverage_case():
    sites = index_source(COVERAGE_SRC, "app.py")
    samples = {
        "app.py:3": [_rec(request_id="r")] * 60 + [_rec()] * 25,
        "app.py:6": [_rec(request_id="-", ctx={"tenant_id": "t"})] * 10 + [_rec()] * 20,
    }
    cfg = LintConfig(context=ContextRules(all=["request_id"], error=["tenant_id"]))
    return sites, samples, cfg


def test_measure_and_overall_coverage():
    sites, samples, cfg = coverage_case()
    per_site = measure_coverage(sites, samples, cfg.required_context)
    got = {sid: [(c.field, c.present, c.total) for c in cov] for sid, cov in per_site.items()}
    assert got == {
        "app.py:3": [("request_id", 60, 85)],
        "app.py:6": [("request_id", 0, 30), ("tenant_id", 10, 30)],
    }
    assert [(c.field, c.present, c.total) for c in overall_coverage(per_site)] == [
        ("tenant_id", 10, 30),
        ("request_id", 60, 115),
    ]


def test_ll105_flags_lost_context_in_production():
    sites, samples, cfg = coverage_case()
    cfg.context.detected = {"request_id": ["app/obs.py:6 sets it on every log record"]}
    diags = [d for d in lint_volume(sites, {}, cfg, samples=samples) if d.code == "LL105"]
    assert [d.line for d in diags] == [3, 6]
    assert "request_id 71%" in diags[0].message
    assert "request_id 0%, tenant_id 33% (errors)" in diags[1].message
    assert "meant to be added automatically, but that setup doesn't reach" in diags[0].message
    # thresholds: few records are ignored, and a lower bar passes the 71% line
    cfg.context.min_records = 50
    assert [d.line for d in lint_volume(sites, {}, cfg, samples=samples) if d.code == "LL105"] == [
        3
    ]
    cfg.context.min_coverage = 0.7
    assert not [d for d in lint_volume(sites, {}, cfg, samples=samples) if d.code == "LL105"]
    # off without rules
    assert not [
        d for d in lint_volume(sites, {}, LintConfig(), samples=samples) if d.code == "LL105"
    ]


def test_cli_context_production(tmp_path, monkeypatch, capsys):
    rows = [{"message": "order created 1", "lineno": 3, "request_id": "r"}] * 30
    rows += [{"message": "order created 2", "lineno": 3}] * 10
    (tmp_path / "app.py").write_text(COVERAGE_SRC)
    (tmp_path / "prod.log").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "prod.log"\n[context]\nall = ["request_id"]\n'
    )
    monkeypatch.chdir(tmp_path)
    assert main(["context", "--production"]) == 1
    out = capsys.readouterr().out
    assert "request_id          75% of 40 record(s)" in out
    assert "75%  request_id      app.py:3  (30/40)" in out
    assert main(["lint", "--volume"]) == 1
    assert "app.py:3:5: LL105" in capsys.readouterr().out


def test_mcp_context_coverage(tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import call

    rows = [{"message": "order created 1", "lineno": 3}] * 25
    (tmp_path / "app.py").write_text(COVERAGE_SRC)
    (tmp_path / "prod.log").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "prod.log"\n[context]\nall = ["request_id"]\n'
    )
    err, out = call(tmp_path, "context_coverage")
    assert not err, out
    assert out["fields"] == [
        {"field": "request_id", "coverage": 0.0, "records": 25, "added_automatically": False}
    ]
    assert [g["id"] for g in out["gaps"]] == ["app.py:3"]
