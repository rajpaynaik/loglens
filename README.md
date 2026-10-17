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
- **Cost per line**: bytes and estimated monthly spend for every log call (`$420/mo` in the editor, `loglens cost` in the terminal), so you fix the few lines that make up most of the bill.
- **Leak scanning**: finds secrets (API keys, tokens, passwords, private keys) and personal data (emails, card numbers, IBANs, SSNs) in what your services *actually* logged, and points at the log call that wrote each one. Values are always masked in the report.
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
| LL007 | Failure logged below `warning` ("failed", "timeout", "could not" at `info`/`debug`), or an error swallowed by a broad `except` and logged at `info` |
| LL100 | Noisy line: emits more than N times per window *(needs a backend)* |
| LL101 | Warning/error line never seen in production *(needs a backend)* |
| LL102 | Line spiked: ≥ `spike_ratio`× the previous window and ≥ `spike_min_count` emits *(needs a backend)* |
| LL104 | Level doesn't match production: `info` lines whose real messages look like failures, or `error` lines firing constantly *(needs a backend)* |

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
loglens cost --price-per-gb 0.5   # bytes and $/month per log call, most expensive first
loglens lint --volume      # static + volume rules
loglens spikes             # lines that jumped vs the previous window, with suspect commits
loglens spikes --since-deploy v1.4.0   # what changed after a release, with that release's commits
loglens history app/checkout.py:7   # commits that touched the code around a log line
loglens leaks              # secrets / personal data in production logs, per log call (exit 1 if any)
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
aggregate = true                    # let Loki count per file/line (see below); needs pathname + lineno
# parser = "json"                   # or "logfmt": how Loki reads pathname/lineno from each line

[lint]
ignore = ["LL004"]
required_fields = ["trace_id"]      # enables LL006
noisy_threshold = 100000
spike_ratio = 5                     # LL102 / `loglens spikes`: growth vs the previous window
spike_min_count = 1000              # ... and at least this many emits
error_noisy_threshold = 10000       # LL104: error lines firing this often per window
# failure_pattern = '\b(failed|timeout|declined)\b'   # LL007/LL104: what counts as a failure

[cost]
price_per_gb = 0.50                 # what your log store charges per GB ingested
price_per_million_events = 0        # per-event indexing charges, if your vendor has them
# currency = "€"

[leaks]
ignore = ["email"]                  # detector kinds to skip
allow = ['@example\.com$']          # regexes for values that are fine, e.g. test data
ignore_fields = ["user_hash"]       # structured fields not to scan
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
aggregate = true                    # count per file/line with a composite aggregation
# file_field = "log.origin.file.name"  # defaults (ECS); must be keyword/numeric fields,
# line_field = "log.origin.file.line"  # e.g. "pathname.keyword" / "lineno"
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
| `compare_deploy` | What spiked or started erroring after release `v1.4.0`, and which commits in it are to blame? |
| `spikes` | Which log lines jumped vs the previous window, and which commits touched that code since? |
| `log_history` | Which commits changed the function around `app/checkout.py:42`? |
| `log_costs` | Which log calls cost the most per month? |
| `find_leaks` | Which log calls are writing secrets or personal data to production logs? |
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

### Counting at scale

By default loglens downloads up to 5,000 log lines per window and matches them itself, so
counts on busy services are capped and quieter lines can be missed entirely. With
`aggregate = true` (Loki and OpenSearch/Elasticsearch), the log store does the counting
per file and line, for example `sum by (pathname, lineno) (count_over_time(...))` in Loki,
so counts are exact at any volume. Lines without file/line fields are still downloaded and
matched by template, so only those are subject to the cap.

## How spikes map to commits

Each refresh fetches counts for the current window and the one just before it. A line whose
count grew by `spike_ratio` (and passed `spike_min_count`) is a spike. loglens then runs
`git log -L` over the **whole function** that contains the log call, because a line usually
spikes when its callers or surrounding logic change, not the log call itself. Line numbers
refer to `HEAD`, so uncommitted edits above the function can shift the range.

### After a release

`loglens spikes --since-deploy v1.4.0` compares each line from the deploy until now with an
equally long window just before it. Besides spikes, it lists **new errors**: error lines that
never fired before the deploy, even a handful of times. For each one it shows only the
commits **in that release** (from the previous tag, or `--base`, up to `v1.4.0`) that touched
the surrounding function, so unreleased work on `main` doesn't show up.

The deploy time defaults to the tag date (annotated tags) or the commit date. If you deploy
later than you tag, pass the real time: `--deployed-at 2026-10-16T14:05:00Z`.

## Severity checks

