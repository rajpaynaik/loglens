from datetime import datetime, timedelta, timezone

from conftest import V1

from loglens.backends import TimeRange
from loglens.cli import main
from loglens.git import enclosing_block, line_history, parse_log, site_history


def test_enclosing_block_picks_innermost_function():
    assert enclosing_block(V1, 8) == (6, 9)
    assert enclosing_block(V1, 13) == (12, 13)
    assert enclosing_block(V1, 3) == (3, 3)  # module level: just the line
    assert enclosing_block("def (", 1) == (1, 1)


def test_parse_log_skips_garbage():
    out = "abc\x1fAda\x1f2026-02-01T00:00:00+00:00\x1fsubject\n\nnot a commit\n"
    (c,) = parse_log(out)
    assert c.short_sha == "abc" and c.author == "Ada" and c.summary == "subject"


def test_site_history_sees_changes_to_the_surrounding_function(repo):
    assert [c.summary for c in site_history(repo, "app/pay.py", 8)] == [
        "double the total",
        "add payments",
    ]
    # refund() never changed after it was added
    assert [c.summary for c in site_history(repo, "app/pay.py", 13)] == ["add payments"]
    # only the log line itself: the body change doesn't count
    assert [c.summary for c in line_history(repo, "app/pay.py", 8)] == ["add payments"]


def test_site_history_since_and_missing(repo):
    since = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert [c.summary for c in site_history(repo, "app/pay.py", 8, since=since)] == [
        "double the total"
    ]
    assert site_history(repo, "app/nope.py", 1) == []
    assert line_history(repo / "not-a-repo", "app/pay.py", 1) == []


def test_cli_history(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert main(["history", "app/pay.py:8", "--max", "1"]) == 0
    assert "double the total" in capsys.readouterr().out


def test_time_range_previous():
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    r = TimeRange(start=end - timedelta(hours=24), end=end)
    prev = r.previous()
    assert prev.end == r.start and prev.start == r.start - timedelta(hours=24)


def test_parse_log_accepts_utc_z_dates():
    # git >= 2.5x prints UTC as "Z", which Python 3.10's fromisoformat rejects on its own
    out = "abc\x1fAda\x1f2026-10-17T00:35:51Z\x1fsubject\n"
    (c,) = parse_log(out)
    assert c.date == datetime(2026, 10, 17, 0, 35, 51, tzinfo=timezone.utc)
