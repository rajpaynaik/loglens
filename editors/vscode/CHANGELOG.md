# Changelog

## 0.1.0 (preview)

First release.

- Production counts and cost per month above every log call, with a spike marker.
- Hover: trend vs the previous window, cost, and recent commits around the call.
- Production errors: a code lens and hover on the line that raised them, grouped by cause.
- Log-quality lint as you type (LL001–LL008), including context rules and severity checks.
- Quick fix: move values out of the message into `extra=` / keyword arguments (LL008).
- Python `logging`, structlog and loguru.
- Finds the server through the Python interpreter selected in VS Code, then `PATH`, and
  restarts when you pick another interpreter.
- Each file uses the nearest `logsite.toml`, so a monorepo or a parent folder works.
