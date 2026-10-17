# Contributing to logsite

Thanks for helping! Issues and PRs are welcome.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q && ruff check src tests && ruff format --check src tests
```

Try it against the demo: `cd examples/demo_app && logsite lint --volume && logsite counts`.

## Project layout

| Path | What |
|------|------|
| `src/logsite/index.py` | AST scan for log calls → `LogSite` |
| `src/logsite/matching.py` | production `LogRecord` → `LogSite` |
| `src/logsite/backends/` | one file per log store; implement `LogBackend.query_logs` |
| `src/logsite/lint.py` | lint rules (`LL0xx` static, `LL1xx` volume) |
| `src/logsite/git.py` | `git log -L` history for the function around a log call |
| `src/logsite/context.py` | finds context added to every log automatically (filters, structlog, OTel) |
| `src/logsite/fixes.py` | editor quick fixes (LL008: values → `extra=`) |
| `src/logsite/structure.py` | structure score (`logsite structure`) |
| `src/logsite/timeline.py` | request timeline across services (`logsite trace`) |
| `src/logsite/cost.py` | per-line usage → monthly cost (`logsite cost`) |
| `src/logsite/leaks.py` | secret / personal-data detectors over production log contents |
| `src/logsite/deploys.py` | before/after-release comparison (`spikes --since-deploy`) |
| `src/logsite/server.py` | language server (pygls) |
| `src/logsite/mcp_server.py` | MCP server for AI assistants (`logsite mcp`) |
| `editors/vscode/` | thin VS Code client |

## Good first contributions

- **A new backend** (Datadog, Splunk, Google Cloud Logging, Azure Monitor). Subclass `LogBackend`,
  implement `query_logs`, register it in `backends/__init__.py:from_config`, and add a test that
  parses a recorded API response (see `test_loki_parse_response_merges_labels`).
- **A new lint rule.** Add it to `RULES` and `check_call` in `lint.py`, with tests showing both a
  positive and a negative case.
- **Another Python logging library** (e.g. `eliot`, `picologging`) in `index.py:library_names`.

## Guidelines

- Keep the core dependency-free (stdlib only). Vendor SDKs go behind optional extras.
- Every rule needs a test that shows it firing *and* a case where it must not fire.
- Use conventional, descriptive commit messages.
