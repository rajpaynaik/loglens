from loglens.backends import parse_line
from loglens.index import index_source
from loglens.lint import LintConfig, lint_source, lint_volume

SRC = """
def f(user, password, items, ctx):
    logger.info(f"user {user}")
    logger.info("token is %s", user.api_key)
    logger.info("author %s", author)
    try:
        go()
    except Exception:
        logger.error("failed")
        logger.error("failed", exc_info=True)
    for i in items:
        logger.debug("item %s", i)
    logger.info(msg)
    logger.info("ok", extra={"trace_id": 1})
    logger.info("ok2", extra=ctx)
"""


def codes(cfg=None):
    return [(d.line, d.code) for d in lint_source(SRC, "a.py", cfg)]


def test_static_rules():
    got = codes()
    assert (3, "LL001") in got
    assert (4, "LL002") in got
    assert (5, "LL002") not in got  # "author" is not "auth"
    assert (9, "LL003") in got
    assert (10, "LL003") not in got
    assert (12, "LL004") in got
    assert (13, "LL005") in got
    assert not any(code == "LL006" for _, code in got)  # off by default


def test_required_fields_rule():
    cfg = LintConfig(required_fields=["trace_id"])
    got = codes(cfg)
    assert (3, "LL006") in got
    assert (14, "LL006") not in got
    assert (15, "LL006") not in got  # extra=ctx can't be checked statically


def test_ignore():
    assert not any(c == "LL001" for _, c in codes(LintConfig(ignore={"LL001"})))


def test_volume_rules():
    sites = index_source('logger.info("a")\nlogger.error("b")\n', "m.py")
    diags = lint_volume(sites, {"m.py:1": 500}, LintConfig(noisy_threshold=100))
    assert [(d.line, d.code) for d in diags] == [(1, "LL100"), (2, "LL101")]


def test_spike_rule():
    sites = index_source('logger.info("a")\nlogger.info("b")\nlogger.info("c")\n', "m.py")
    cfg = LintConfig(spike_ratio=5, spike_min_count=100)
    now = {"m.py:1": 1000, "m.py:2": 1000, "m.py:3": 50}
    before = {"m.py:1": 100, "m.py:2": 900, "m.py:3": 0}
    spikes = [d for d in lint_volume(sites, now, cfg, previous=before) if d.code == "LL102"]
    assert [d.line for d in spikes] == [1]  # 10x; line 2 is flat, line 3 is below min_count
    # new lines count as spikes once they pass min_count
    diags = lint_volume(sites, {"m.py:3": 500}, cfg, previous={})
    assert [d.line for d in diags if d.code == "LL102"] == [3]
    # without previous counts the rule can't run
    assert not any(d.code == "LL102" for d in lint_volume(sites, now, cfg))


SEVERITY_SRC = """
def f(order, status):
    logger.info("payment failed for order %s", order.id)
    logger.debug("could not reach %s", host)
    logger.warning("payment failed for order %s", order.id)
    logger.info("batch done with no errors")
    logger.info("error rate is %s", rate)
    try:
        go()
    except Exception:
        logger.info("skipped %s", order.id)
    try:
        go()
    except KeyError:
        logger.info("using default for %s", key)
    try:
        go()
    except Exception:
        logger.info("retrying %s", order.id)
        raise
    try:
        go()
    except:
        def later():
            raise ValueError
        logger.debug("ignored")
    try:
        go()
    except (ValueError, Exception):
        logger.info("ignored too")
"""


def test_severity_rule_ll007():
    diags = [d for d in lint_source(SEVERITY_SRC, "s.py") if d.code == "LL007"]
    got = {d.line: d.severity for d in diags}
    assert got == {
        3: "warning",  # "failed" at info
        4: "warning",  # "could not" at debug
        11: "info",  # swallowed by except Exception
        26: "info",  # bare except; the raise is in a nested function, not the handler
        30: "info",  # tuple containing Exception
    }
    # not flagged: warning level (5), "no errors" (6), "error rate" (7), narrow except (15),
    # handler that re-raises (19)
    assert '"failed" is logged at info' in next(d.message for d in diags if d.line == 3)


def test_severity_rule_ll007_custom_pattern_and_ignore():
    src = 'logger.info("payment declined for %s", x)\n'
    assert not [d for d in lint_source(src, "a.py") if d.code == "LL007"]
    cfg = LintConfig.from_dict({"failure_pattern": r"\bdeclined\b"})
    assert [d.code for d in lint_source(src, "a.py", cfg)] == ["LL007"]
    assert not lint_source(src, "a.py", LintConfig.from_dict({"ignore": ["LL007"]}))


def test_severity_rule_ll104():
    sites = index_source(
        'logger.error("boom")\n'
        'logger.info("payment %s", status)\n'
        'logger.info("payment failed %s", x)\n'
        'logger.info("status %s", s)\n',
        "p.py",
    )
    cfg = LintConfig(error_noisy_threshold=1000)
    counts = {"p.py:1": 5000, "p.py:2": 10, "p.py:3": 10, "p.py:4": 10}
    samples = {
        sid: [parse_line(m) for m in messages]
        for sid, messages in {
            "p.py:2": ["payment ok", "payment failed: card declined", "payment ok"],
            "p.py:3": ["payment failed 1"],  # template already says failed: that's LL007's job
            "p.py:4": ["status ok", "status 0 errors"],  # "0 errors" isn't a failure
        }.items()
    }
    diags = [d for d in lint_volume(sites, counts, cfg, samples=samples) if d.code == "LL104"]
    assert [d.line for d in diags] == [1, 2]
    noisy, dynamic = diags
    assert "logged as error 5,000 times" in noisy.message
    assert dynamic.message.startswith(
        '1 of 3 sampled production messages look like failures ("failed")'
    )
    assert "card declined" not in dynamic.message  # messages may hold personal data
    # without samples only the error-volume half runs
    assert [d.line for d in lint_volume(sites, counts, cfg) if d.code == "LL104"] == [1]


def test_cli_lint_volume_reports_severity_on_demo(monkeypatch, capsys):
    from loglens.cli import main

    monkeypatch.chdir("examples/demo_app")
    assert main(["lint", "--volume"]) == 1
    out = capsys.readouterr().out
    assert "app/checkout.py:23:9: LL007" in out and "app/checkout.py:24:5: LL007" in out
    assert "app/checkout.py:28:5: LL104" in out