A payment failure logged at `info` never reaches an alert that watches `warning`/`error`.
LL007 catches it in the code: a failure word in the message at `info`/`debug`, or an error
swallowed by `except Exception:` (or a bare `except:`) and logged at `info`. Narrow handlers
(`except KeyError:`) and handlers that re-raise are fine.

Some lines only look like failures once rendered: `logger.info("settlement %s: %s", id,
status)` writes `settlement 44: failed (card declined)`. With `lint --volume`, LL104 checks a
sample of each line's real messages, and also flags `error` lines that fire constantly,
since alert fatigue is how real errors end up downgraded to `info`. LL104 reports only the
matched word, never the message, because messages can hold personal data.

## Cost per line

In most services a handful of log calls write most of the bytes. `loglens cost` ranks every
log call by projected monthly spend (usage over the configured window, scaled to 30 days):

```
 per month     volume   events      avg  line
   $1,840   3.7 TB    9.1B    410 B  app/api.py:88  'request received %s %s'
     $212   424.0 GB  1.3B    330 B  app/cache.py:41  'cache miss for %s'
```

| Backend | Bytes come from |
|---------|-----------------|
| Loki with `aggregate = true` | `bytes_over_time` per file/line: exact at any volume |
| OpenSearch / Elasticsearch with `aggregate = true` | exact counts × each line's average document size from a sample (marked `~`) |
| CloudWatch Logs | each event's size + the 26 bytes CloudWatch bills per event |
| Others, or without `aggregate` | the size of each record fetched (exact, but capped at `--limit`) |

Set `price_per_gb` to what you pay for ingestion (CloudWatch Logs is about $0.50/GB), and
`price_per_million_events` if your vendor also charges per indexed event. Prices are yours
to set because they vary by vendor, plan and region. Once a price is set, the editor shows
the cost next to each log call and in its hover.

## Leak scanning

LL002 guesses from variable names (`password`, `card_number`) in the code. `loglens leaks`
checks what really reached production: it scans the message and every structured field of
recent log records (up to `--limit`, default 5,000) and groups findings by the log call
that wrote them, secrets first.

```
app/checkout.py:13  1 record(s)  'card {} charged'
    pii    card_number     x1      e.g. ************1111
```

| Kind | Detection |
|------|-----------|
| Secrets | AWS / GitHub / Slack / Stripe / Google keys, JWTs, `Bearer` tokens, private key blocks, `password=` / `api_key:` style values |
| Personal data | Emails, card numbers (Luhn-checked, plus card-style grouping or a nearby word like "card"), IBANs (checksum-validated), US SSNs |

Card numbers are the classic false positive: a bare 16-digit order ID passes the checksum
1 time in 10. loglens only flags one when it is grouped like a card (`4111 1111 …`) or
appears near a card-related word or in a card-named field.

Matched values are never printed in full, so the report is safe to keep in CI logs.

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
- [x] Server-side aggregation for Loki and OpenSearch (`aggregate = true`) for large volumes
- [ ] Other languages: structlog/loguru, then Go `slog`, Java SLF4J, Node pino
- [x] Cost per log line: bytes and estimated spend per line (not just counts), shown in the code lens and as `loglens cost`, with a configurable price per GB
- [ ] Audit event coverage: declare sensitive code paths (e.g. functions that change permissions, money or personal data) and the audit events they must emit; lint flags paths that don't, and checks that audit events never carry PII
- [x] Deploy-aware comparison: `loglens spikes --since-deploy <sha|tag>` compares error and volume per line before vs after a real release instead of a rolling window
- [x] Leak scanning in production logs: sample real log contents for secrets and personal data (tokens, card numbers, emails) and point to the code line that emitted each leak
- [ ] More MCP tools for incident investigation: recent messages for a line, errors that started after a deploy, so AI assistants can do root-cause analysis on loglens data
- [x] Severity correctness: flag log levels that don't match the event, statically (`info`/`debug` inside `except`, or messages like "failed"/"timeout" logged below `warning`) and from production (INFO lines whose messages look like failures, ERROR lines firing constantly at high volume), so failures aren't hidden from alerting
- [ ] Context coverage: required fields by situation (`[context]` rules such as errors need `tenant_id`, logs in retry loops need `attempt`, logs next to HTTP/DB calls need `dependency`), credit for context injected automatically (logging filters, structlog `bind_contextvars`, OpenTelemetry), and per-line field coverage measured in production (e.g. `tenant_id` present in 62% of records)
- [ ] Structured messages: flag values interpolated into the message (`"order %s failed"`) and offer an editor quick fix that moves them into `extra=` fields, plus a per-service structure score
- [ ] Request timeline MCP tool: given a `trace_id`, every log line from every service for that request, in order, each linked to the code line that wrote it

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). A new backend is a good first PR.

## License

MIT
