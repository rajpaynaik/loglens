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
