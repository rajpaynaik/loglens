# loglens for VS Code

Thin client for the [loglens](https://github.com/rajpaynaik/loglens) language server.

1. `pip install "loglens[lsp]"` (or set `loglens.serverCommand` to your venv's `loglens`)
2. Add a `loglens.toml` to your project (see the main README)
3. Open a Python file. Lint warnings appear right away; production counts appear as code lenses once the backend responds.

## Develop

```bash
npm install && npm run compile
# then press F5 in VS Code with this folder open to launch an Extension Development Host
```
