import pytest

from loglens.cli import main
from loglens.context import detect_in_source, satisfied
from loglens.index import index_source, is_dependency_call
from loglens.lint import ContextRules, LintConfig, lint_paths, lint_source

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
    (tmp_path / "loglens.toml").write_text(
        '[context]\nall = ["request_id"]\nerror = ["tenant_id"]\n'
    )
    monkeypatch.chdir(tmp_path)
    assert main(["context"]) == 1
    out = capsys.readouterr().out
    assert "request_id        app.py:3 sets it on every log record" in out
    assert "1 of 2 log call(s) are missing context:" in out
    assert "1  tenant_id (errors)" in out

    (tmp_path / "loglens.toml").write_text('[context]\nerrors = ["tenant_id"]\n')
    with pytest.raises(SystemExit, match=r"unknown \[context\] key\(s\) \['errors'\]"):
        main(["context"])
