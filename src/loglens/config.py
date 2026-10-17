"""Load configuration from ``loglens.toml`` or ``[tool.loglens]`` in ``pyproject.toml``.

Example ``loglens.toml``::

    [backend]
    type = "loki"
    url = "http://localhost:3100"
    selector = '{app="checkout"}'
    password_env = "LOKI_PASSWORD"   # read secrets from the environment

    [lint]
    ignore = ["LL004"]
    required_fields = ["trace_id"]
    noisy_threshold = 50000

    [context]                        # LL006: required fields by situation
    all = ["request_id"]
    error = ["tenant_id"]
    in_retry = ["attempt"]
    near_dependency = ["dependency"]
    injected = ["trace_id"]          # added outside the code (e.g. by a log shipper)

    [leaks]                          # loglens leaks
    ignore = ["email"]               # detector kinds to skip
    allow = ['@example\\.com$']       # regexes for values that are fine (test data)

    [cost]                           # loglens cost, $/mo in the editor
    price_per_gb = 0.50              # what your log store charges per GB ingested
    price_per_million_events = 0     # per-event indexing charges, if any

    window_hours = 24
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from .cost import CostConfig
from .leaks import LeakConfig
from .lint import LintConfig


@dataclass
class Config:
    root: Path
    backend: dict = field(default_factory=dict)
    lint: LintConfig = field(default_factory=LintConfig)
    leaks: LeakConfig = field(default_factory=LeakConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    window_hours: float = 24.0
    excludes: list[str] = field(default_factory=list)


def find_root(start: Path) -> Path:
    start = start.resolve()
    for d in [start, *start.parents]:
        if (
            (d / "loglens.toml").is_file()
            or (d / "pyproject.toml").is_file()
            or (d / ".git").exists()
        ):
            return d
    return start


def load_config(start: str | Path | None = None) -> Config:
    root = find_root(Path(start or Path.cwd()))
    data: dict = {}
    if (root / "loglens.toml").is_file():
        data = tomllib.loads((root / "loglens.toml").read_text(encoding="utf-8"))
    elif (root / "pyproject.toml").is_file():
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        data = pyproject.get("tool", {}).get("loglens", {})

    backend = dict(data.get("backend", {}))
    for key in list(backend):
        if key.endswith("_env"):
            backend.setdefault(key[: -len("_env")], os.environ.get(backend[key]))
    if backend.get("type") == "file" and "path" in backend:
        backend["path"] = str((root / backend["path"]).resolve())

    return Config(
        root=root,
        backend=backend,
        lint=LintConfig.from_dict(data.get("lint", {}), context=data.get("context", {})),
        leaks=LeakConfig.from_dict(data.get("leaks", {})),
        cost=CostConfig.from_dict(data.get("cost", {})),
        window_hours=float(data.get("window_hours", 24)),
        excludes=list(data.get("exclude", [])),
    )
