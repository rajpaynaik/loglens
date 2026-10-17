"""structlog and loguru support: indexing, lint rules, quick fixes and production parsing."""

import json
import subprocess
import sys

import pytest

from logsite.backends import FileBackend, TimeRange, parse_line
from logsite.context import detect_in_source
from logsite.fixes import structured_rewrite
from logsite.index import index_calls, index_project, index_source
from logsite.lint import LintConfig, lint_source

SRC = """
import logging
import structlog
from loguru import logger as lg

log = structlog.get_logger()
req_log = log.bind(request_id="r")
std = logging.getLogger(__name__)

async def a(order, uid, pw):
    log.info("order_created", order_id=order.id)
    log.bind(user_id=uid).info("bound_event")
    req_log.warning("slow_request")
    await log.ainfo("async_event")
    lg.info("User {} logged in", uid)
    lg.info("User {user} paid {amount:.2f}", user=uid, amount=3)
    lg.success("done")
    lg.opt(exception=True).error("failed hard")
    std.info("stdlib %s", uid)
    structlog.get_logger().error(event="kw_event")
    obj.info("not a logger")
"""


def test_index_recognises_structlog_loguru_and_named_stdlib_loggers():
    got = [
        (s.line, s.library, s.level, s.template, s.bound_fields, s.exc_opt)
        for s in index_source(SRC, "a.py")
    ]
    assert got == [
        (11, "structlog", "info", "order_created", (), False),
        (12, "structlog", "info", "bound_event", ("user_id",), False),
        (13, "structlog", "warning", "slow_request", ("request_id",), False),
        (14, "structlog", "info", "async_event", (), False),
        (15, "loguru", "info", "User {} logged in", (), False),
        (16, "loguru", "info", "User {} paid {}", (), False),
        (17, "loguru", "info", "done", (), False),
        (18, "loguru", "error", "failed hard", (), True),
        (19, "logging", "info", "stdlib %s", (), False),
        (20, "structlog", "error", "kw_event", (), False),
    ]


LINT_SRC = """
import structlog
from loguru import logger as lg
log = structlog.get_logger()
req = log.bind(request_id="r")

def a(order, uid, pw):
    log.info("order_created", order_id=order.id, request_id=1)
    req.info("bound_ok")
    log.info("signup", password=pw)
    lg.info("User {user} paid", user=uid)
    try:
        go()
    except Exception:
        lg.error("boom")
        lg.opt(exception=True).error("boom with tb")
        log.error("charge_failed", exc_info=True)
"""


def test_lint_rules_know_where_each_library_puts_fields():
    cfg = LintConfig.from_dict({"extend_select": ["LL008"]}, context={"all": ["request_id"]})
    diags = lint_source(LINT_SRC, "a.py", cfg)
    by_code: dict[str, list[int]] = {}
    for d in diags:
        by_code.setdefault(d.code, []).append(d.line)
    # keyword field (8) and bound field (9) satisfy request_id
    assert by_code["LL006"] == [10, 11, 15, 16, 17]
    assert by_code["LL002"] == [10]  # password=... as a keyword
    assert by_code["LL003"] == [15]  # opt(exception=True) and exc_info=True keep tracebacks
    assert by_code["LL008"] == [11]  # loguru "{user}" formats the value in
    msg = {d.code: d.message for d in diags if d.line == 15}
    assert "logger.opt(exception=True).error" in msg["LL003"]
    assert "as keyword arguments" in msg["LL006"]


def rewrites(src):
    return {s.line: structured_rewrite(src, s, c) for s, c in index_calls(src, "a.py")}


