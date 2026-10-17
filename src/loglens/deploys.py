"""Deploy-aware comparison: what changed per log line after a release, and which commits in
that release touched the code around it.

The deploy is a git ref (usually a release tag). Its time comes from the tag (or commit)
unless given explicitly, and "the release" is every commit between the previous tag (or an
explicit base) and the ref.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .backends import LogBackend, TimeRange
from .git import Commit, previous_tag, ref_time, resolve_ref, rev_list, site_history
from .lint import LintConfig, describe_trend, find_spikes
from .models import LogSite

ERROR_LEVELS = {"error", "critical", "exception"}
# How far back git log -L looks for candidate commits before filtering to the release.
_CANDIDATES = 200


@dataclass(frozen=True)
class Deploy:
    ref: str
    sha: str
    at: datetime
    base: str | None  # previous release; None if the ref has no earlier tag

    @property
    def short_sha(self) -> str:
        return self.sha[:8]


@dataclass
class Change:
    site: LogSite
    kind: str  # "spike" | "new_error"
    before: int
    after: int
    commits: list[Commit] = field(default_factory=list)

    @property
    def trend(self) -> str:
        return describe_trend(self.after, self.before)


@dataclass
class DeployReport:
    deploy: Deploy
    before: TimeRange
    after: TimeRange
    release_size: int  # commits in the release
    changes: list[Change]


def resolve_deploy(
    root: str | Path, ref: str, at: datetime | None = None, base: str | None = None
) -> Deploy:
    """Look up a deploy in git. Raises ValueError with a user-facing message."""
    sha = resolve_ref(root, ref)
    if sha is None:
        raise ValueError(f"unknown git ref {ref!r} in {root}")
    when = at or ref_time(root, ref)
    if when is None:
        raise ValueError(f"couldn't find a date for {ref!r}; pass the deploy time explicitly")
    if when.tzinfo is None:
        when = when.astimezone()  # naive times are local
    base = base or previous_tag(root, ref)
    if base is not None and resolve_ref(root, base) is None:
        raise ValueError(f"unknown git ref {base!r} in {root}")
    return Deploy(ref=ref, sha=sha, at=when.astimezone(timezone.utc), base=base)


def compare_deploy(
    root: str | Path,
    sites: Iterable[LogSite],
    backend: LogBackend,
    deploy: Deploy,
    cfg: LintConfig,
    commits: int = 3,
    now: datetime | None = None,
) -> DeployReport:
    """Compare per-line counts from the deploy until now with an equally long window before.

    Reports spikes (``cfg.spike_ratio`` / ``cfg.spike_min_count``) and error lines that never
    fired before the deploy, each with the release's commits that touched the code around it.
    """
    now = now or datetime.now(timezone.utc)
    if deploy.at >= now:
        raise ValueError(
            f"{deploy.ref} was deployed at {deploy.at:%Y-%m-%d %H:%M} UTC, in the future"
        )
    sites = list(sites)
    by_id = {s.id: s for s in sites}
    after = TimeRange(start=deploy.at, end=now)
    before = after.previous()
    now_counts = backend.count_by_line(sites, after)
    then_counts = backend.count_by_line(sites, before)

    changes = [
        Change(by_id[sid], "spike", prev, n)
        for sid, n, prev in find_spikes(now_counts, then_counts, cfg)
    ]
    spiked = {c.site.id for c in changes}
    new_errors = [
        Change(by_id[sid], "new_error", 0, n)
        for sid, n in now_counts.items()
        if sid not in spiked and by_id[sid].level in ERROR_LEVELS and not then_counts.get(sid)
    ]
    changes += sorted(new_errors, key=lambda c: c.after, reverse=True)

    # The release: commits between the previous release and this one. Without a base, fall
    # back to commits reachable from the ref that landed during the "before" window.
    if deploy.base:
        release = set(rev_list(root, f"{deploy.base}..{deploy.sha}"))
    else:
        release = set(rev_list(root, f"--since={before.start.isoformat()}", deploy.sha))
    if commits:
        revs = f"{deploy.base}..HEAD" if deploy.base else None
        since = None if deploy.base else before.start
        for change in changes:
            candidates = site_history(
                root,
                change.site.path,
                change.site.line,
                since=since,
                max_count=_CANDIDATES,
                revs=revs,
            )
            change.commits = [c for c in candidates if c.sha in release][:commits]
    return DeployReport(
        deploy=deploy, before=before, after=after, release_size=len(release), changes=changes
    )
