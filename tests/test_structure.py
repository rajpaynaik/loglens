import ast
import json

import pytest

from logsite.cli import main
from logsite.fixes import constant_message, field_name, structured_rewrite
from logsite.index import index_calls
from logsite.lint import LintConfig, lint_source
from logsite.structure import production_score, score_project

SRC = """
logger.error("payment failed for order %s: %s", order.id, err)
logger.info(f"user {user.id} logged in from {ip}")
logger.info("item {} reserved for {}".format(item.sku, self.user_id))
log.warning("retry %d of %d", attempt, max_attempts, exc_info=True)
logger.info("starting")
logger.info("charged", extra={"order_id": order.id})
logger.info("rate 100%% done")
"""


def ll008(src=SRC, cfg=None):
    return [d.line for d in lint_source(src, "a.py", cfg) if d.code == "LL008"]


def test_ll008_is_opt_in():
    assert ll008() == []
    cfg = LintConfig.from_dict({"extend_select": ["LL008"]})
    assert ll008(cfg=cfg) == [2, 3, 4, 5]  # constant messages (6-8) are fine
    assert LintConfig.from_dict({"select": ["LL008"]}).enabled("LL008")
    with pytest.raises(ValueError, match=r"unknown lint rule\(s\) \['LL999'\]"):
        LintConfig.from_dict({"extend_select": ["LL999"]})


def rewrites(src):
    return {
        site.line: structured_rewrite(src, site, call) for site, call in index_calls(src, "a.py")
    }


def test_structured_rewrite():
    got = rewrites(SRC)
    assert got[2] == (
        'logger.error("payment failed for order", extra={"order_id": order.id, "err": err})'
    )
    assert got[3] == 'logger.info("user logged in", extra={"user_id": user.id, "ip": ip})'
    assert got[4] == (
        'logger.info("item reserved", extra={"item_sku": item.sku, "user_id": self.user_id})'
    )
    assert got[5] == (
        'log.warning("retry", exc_info=True, extra={"attempt": attempt, "max_attempts": '
        "max_attempts})"
    )
    assert got[6] is None and got[7] is None and got[8] is None  # nothing to move


def test_rewrite_merges_extra_avoids_reserved_names_and_keeps_level_arg():
    src = (
        'logger.info("sent %s to %s", msg, name, extra={"tenant_id": t})\n'
        'logger.log(logging.INFO, "count %d", len(items))\n'
        'logger.info("a %s b %s", x, x)\n'
    )
    got = rewrites(src)
    # msg / name are LogRecord attributes: extra={"msg": ...} would raise KeyError
    assert got[1] == (
        'logger.info("sent", extra={"tenant_id": t, "msg_value": msg, "name_value": name})'
    )
    assert got[2] == 'logger.log(logging.INFO, "count", extra={"items_count": len(items)})'
    assert got[3] == 'logger.info("a b", extra={"x": x, "x_2": x})'
    for line in got.values():
        ast.parse(line)  # valid Python


@pytest.mark.parametrize(
    "src",
    [
        'logger.info("%(user)s did it", {"user": u})',  # mapping
        'logger.info("x %s" % y)',  # eager %, left to a human
        'logger.info("a %s %s", x)',  # mismatch
        'logger.info("x %s", y, extra=ctx)',  # can't merge
        'logger.info("x %s", *args)',
        'logger.info("x {}".format(a, b=1))',
    ],
)
def test_rewrite_refuses_unsafe_cases(src):
    assert list(rewrites(src).values()) == [None]


@pytest.mark.parametrize(
    "template, expected",
    [
        ("payment failed for order %s: %s", "payment failed for order"),
        ("user {} logged in from {}", "user logged in"),
        ("retry %d of %d", "retry"),
        ("done: %s items (%s skipped)", "done items skipped"),
        ("rate 100%% of %s", "rate 100%"),
        ("%s", "event"),
    ],
)
def test_constant_message(template, expected):
    assert constant_message(template) == expected


def test_field_names():
    def name(expr):
        return field_name(ast.parse(expr, mode="eval").body)

    assert name("order.id") == "order_id"
    assert name("self.user_id") == "user_id"
    assert name("request.user.email") == "user_email"
    assert name('row["sku"]') == "sku"
    assert name("len(items)") == "items_count"
    assert name("a + b") is None


def test_structure_score(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "worker").mkdir()
    (tmp_path / "app" / "api.py").write_text(
        'logger.info("request", extra={"path": p})\nlogger.info("user %s", u)\n'
    )
    (tmp_path / "worker" / "jobs.py").write_text('logger.info("job done")\n')
    (tmp_path / "main.py").write_text('logger.info(f"boot {v}")\n')
    report = score_project(tmp_path)
    assert (report.overall.structured, report.overall.total) == (2, 4)
    assert {k: (s.structured, s.total) for k, s in report.by_area.items()} == {
        "app/": (1, 2),
        "worker/": (1, 1),
        "./": (0, 1),
    }
    p = production_score(report, {"app/api.py:1": 900, "app/api.py:2": 100})
    assert p.ratio == 0.9


def test_cli_structure(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text('logger.info("ok")\nlogger.info("user %s", u)\n')
    rows = [{"message": "user 1", "lineno": 2}] * 3 + [{"message": "ok", "lineno": 1}]
    (tmp_path / "prod.log").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "prod.log"\n')
    monkeypatch.chdir(tmp_path)
    assert main(["structure", "--production"]) == 0
    out = capsys.readouterr().out
    assert "Structure score: 50% of log calls keep a constant message (1/2)" in out
    assert "25% of 4 log line(s) come from constant-message calls" in out
    assert "3  app.py:2  'user %s'" in out


def test_mcp_structure_score(tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import call

    (tmp_path / "app.py").write_text('logger.info("ok")\nlogger.info("user %s", u)\n')
    err, out = call(tmp_path, "structure_score")
    assert not err, out
    assert (out["structured"], out["total"], out["score"]) == (1, 2, 0.5)