def test_quick_fix_uses_keyword_arguments():
    got = rewrites(
        "import structlog\nfrom loguru import logger\nlog = structlog.get_logger()\n"
        'log.info("user %s logged in", user.id, request_id=r)\n'
        'log.info(f"order {order.id} shipped")\n'
        'logger.info("User {} paid {amount:.2f}", uid, amount=total)\n'
        'logger.info("User {user} paid", user=uid, tenant_id=t)\n'
        'logger.info("{{literal}} {}", x)\n'
    )
    assert got[4] == 'log.info("user logged in", request_id=r, user_id=user.id)'
    assert got[5] == 'log.info("order shipped", order_id=order.id)'
    assert got[6] == 'logger.info("User paid", uid=uid, amount=total)'
    assert got[7] == 'logger.info("User paid", tenant_id=t, user=uid)'
    assert got[8] is None  # escaped braces: left alone


LOGURU_LINE = json.dumps(
    {
        "text": "2026-10-16 21:57:07.984 | INFO | app.lg:pay:11 - User 0 paid 0\n",
        "record": {
            "extra": {"request_id": "r-0", "amount": 0},
            "file": {"name": "lg.py", "path": "/srv/app/lg.py"},
            "function": "pay",
            "level": {"name": "INFO", "no": 20},
            "line": 11,
            "message": "User 0 paid 0",
            "name": "app.lg",
            "time": {"repr": "2026-10-16 21:57:07+00:00", "timestamp": 1760651827.98},
        },
    }
)


def test_parse_loguru_serialized_records():
    r = parse_line(LOGURU_LINE)
    assert r.message == "User 0 paid 0"
    assert (r.source_file, r.source_line) == ("/srv/app/lg.py", 11)
    assert r.fields["request_id"] == "r-0" and r.fields["levelname"] == "INFO"
    assert r.timestamp is not None and r.timestamp.year == 2025


def test_detect_loguru_context():
    found = detect_in_source(
        "from loguru import logger\n"
        "logger.configure(extra={'service': 'checkout'})\n"
        "def f(r):\n"
        "    with logger.contextualize(request_id=r):\n"
        "        logger.bind(attempt=1).info('x')\n",
        "a.py",
    )
    assert found == {
        "service": ["a.py:2 loguru configure(extra=)"],
        "request_id": ["a.py:4 loguru contextualize()"],
    }  # bind() is per call, not "every log"


APP_SL = """
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.PATHNAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=open("structlog.log", "w")),
)
log = structlog.get_logger()


def order(i):
    structlog.contextvars.bind_contextvars(request_id=f"r-{i}")
    log.info("order_created", order_id=i)
    if i % 4 == 0:
        log.bind(attempt=1).warning("payment_retry", order_id=i)
"""

APP_LG = """
from loguru import logger

logger.remove()
logger.add(open("loguru.log", "w"), serialize=True)


def pay(i):
    with logger.contextualize(request_id=f"r-{i}"):
        logger.info("User {} paid {amount}", i, amount=i * 100)
        if i % 5 == 0:
            logger.error("payment failed for order {order}", order=i)
"""


def test_real_structlog_and_loguru_output_maps_back_to_code(tmp_path):
    pytest.importorskip("structlog")
    pytest.importorskip("loguru")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "sl.py").write_text(APP_SL)
    (tmp_path / "app" / "lg.py").write_text(APP_LG)
    run = "from app import sl, lg\nfor i in range(40):\n    sl.order(i)\n    lg.pay(i)\n"
    subprocess.run([sys.executable, "-c", run], cwd=tmp_path, check=True)
    logs = (tmp_path / "structlog.log").read_text() + (tmp_path / "loguru.log").read_text()
    (tmp_path / "prod.log").write_text(logs)

    sites = index_project(tmp_path)
    counts = FileBackend(tmp_path / "prod.log").count_by_line(sites, TimeRange.last(24))
    sl = next(s.line for s in sites if s.template == "order_created")
    retry = next(s.line for s in sites if s.template == "payment_retry")
    paid = next(s.line for s in sites if s.template == "User {} paid {}")
    failed = next(s.line for s in sites if s.template == "payment failed for order {}")
    assert counts == {
        f"app/sl.py:{sl}": 40,
        f"app/sl.py:{retry}": 10,
        f"app/lg.py:{paid}": 40,
        f"app/lg.py:{failed}": 8,
    }
