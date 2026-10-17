from logsite.index import index_source

SRC = """
import logging
logger = logging.getLogger(__name__)

def f(uid, items):
    logger.info("user %s logged in", uid)
    logging.warning(f"user {uid} is {items!r}")
    self.log.error("bad {} thing".format(uid))
    logger.log(logging.INFO, "via log() %d", 3)
    logging.getLogger("x").debug("chained")
    print("not a log")
    obj.info("not a logger")
    for i in items:
        logger.debug("item %s", i)
    try:
        pass
    except Exception:
        logger.exception("boom")
    logger.info(build_message())
"""


def test_finds_log_calls_and_templates():
    sites = index_source(SRC, "app.py")
    got = [(s.line, s.level, s.template) for s in sites]
    assert got == [
        (6, "info", "user %s logged in"),
        (7, "warning", "user {} is {}"),
        (8, "error", "bad {} thing"),
        (9, "log", "via log() %d"),
        (10, "debug", "chained"),
        (14, "debug", "item %s"),
        (18, "exception", "boom"),
        (19, "info", None),
    ]


def test_context_flags():
    sites = {s.line: s for s in index_source(SRC, "app.py")}
    assert sites[14].in_loop and not sites[6].in_loop
    assert sites[18].in_except and not sites[14].in_except


def test_syntax_error_returns_empty():
    assert index_source("def (:", "bad.py") == []
