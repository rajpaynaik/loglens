# logsite for VS Code

Thin client for the [logsite](https://github.com/rajpaynaik/logsite) language server.

1. `pip install "logsite[lsp]"` (or set `logsite.serverCommand` to your venv's `logsite`)
2. Add a `logsite.toml` to your project (see the main README)
3. Open a Python file. Lint warnings appear right away; production counts appear as code lenses once the backend responds.

## Develop

```bash
npm install && npm run compile
# then press F5 in VS Code with this folder open to launch an Extension Development Host
```
