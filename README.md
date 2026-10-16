# loglens

**See how often each log line fires in production, and catch bad logging, right next to the `logger.info(...)` call in your editor.**

```python
logger.info("charging order %s for %d cents", order.id, order.total)   # 📈 1.2M in 24h
logger.debug(f"card {card_number} charged")                            # ⚠ LL001 eager f-string · LL002 PII: card_number
logger.warning("legacy discount path used for order %s", order.id)     # no logs in 24h  ← dead code?
```

loglens indexes every log call in your code, maps production logs back to the exact line that emitted them, and shows the result inline through the Language Server Protocol, so it works in VS Code, Neovim, JetBrains (LSP plugin), Helix, Zed and others.

It doesn't depend on any one vendor: Loki, OpenSearch / Elasticsearch and CloudWatch Logs are supported. Each backend is one small Python class.

> Status: **pre-alpha**. Python codebases only, for now. Feedback and contributors welcome.

## Features

- **Log index**: finds every `logger.*` / `logging.*` call using `ast`, including `%`-style, f-string and `.format()` templates.
- **Production counts per line**: code lens above each log call (`1.2M in 24h`), with a `▲ 8.0x` marker when the line spiked compared with the window before.
- **MCP server**: AI assistants (Claude Code, Claude Desktop, Cursor, …) can ask "which log lines spiked after the last deploy, and what changed?" See [MCP server](#mcp-server).
- **Git correlation**: hover a log call to see its trend and the last commits that touched the function around it; `loglens spikes` lists the lines that jumped, with the commits that likely caused it.
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
| LL102 | Line spiked: ≥ `spike_ratio`× the previous window and ≥ `spike_min_count` emits *(needs a backend)* |

## Install

```bash
pip install "loglens[lsp]"        # editor support; add ,mcp for the MCP server
                                  # from source for now: pip install -e ".[dev]"
```

## Quick start

```bash
cd examples/demo_app
loglens index              # list every log call
loglens lint               # static log-quality checks (exit 1 on findings, good for CI)
loglens counts             # production counts per line, from the configured backend
loglens lint --volume      # static + volume rules
loglens spikes             # lines that jumped vs the previous window, with suspect commits
loglens history app/checkout.py:7   # commits that touched the code around a log line
loglens serve              # language server on stdio (used by editors)
loglens mcp                # MCP server on stdio (used by AI assistants)
```

## Configuration

`loglens.toml` at your project root, or `[tool.loglens]` in `pyproject.toml`:

```toml
window_hours = 24

[backend]
type = "loki"                       # or "opensearch", "elasticsearch", "cloudwatch", "file"
url = "http://localhost:3100"
selector = '{app="checkout"}'
username = "me"
password_env = "LOKI_PASSWORD"      # any key ending in _env is read from the environment
# tenant_id = "team-a"

[lint]
ignore = ["LL004"]
required_fields = ["trace_id"]      # enables LL006
noisy_threshold = 100000
spike_ratio = 5                     # LL102 / `loglens spikes`: growth vs the previous window
spike_min_count = 1000              # ... and at least this many emits
```

Other backends:

```toml
[backend]                           # OpenSearch or Elasticsearch (same _search API)
type = "opensearch"
url = "https://search.internal:9200"
index = "logs-checkout-*"
query = 'service.name:checkout'     # optional Lucene query_string
# timestamp_field = "@timestamp"    # defaults
# message_field = "message"
api_key_env = "OPENSEARCH_API_KEY"  # or username + password_env
```

```toml
[backend]                           # AWS CloudWatch Logs: pip install "loglens[cloudwatch]"
type = "cloudwatch"
log_group = "/ecs/checkout"
region = "us-east-1"
# profile = "prod"                  # credentials come from the usual AWS chain
# filter_pattern = "INFO"           # CloudWatch filter syntax
# log_stream_prefix = "web-"
```

## MCP server

`loglens mcp` exposes loglens to AI assistants over the [Model Context Protocol](https://modelcontextprotocol.io).
Install it with `pip install "loglens[mcp]"`, then register it. For Claude Code:

```bash
claude mcp add loglens -- loglens --config-dir /path/to/your/project mcp
```

Other clients take the same command (`loglens --config-dir <project> mcp`) in their MCP config.
All tools are read-only:

| Tool | What it answers |
|------|-----------------|
| `spikes` | Which log lines jumped vs the previous window, and which commits touched that code since? |
| `log_history` | Which commits changed the function around `app/checkout.py:42`? |
| `log_counts` | Which log calls are the loudest in production? |
| `lint` | Which log calls have quality problems (optionally including volume rules)? |
| `list_log_calls` | Where are the log calls, and what are their message templates? |
| `list_rules` | What do the `LL…` codes mean? |

## How log → line matching works

Matching production logs to source lines is the hard part. loglens tries two methods, most reliable first:

1. **Structured fields.** If your logs carry `pathname`/`filename` + `lineno` (or the ECS `log.origin.file.*` / OpenTelemetry `code.*` equivalents), matching is exact. Python's `logging` provides these for free; for example with [python-json-logger](https://github.com/nhairs/python-json-logger):
   ```python
   JsonFormatter("%(asctime)s %(levelname)s %(message)s %(pathname)s %(lineno)d")
   ```
2. **Template matching.** Each message template (`"user %s logged in"`) becomes a regex. When several match, the one with the most literal text wins, so `"%s"` never steals a match from a specific template.

Adding `pathname` + `lineno` to your log format is the biggest accuracy upgrade you can make.

## How spikes map to commits

Each refresh fetches counts for the current window and the one just before it. A line whose
count grew by `spike_ratio` (and passed `spike_min_count`) is a spike. loglens then runs
`git log -L` over the **whole function** that contains the log call, because a line usually
spikes when its callers or surrounding logic change, not the log call itself. Line numbers
refer to `HEAD`, so uncommitted edits above the function can shift the range.

## Architecture

```
VS Code / JetBrains / Neovim (thin client)
        │  Language Server Protocol
Python core (pygls)  ── also exposed as an MCP server (mcp_server.py)
  ├── index.py     → finds every log call in code (ast)
  ├── matching.py  → production log → source line
  ├── backends/    → file | Loki | OpenSearch/Elasticsearch | CloudWatch
  ├── lint.py      → noise, dead logs, missing IDs, PII
  ├── kafka*       → consumer lag, DLQ, errors per topic
  └── git.py       → commits that touched a spiking line (git log -L)
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
- [x] Git correlation: when a line spikes, show the commits that touched it
- [x] MCP server: let AI assistants ask "which log lines spiked after the last deploy?"
- [x] OpenSearch and CloudWatch Logs backends
- [ ] Kafka module: consumer lag, DLQ messages and errors per topic
- [ ] Server-side aggregation for Loki (`sum by (lineno)`) for large volumes
- [ ] Other languages: structlog/loguru, then Go `slog`, Java SLF4J, Node pino

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). A new backend is a good first PR.

## License

MIT
