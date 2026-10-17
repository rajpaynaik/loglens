# Contributing to loglens

Thanks for helping! Issues and PRs are welcome.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q && ruff check src tests && ruff format --check src tests
```

Try it against the demo: `cd examples/demo_app && loglens lint --volume && loglens counts`.

## Project layout

| Path | What |
|------|------|
| `src/loglens/index.py` | AST scan for log calls → `LogSite` |
| `src/loglens/matching.py` | production `LogRecord` → `LogSite` |
| `src/loglens/backends/` | one file per log store; implement `LogBackend.query_logs` |
| `src/loglens/lint.py` | lint rules (`LL0xx` static, `LL1xx` volume) |
| `src/loglens/git.py` | `git log -L` history for the function around a log call |
| `src/loglens/deploys.py` | before/after-release comparison (`spikes --since-deploy`) |
| `src/loglens/server.py` | language server (pygls) |
| `src/loglens/mcp_server.py` | MCP server for AI assistants (`loglens mcp`) |
| `editors/vscode/` | thin VS Code client |

## Good first contributions

- **A new backend** (Datadog, Splunk, Google Cloud Logging, Azure Monitor). Subclass `LogBackend`,
  implement `query_logs`, register it in `backends/__init__.py:from_config`, and add a test that
  parses a recorded API response (see `test_loki_parse_response_merges_labels`).
- **A new lint rule.** Add it to `RULES` and `check_call` in `lint.py`, with tests showing both a
  positive and a negative case.
- **structlog / loguru support** in `index.py`.

## Guidelines

- Keep the core dependency-free (stdlib only). Vendor SDKs go behind optional extras.
- Every rule needs a test that shows it firing *and* a case where it must not fire.
- Use conventional, descriptive commit messages.
