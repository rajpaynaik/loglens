import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from loglens.backends import TimeRange
from loglens.cli import main
from loglens.git import enclosing_block, line_history, parse_log, site_history

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

V1 = """\
import logging

logger = logging.getLogger(__name__)


def charge(order):
    total = order.total
    logger.info("charging order %s", order.id)
    return total


def refund(order):
    logger.info("refunding order %s", order.id)
"""

# Changes the body of charge() but not the log line itself.
V2 = V1.replace("total = order.total", "total = order.total * 2")


def git(repo, *args, date="2026-01-01T00:00:00+00:00"):
    env = {
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_COMMITTER_NAME": "Ada",
        "GIT_COMMITTER_EMAIL": "ada@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "HOME": str(repo),
        "PATH": os.environ["PATH"],
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pay.py").write_text(V1)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "add payments", date="2026-01-01T00:00:00+00:00")
    (tmp_path / "app" / "pay.py").write_text(V2)
    git(tmp_path, "commit", "-qam", "double the total", date="2026-02-01T00:00:00+00:00")
    return tmp_path


def test_enclosing_block_picks_innermost_function():
    assert enclosing_block(V1, 8) == (6, 9)
    assert enclosing_block(V1, 13) == (12, 13)
    assert enclosing_block(V1, 3) == (3, 3)  # module level: just the line
    assert enclosing_block("def (", 1) == (1, 1)


def test_parse_log_skips_garbage():
    out = "abc\x1fAda\x1f2026-02-01T00:00:00+00:00\x1fsubject\n\nnot a commit\n"
    (c,) = parse_log(out)
    assert c.short_sha == "abc" and c.author == "Ada" and c.summary == "subject"


@needs_git
def test_site_history_sees_changes_to_the_surrounding_function(repo):
    assert [c.summary for c in site_history(repo, "app/pay.py", 8)] == [
        "double the total",
        "add payments",
    ]
    # refund() never changed after it was added
    assert [c.summary for c in site_history(repo, "app/pay.py", 13)] == ["add payments"]
    # only the log line itself: the body change doesn't count
    assert [c.summary for c in line_history(repo, "app/pay.py", 8)] == ["add payments"]


@needs_git
def test_site_history_since_and_missing(repo):
    since = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert [c.summary for c in site_history(repo, "app/pay.py", 8, since=since)] == [
        "double the total"
    ]
    assert site_history(repo, "app/nope.py", 1) == []
    assert line_history(repo / "not-a-repo", "app/pay.py", 1) == []


@needs_git
def test_cli_history(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert main(["history", "app/pay.py:8", "--max", "1"]) == 0
    assert "double the total" in capsys.readouterr().out


def test_time_range_previous():
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    r = TimeRange(start=end - timedelta(hours=24), end=end)
    prev = r.previous()
    assert prev.end == r.start and prev.start == r.start - timedelta(hours=24)
