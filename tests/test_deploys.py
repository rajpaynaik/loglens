import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from conftest import V2, git

from logsite.backends import FileBackend
from logsite.cli import main
from logsite.deploys import compare_deploy, resolve_deploy
from logsite.git import previous_tag, ref_time
from logsite.index import index_project
from logsite.lint import LintConfig

NOW = datetime.now(timezone.utc).replace(microsecond=0)
DEPLOYED = NOW - timedelta(hours=1)
VOID = '\n\ndef void(order):\n    logger.error("void failed for order %s", order.id)\n'


def iso(dt):
    return dt.isoformat()


@pytest.fixture
def release(repo):
    """v1.0 = the conftest repo; v1.1 changes charge() and adds void() (an error log on
    line 17); then an unreleased commit touches charge() again. Logs: charge() spikes after
    the deploy, refund() is flat, void() starts erroring."""
    git(repo, "tag", "v1.0")
    pay = repo / "app" / "pay.py"
    pay.write_text(V2.replace("* 2", "* 5") + VOID)
    git(repo, "commit", "-qam", "release: charge errors", date=iso(NOW - timedelta(hours=3)))
    git(repo, "tag", "v1.1")
    pay.write_text(V2.replace("* 2", "* 6") + VOID)
    git(repo, "commit", "-qam", "wip: unreleased", date=iso(NOW - timedelta(minutes=10)))

    def line(at, lineno, msg):
        return json.dumps(
            {"timestamp": iso(at), "message": msg, "pathname": "/srv/app/pay.py", "lineno": lineno}
        )

    before, after = DEPLOYED - timedelta(minutes=30), DEPLOYED + timedelta(minutes=30)
    lines = [line(before, 8, "charging order 1")] * 2 + [line(after, 8, "charging order 2")] * 50
    lines += [line(before, 13, "refunding order 1")] * 5 + [
        line(after, 13, "refunding order 2")
    ] * 5
    lines += [line(after, 17, "void failed for order 3")] * 3
    (repo / "prod.log").write_text("\n".join(lines) + "\n")
    (repo / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "prod.log"\n'
        "[lint]\nspike_ratio = 5\nspike_min_count = 10\n"
    )
    return repo


def report(repo, ref="v1.1", **kw):
    deploy = resolve_deploy(repo, ref, at=DEPLOYED, **kw)
    backend = FileBackend(repo / "prod.log")
    cfg = LintConfig(spike_ratio=5, spike_min_count=10)
    return deploy, compare_deploy(repo, index_project(repo), backend, deploy, cfg, now=NOW)


def test_compare_deploy_reports_spikes_and_new_errors_with_release_commits(release):
    deploy, r = report(release)
    assert deploy.base == "v1.0"  # the tag before v1.1
    assert r.before.end == r.after.start == DEPLOYED
    assert r.release_size == 1
    got = [(c.kind, c.site.id, c.before, c.after, c.trend) for c in r.changes]
    assert got == [
        ("spike", "app/pay.py:8", 2, 50, "25.0x"),
        ("new_error", "app/pay.py:17", 0, 3, "new"),
    ]
    # Only commits in v1.0..v1.1: not the unreleased one, not the one already in v1.0.
    for c in r.changes:
        assert [x.summary for x in c.commits] == ["release: charge errors"]


def test_explicit_base_widens_the_release(release):
    first = subprocess.run(
        ["git", "-C", str(release), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _, r = report(release, base=first)
    (spike, _) = r.changes
    assert [x.summary for x in spike.commits] == ["release: charge errors", "double the total"]


def test_resolve_deploy_errors_and_dates(release):
    with pytest.raises(ValueError, match="unknown git ref 'v9'"):
        resolve_deploy(release, "v9")
    with pytest.raises(ValueError, match="unknown git ref 'nope'"):
        resolve_deploy(release, "v1.1", base="nope")
    future = resolve_deploy(release, "v1.1", at=NOW + timedelta(hours=1))
    with pytest.raises(ValueError, match="in the future"):
        compare_deploy(release, [], FileBackend(release / "prod.log"), future, LintConfig())
    # lightweight tag: the commit date; annotated tag: the tag date
    assert ref_time(release, "v1.1") == NOW - timedelta(hours=3)
    git(release, "tag", "-a", "v1.2", "-m", "release", date=iso(NOW - timedelta(minutes=5)))
    assert ref_time(release, "v1.2") == NOW - timedelta(minutes=5)
    assert previous_tag(release, "v1.2") == "v1.1"
    assert resolve_deploy(release, "v1.2").at == NOW - timedelta(minutes=5)


def test_cli_since_deploy(release, monkeypatch, capsys):
    monkeypatch.chdir(release)
    assert main(["spikes", "--since-deploy", "v1.1", "--deployed-at", iso(DEPLOYED)]) == 0
    out = capsys.readouterr().out
    assert "release v1.0..v1.1, 1 commit(s)" in out
    assert "app/pay.py:8  2 -> 50 (25.0x)" in out
    assert "New errors" in out and "app/pay.py:17  0 -> 3 (new)" in out
    assert "wip: unreleased" not in out
    with pytest.raises(SystemExit, match="can't be combined"):
        main(["spikes", "--since-deploy", "v1.1", "--hours", "2"])
    with pytest.raises(SystemExit, match="need --since-deploy"):
        main(["spikes", "--base", "v1.0"])


def test_mcp_compare_deploy(release):
    pytest.importorskip("mcp")
    from test_mcp import call

    err, out = call(release, "compare_deploy", {"ref": "v1.1", "deployed_at": iso(DEPLOYED)})
    assert not err, out
    assert out["base"] == "v1.0" and out["release_commits"] == 1
    assert [(c["kind"], c["id"]) for c in out["changes"]] == [
        ("spike", "app/pay.py:8"),
        ("new_error", "app/pay.py:17"),
    ]
    err, msg = call(release, "compare_deploy", {"ref": "v9"})
    assert err and "unknown git ref" in msg
