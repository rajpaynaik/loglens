# logsite

**See how often each log line fires in production, and catch bad logging, right next to the `logger.info(...)` call in your editor.**

```python
logger.info("charging order %s for %d cents", order.id, order.total)   # 📈 1.2M in 24h
logger.debug(f"card {card_number} charged")                            # ⚠ LL001 eager f-string · LL002 PII: card_number
logger.warning("legacy discount path used for order %s", order.id)     # no logs in 24h  ← dead code?
```

logsite indexes every log call in your code, maps production logs back to the exact line that emitted them, and shows the result inline through the Language Server Protocol, so it works in VS Code, Neovim, JetBrains (LSP plugin), Helix, Zed and others.

It doesn't depend on any one vendor: Loki, OpenSearch / Elasticsearch and CloudWatch Logs are supported. Each backend is one small Python class.

> Formerly **loglens**: existing `loglens.toml` / `[tool.loglens]` configs keep working.
>
> Status: **pre-alpha**. Python codebases only, for now. Feedback and contributors welcome.

## Features

- **Log index**: finds every log call using `ast`: the standard `logging` module, **structlog** and **loguru**, including `%`-style, f-string, `.format()` and loguru `{}` templates, and chained calls like `log.bind(user_id=u).info(...)`.
- **Production counts per line**: code lens above each log call (`1.2M in 24h`), with a `▲ 8.0x` marker when the line spiked compared with the window before.
- **MCP server**: AI assistants (Claude Code, Claude Desktop, Cursor, …) can ask "which log lines spiked after the last deploy, and what changed?" See [MCP server](#mcp-server).
- **Cost per line**: bytes and estimated monthly spend for every log call (`$420/mo` in the editor, `logsite cost` in the terminal), so you fix the few lines that make up most of the bill.
- **Request timeline**: `logsite trace <trace_id>` shows every log line for one request across services, in order, with this repo's lines linked to the code that wrote them and the first warning flagged as a likely cause.
- **Leak scanning**: finds secrets (API keys, tokens, passwords, private keys) and personal data (emails, card numbers, IBANs, SSNs) in what your services *actually* logged, and points at the log call that wrote each one. Values are always masked in the report.
- **Git correlation**: hover a log call to see its trend and the last commits that touched the function around it; `logsite spikes` lists the lines that jumped, with the commits that likely caused it.
- **Log-quality linter**: works offline and in CI:

| Code  | Rule |
|-------|------|
| LL001 | Eager formatting (f-string, `.format()`, `%`, `+`) in a log call |
| LL002 | Possible PII / secret logged (`password`, `token`, `email`, `card_number`, …) |
| LL003 | `logger.error` inside `except` without `exc_info`, so the traceback is lost |
| LL004 | `info`/`debug` log inside a loop |
| LL005 | Message built at runtime, so it can't be traced back to production |
| LL006 | Missing required context: fields for every log, errors, retry loops and dependency calls (`[context]`), opt-in; context added automatically is credited |
| LL007 | Failure logged below `warning` ("failed", "timeout", "could not" at `info`/`debug`), or an error swallowed by a broad `except` and logged at `info` |
| LL008 | Values formatted into the message instead of `extra=` fields; editor quick fix rewrites the call. **Opt-in** (`extend_select = ["LL008"]`) |
| LL100 | Noisy line: emits more than N times per window *(needs a backend)* |
| LL101 | Warning/error line never seen in production *(needs a backend)* |
| LL102 | Line spiked: ≥ `spike_ratio`× the previous window and ≥ `spike_min_count` emits *(needs a backend)* |
| LL104 | Level doesn't match production: `info` lines whose real messages look like failures, or `error` lines firing constantly *(needs a backend)* |
| LL105 | Required context missing from production records: e.g. `request_id` in only 71% of a line's real records *(needs a backend)* |

## Install

```bash
pip install "logsite[lsp]"        # editor support; add ,mcp for the MCP server
                                  # until the first release is on PyPI: pip install -e ".[dev]"
```

## Quick start

```bash
cd examples/demo_app
logsite index              # list every log call
logsite lint               # static log-quality checks (exit 1 on findings, good for CI)
logsite context            # required context fields, and what's already added automatically
logsite context --production   # how often real records actually carry them, per log call
logsite structure          # share of log calls with a constant message + fields (--production: by volume)
logsite counts             # production counts per line, from the configured backend
logsite cost --price-per-gb 0.5   # bytes and $/month per log call, most expensive first
logsite lint --volume      # static + volume rules
logsite spikes             # lines that jumped vs the previous window, with suspect commits
logsite spikes --since-deploy v1.4.0   # what changed after a release, with that release's commits
logsite history app/checkout.py:7   # commits that touched the code around a log line
logsite leaks              # secrets / personal data in production logs, per log call (exit 1 if any)
logsite trace 4bf92f35...  # every log line for one request, across services, in order
logsite serve              # language server on stdio (used by editors)
logsite mcp                # MCP server on stdio (used by AI assistants)
```

## Configuration

`logsite.toml` at your project root, or `[tool.logsite]` in `pyproject.toml`:

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
extend_select = ["LL008"]           # opt-in rules on top of the defaults
required_fields = ["trace_id"]      # LL006 for every log; see [context] for situational rules
noisy_threshold = 100000
spike_ratio = 5                     # LL102 / `logsite spikes`: growth vs the previous window
spike_min_count = 1000              # ... and at least this many emits
error_noisy_threshold = 10000       # LL104: error lines firing this often per window
# failure_pattern = '\b(failed|timeout|declined)\b'   # LL007/LL104: what counts as a failure

[context]                           # LL006: what each log call must carry
all = ["request_id"]
error = ["tenant_id", "user_id"]    # who was affected
in_retry = ["attempt"]              # retry loops, @retry / @backoff functions
near_dependency = ["dependency"]    # except blocks around HTTP / DB / client calls
injected = ["trace_id"]             # added outside the code, e.g. by your log shipper
# min_coverage = 0.95               # LL105: share of production records that must carry a field
# min_records = 20                  # LL105: skip lines with fewer sampled records

[cost]
price_per_gb = 0.50                 # what your log store charges per GB ingested
price_per_million_events = 0        # per-event indexing charges, if your vendor has them
# currency = "€"

[timeline]                          # logsite trace
field = "trace_id"                  # or request_id; common spellings (traceId, trace.id) match
service = "checkout"                # this repo's name in the timeline (default: its folder)
[timeline.backend]                  # optional: a wider scope than [backend], e.g. every service
type = "loki"
url = "http://localhost:3100"
selector = '{env="prod"}'

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
[backend]                           # AWS CloudWatch Logs: pip install "logsite[cloudwatch]"
type = "cloudwatch"
log_group = "/ecs/checkout"
region = "us-east-1"
# profile = "prod"                  # credentials come from the usual AWS chain
# filter_pattern = "INFO"           # CloudWatch filter syntax
# log_stream_prefix = "web-"
```

## MCP server

`logsite mcp` exposes logsite to AI assistants over the [Model Context Protocol](https://modelcontextprotocol.io).
Install it with `pip install "logsite[mcp]"`, then register it. For Claude Code:

```bash
claude mcp add logsite -- logsite --config-dir /path/to/your/project mcp
```

Other clients take the same command (`logsite --config-dir <project> mcp`) in their MCP config.
All tools are read-only:

| Tool | What it answers |
|------|-----------------|
| `request_timeline` | What happened to request `4bf92f35…`, service by service, and where did it first go wrong? |
| `compare_deploy` | What spiked or started erroring after release `v1.4.0`, and which commits in it are to blame? |
| `spikes` | Which log lines jumped vs the previous window, and which commits touched that code since? |
| `log_history` | Which commits changed the function around `app/checkout.py:42`? |
| `log_costs` | Which log calls cost the most per month? |
| `context_coverage` | Which required context fields are missing from real records, and on which log calls? |
| `structure_score` | How much of our logging is structured, and which loud lines aren't? |
| `find_leaks` | Which log calls are writing secrets or personal data to production logs? |
| `log_counts` | Which log calls are the loudest in production? |
| `lint` | Which log calls have quality problems (optionally including volume rules)? |
| `list_log_calls` | Where are the log calls, and what are their message templates? |
| `list_rules` | What do the `LL…` codes mean? |

## structlog and loguru

logsite works out which library each logger comes from (`log = structlog.get_logger()`,
`from loguru import logger`, `std = logging.getLogger(__name__)`, whatever the variable is
called) and applies the right conventions:

| | `logging` | structlog | loguru |
|---|---|---|---|
| Fields (LL006, LL008 fix) | `extra={...}` | keyword arguments + `.bind(...)` | keyword arguments + `.bind(...)` |
| Traceback (LL003) | `exc_info=True` / `.exception()` | `exc_info=True` / `.exception()` | `.opt(exception=True)` / `.exception()` |
| Context added automatically | logging filters | `bind_contextvars`, processors | `contextualize`, `configure(extra=)` |
| Matching production logs | `pathname` + `lineno` | `CallsiteParameterAdder` (`pathname`, `lineno`) | `serialize=True` JSON (`record.file`, `record.line`) |

For exact matching, add the call site to structlog's processors:

```python
structlog.processors.CallsiteParameterAdder(
    {CallsiteParameter.PATHNAME, CallsiteParameter.LINENO}
)
```

loguru includes it automatically with `logger.add(sink, serialize=True)`.

## How log → line matching works

Matching production logs to source lines is the hard part. logsite tries two methods, most reliable first:

1. **Structured fields.** If your logs carry `pathname`/`filename` + `lineno` (or the ECS `log.origin.file.*` / OpenTelemetry `code.*` equivalents), matching is exact. Python's `logging` provides these for free; for example with [python-json-logger](https://github.com/nhairs/python-json-logger):
   ```python
   JsonFormatter("%(asctime)s %(levelname)s %(message)s %(pathname)s %(lineno)d")
   ```
2. **Template matching.** Each message template (`"user %s logged in"`) becomes a regex. When several match, the one with the most literal text wins, so `"%s"` never steals a match from a specific template.

Adding `pathname` + `lineno` to your log format is the biggest accuracy upgrade you can make.

### Counting at scale

By default logsite downloads up to 5,000 log lines per window and matches them itself, so
counts on busy services are capped and quieter lines can be missed entirely. With
`aggregate = true` (Loki and OpenSearch/Elasticsearch), the log store does the counting
per file and line, for example `sum by (pathname, lineno) (count_over_time(...))` in Loki,
so counts are exact at any volume. Lines without file/line fields are still downloaded and
matched by template, so only those are subject to the cap.

## How spikes map to commits

Each refresh fetches counts for the current window and the one just before it. A line whose
count grew by `spike_ratio` (and passed `spike_min_count`) is a spike. logsite then runs
`git log -L` over the **whole function** that contains the log call, because a line usually
spikes when its callers or surrounding logic change, not the log call itself. Line numbers
refer to `HEAD`, so uncommitted edits above the function can shift the range.

### After a release

`logsite spikes --since-deploy v1.4.0` compares each line from the deploy until now with an
equally long window just before it. Besides spikes, it lists **new errors**: error lines that
never fired before the deploy, even a handful of times. For each one it shows only the
commits **in that release** (from the previous tag, or `--base`, up to `v1.4.0`) that touched
the surrounding function, so unreleased work on `main` doesn't show up.

The deploy time defaults to the tag date (annotated tags) or the commit date. If you deploy
later than you tag, pass the real time: `--deployed-at 2026-10-16T14:05:00Z`.

## Context rules

`database timeout`, with no query, duration, retry count or request ID, can't be acted on.
LL006 requires context fields by situation, which logsite works out from the code:

| Situation | When | Typical fields |
|-----------|------|----------------|
| `all` | every log call | `request_id`, `trace_id` |
| `error` | `error` / `critical` / `exception` | `tenant_id`, `user_id` |
| `in_retry` | a loop with `try`/`except` that sleeps or counts attempts, or a `@retry` / `@backoff` function | `attempt` |
| `near_dependency` | an `except` around a call to `requests`, `httpx`, `boto3`, `cursor.execute` or a `*client` / `*session` / `*gateway` | `dependency`, `status_code` |

The right way to add common context is once, for every log, not on each call. logsite
credits fields that the project adds automatically: a logging filter or record factory
that sets `record.request_id`, structlog `bind_contextvars(...)` or processors,
`LoggerAdapter(logger, {...})` extras, and OpenTelemetry's logging instrumentation (which
satisfies `trace_id` / `span_id`). Common spellings count as the same field
(`requestId`, `correlation_id`). `logsite context` shows the rules, what was found and where,
and how many log calls are missing what.

Code can look right and still lose context: a filter that only some handlers have, a
thread or async task that doesn't copy the context, a background job with no request.
`logsite context --production` checks what real records carry:

```
In production (last 24h, 115 sampled record(s)):
  tenant_id           33% of 30 record(s)
  request_id          52% of 115 record(s) (added automatically)

Log calls below 95% (with >= 20 records):
     0%  request_id      app/orders.py:14  (0/30)
    33%  tenant_id       app/orders.py:14  (10/30)
    71%  request_id      app/orders.py:7  (60/85)
```

Fields count wherever they appear in a record, nested (`ctx.tenant_id`) or under a common
synonym. Placeholder values (`""`, `"-"`, `null`) count as missing. LL105 reports the same
gaps in `lint --volume`.

## Structured messages

`logger.error("payment failed for order %s: %s", order.id, err)` produces a different
message for every order. You can't count or alert on it without a regex, and `order_id`
isn't a field you can filter on. The structured version keeps the message constant:

```python
logger.error("payment failed for order", extra={"order_id": order.id, "err": err})
```

LL008 flags values formatted into a message (`%` args, f-strings, `.format()`). It's
opt-in, because `%`-style messages are idiomatic Python logging. Turn it on with
`extend_select = ["LL008"]` once your team has chosen structured logging. In the editor, the
quick fix does the rewrite: it names fields after the expressions (`order.id` → `order_id`),
merges into an existing `extra={...}`, and avoids `LogRecord` attribute names (`msg`,
`name`, ...) that would raise at runtime. It only offers the fix when the rewrite is safe.

`logsite structure` scores the codebase by top-level directory, and with `--production`
by real volume, listing the loudest unstructured lines (the ones most worth converting).

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

In most services a handful of log calls write most of the bytes. `logsite cost` ranks every
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

## Following a request

A failed checkout is hard to read service by service: the gateway returns a 504, checkout
logs a timeout, inventory logs a warning. In order, the cause is obvious:

```
$ logsite trace 4bf92f3577b34da6a3ce929d0e0e4703
trace_id=4bf92f3577b34da6a3ce929d0e0e4703: 7 line(s) from 3 service(s) over 2.015s

+     0.0ms   gateway    info    POST /checkout
+     3.0ms   checkout   info    checkout_started  app/checkout.py:7
+     5.0ms   inventory  INFO    reserve request
+  2005.0ms ~ inventory  WARN    db pool exhausted, waited 2000ms
+  2010.0ms ! checkout   error   inventory_timeout  app/checkout.py:11
+  2011.0ms   checkout   info    charging card ************1111  app/checkout.py:13
+  2015.0ms ! gateway    error   POST /checkout 504

first problem: inventory (WARN): db pool exhausted, waited 2000ms
first error: checkout at app/checkout.py:11 (error): inventory_timeout
```

- Lines this repo wrote link to their log call; other services show the service name
  (from `service`, `service.name`, `app`, `container`, CloudWatch log stream, ...).
- The id matches the field and its common spellings (`traceId`, `trace.id`), or the
  message as a whole word when a record has no such field.
- `[backend]` is usually scoped to one service. Point `[timeline.backend]` at a wider scope
  (`{env="prod"}`, `logs-*`) to see the whole path.
- Secrets and personal data in messages are masked, so it's safe to hand to an AI assistant
  (`request_timeline` over MCP).

This needs a shared id in the logs. Context rules (`[context] all = ["trace_id"]`) and
`logsite context --production` check that it's there.

## Leak scanning

LL002 guesses from variable names (`password`, `card_number`) in the code. `logsite leaks`
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
1 time in 10. logsite only flags one when it is grouped like a card (`4111 1111 …`) or
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

Every backend implements one interface ([`backends/base.py`](src/logsite/backends/base.py)):

```python
class LogBackend(ABC):
    def query_logs(self, time_range, contains=None, limit=5000) -> list[LogRecord]: ...
    def count_by_line(self, sites, time_range) -> dict[str, int]: ...  # default: fetch + match
```

## Roadmap

- [x] **v0.1**: log index, Loki + file backends, code-lens counts, quality linter, CLI
- [ ] VS Code extension on the Marketplace and Open VSX ([`editors/vscode`](editors/vscode)): packaging and release workflow ready ([RELEASING.md](RELEASING.md)); waiting on the first `v0.1.0` tag
- [x] Git correlation: when a line spikes, show the commits that touched it
- [x] MCP server: let AI assistants ask "which log lines spiked after the last deploy?"
- [x] OpenSearch and CloudWatch Logs backends
- [ ] Kafka module: consumer lag, DLQ messages and errors per topic
- [x] Server-side aggregation for Loki and OpenSearch (`aggregate = true`) for large volumes
- [x] structlog and loguru: indexing, library-aware lint rules and quick fixes, loguru JSON parsing
- [ ] Other languages: Go `slog`, Java SLF4J, Node pino
- [x] Cost per log line: bytes and estimated spend per line (not just counts), shown in the code lens and as `logsite cost`, with a configurable price per GB
- [ ] Audit event coverage: declare sensitive code paths (e.g. functions that change permissions, money or personal data) and the audit events they must emit; lint flags paths that don't, and checks that audit events never carry PII
- [x] Deploy-aware comparison: `logsite spikes --since-deploy <sha|tag>` compares error and volume per line before vs after a real release instead of a rolling window
- [x] Leak scanning in production logs: sample real log contents for secrets and personal data (tokens, card numbers, emails) and point to the code line that emitted each leak
- [ ] More MCP tools for incident investigation: recent messages for a line, errors that started after a deploy, so AI assistants can do root-cause analysis on logsite data
- [x] Severity correctness: flag log levels that don't match the event, statically (`info`/`debug` inside `except`, or messages like "failed"/"timeout" logged below `warning`) and from production (INFO lines whose messages look like failures, ERROR lines firing constantly at high volume), so failures aren't hidden from alerting
- [x] Context rules in code: required fields by situation (`[context]`: errors, retry loops, dependency calls), with credit for context injected automatically (logging filters, structlog contextvars, LoggerAdapter, OpenTelemetry)
- [x] Context coverage in production: per-line field coverage measured from real records (e.g. `tenant_id` present in 62% of records), to catch context lost in threads, async tasks and background jobs
- [x] Structured messages: flag values interpolated into the message (`"order %s failed"`) and offer an editor quick fix that moves them into `extra=` fields, plus a per-service structure score
- [x] Request timeline MCP tool: given a `trace_id`, every log line from every service for that request, in order, each linked to the code line that wrote it

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). A new backend is a good first PR.

## License

MIT
