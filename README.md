# loglens

**See how often each log line fires in production, and catch bad logging, right next to the `logger.info(...)` call in your editor.**

```python
logger.info("charging order %s for %d cents", order.id, order.total)   # 📈 1.2M in 24h
logger.debug(f"card {card_number} charged")                            # ⚠ LL001 eager f-string · LL002 PII: card_number
logger.warning("legacy discount path used for order %s", order.id)     # no logs in 24h  ← dead code?
```

loglens indexes every log call in your code, maps production logs back to the exact line that emitted them, and shows the result inline through the Language Server Protocol, so it works in VS Code, Neovim, JetBrains (LSP plugin), Helix, Zed and others.

It doesn't depend on any one vendor: Loki today, with OpenSearch and CloudWatch adapters on the roadmap. Each backend is one small Python class.

> Status: **pre-alpha**. Python codebases only, for now. Feedback and contributors welcome.

## Features

- **Log index**: finds every `logger.*` / `logging.*` call using `ast`, including `%`-style, f-string and `.format()` templates.
- **Production counts per line**: code lens above each log call (`1.2M in 24h`).
- **Log-quality linter**: works offline and in CI:

| Code  | Rule |
|-------|------|
| LL001 | Eager formatting (f-string, `.format()`, `%`, `+`) in a log call |
| LL002 | Possible PII / secret logged (`password`, `token`, `email`, `card_number`, …) |
| LL003 | `logger.error` inside `except` without `exc_info`, so the traceback is lost |
| LL004 | `info`/`debug` log inside a loop |
| LL005 | Message built at runtime, so it can't be traced back to production |
| LL006 | Missing required context field in `extra=` (e.g. `trace_id`), opt-in |
| LL100 | Noisy line: emits more than N times per window *(needs a backend)* |
| LL101 | Warning/error line never seen in production *(needs a backend)* |

## Install

```bash
pip install "loglens[lsp]"        # from source for now: pip install -e ".[dev]"
```

## Quick start

```bash
cd examples/demo_app
loglens index              # list every log call
loglens lint               # static log-quality checks (exit 1 on findings, good for CI)
loglens counts             # production counts per line, from the configured backend
loglens lint --volume      # static + volume rules
loglens serve              # language server on stdio (used by editors)
```

## Configuration

`loglens.toml` at your project root, or `[tool.loglens]` in `pyproject.toml`:

```toml
window_hours = 24

[backend]
type = "loki"                       # or "file"
url = "http://localhost:3100"
selector = '{app="checkout"}'
username = "me"
password_env = "LOKI_PASSWORD"      # any key ending in _env is read from the environment
# tenant_id = "team-a"

[lint]
ignore = ["LL004"]
required_fields = ["trace_id"]      # enables LL006
noisy_threshold = 100000
```

## How log → line matching works

Matching production logs to source lines is the hard part. loglens tries two methods, most reliable first:

1. **Structured fields.** If your logs carry `pathname`/`filename` + `lineno`, matching is exact. Python's `logging` provides these for free; for example with [python-json-logger](https://github.com/nhairs/python-json-logger):
   ```python
   JsonFormatter("%(asctime)s %(levelname)s %(message)s %(pathname)s %(lineno)d")
   ```
2. **Template matching.** Each message template (`"user %s logged in"`) becomes a regex. When several match, the one with the most literal text wins, so `"%s"` never steals a match from a specific template.

Adding `pathname` + `lineno` to your log format is the biggest accuracy upgrade you can make.

## Architecture

```
VS Code / JetBrains / Neovim (thin client)
        │  Language Server Protocol
Python core (pygls)  ── also exposed as an MCP server (planned)
  ├── index.py     → finds every log call in code (ast)
  ├── matching.py  → production log → source line
  ├── backends/    → file | Loki | OpenSearch* | CloudWatch*
  ├── lint.py      → noise, dead logs, missing IDs, PII
  ├── kafka*       → consumer lag, DLQ, errors per topic
  └── git*         → commits that touched a spiking line (git log -L)
                                                   * = roadmap
```

Every backend implements one interface ([`backends/base.py`](src/loglens/backends/base.py)):

```python
class LogBackend(ABC):
    def query_logs(self, time_range, contains=None, limit=5000) -> list[LogRecord]: ...
    def count_by_line(self, sites, time_range) -> dict[str, int]: ...  # default: fetch + match
```

## Roadmap

- [x] **v0.1**: log index, Loki + file backends, code-lens counts, quality linter, CLI
- [ ] VS Code extension on the Marketplace ([`editors/vscode`](editors/vscode))
- [ ] Git correlation: when a line spikes, show the commits that touched it
- [ ] MCP server: let AI assistants ask "which log lines spiked after the last deploy?"
- [ ] OpenSearch and CloudWatch Logs backends
- [ ] Kafka module: consumer lag, DLQ messages and errors per topic
- [ ] Server-side aggregation for Loki (`sum by (lineno)`) for large volumes
- [ ] Other languages: structlog/loguru, then Go `slog`, Java SLF4J, Node pino

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). A new backend is a good first PR.

## License

MIT
