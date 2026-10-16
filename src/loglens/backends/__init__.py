from .base import LogBackend, TimeRange, parse_line
from .file import FileBackend
from .loki import LokiBackend

__all__ = ["FileBackend", "LogBackend", "LokiBackend", "TimeRange", "parse_line", "from_config"]


def from_config(cfg: dict) -> LogBackend | None:
    """Build a backend from the ``[backend]`` table of the loglens config."""
    kind = cfg.get("type")
    if not kind:
        return None
    if kind == "file":
        return FileBackend(cfg["path"])
    if kind == "loki":
        return LokiBackend(
            cfg["url"],
            cfg["selector"],
            username=cfg.get("username"),
            password=cfg.get("password"),
            tenant_id=cfg.get("tenant_id"),
        )
    raise ValueError(f"unknown backend type: {kind!r} (supported: file, loki)")
