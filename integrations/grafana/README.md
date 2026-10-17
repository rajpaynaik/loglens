# Grafana dashboard for the catalog

`logsite-dashboard.json` is the generic **Log cost per code line** dashboard: it works with
any project, because everything project-specific is a dashboard variable (stream selector,
parser, file / line / level fields, path prefix to strip, link to the code, price per GB).
It reads Loki and needs records with file and line fields.

Regenerate it after changing `src/logsite/grafana.py` (a test fails until you do):

```bash
logsite grafana --generic -o integrations/grafana/logsite-dashboard.json
```

For your own project, `logsite grafana` makes a richer version: each row shows its log
message and links to the exact line at the current commit.

## Publishing it on grafana.com

1. Sign in at <https://grafana.com> (a free account).
2. Go to <https://grafana.com/grafana/dashboards/> → **Upload dashboard**.
3. Upload `logsite-dashboard.json`, add a screenshot (a table with real rows reads best),
   and link the project: <https://github.com/rajpaynaik/logsite>.

People then import it by its ID: **Dashboards → New → Import → paste the ID**.
