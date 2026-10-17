# Releasing

A release publishes the Python package to **PyPI**, the editor extension to the **VS Code
Marketplace** and **Open VSX**, and both files to a **GitHub release**. Pushing a `v*` tag
runs [`release.yml`](.github/workflows/release.yml), which does all of it.

## One-time setup

1. **PyPI** (no token needed, it uses trusted publishing):
   - On <https://pypi.org/manage/account/publishing/>, add a *pending publisher*: project
     `logsite`, owner `rajpaynaik`, repository `logsite`, workflow `release.yml`,
     environment `pypi`.
   - In the GitHub repo: **Settings → Environments → New environment** named `pypi`.
     Optionally require a reviewer, so every release waits for your approval.
2. **VS Code Marketplace**:
   - Create the publisher `rajpaynaik` at <https://marketplace.visualstudio.com/manage>
     (it must match `publisher` in `editors/vscode/package.json`).
   - Create an Azure DevOps personal access token with organization **All accessible
     organizations** and scope **Marketplace → Manage**
     (<https://code.visualstudio.com/api/working-with-extensions/publishing-extension#get-a-personal-access-token>).
   - Save it as the repository secret `VSCE_PAT` (**Settings → Secrets and variables →
     Actions**).
3. **Open VSX** (optional, for VSCodium, Cursor, Windsurf, Gitpod):
   - Sign in at <https://open-vsx.org>, sign the publisher agreement, create the namespace
     `rajpaynaik` (`npx ovsx create-namespace rajpaynaik -p <token>`), and an access token.
   - Save it as the repository secret `OVSX_PAT`.

A store without credentials is skipped with a warning; the rest of the release still runs.

## Each release

1. Set the same version in `src/logsite/__init__.py` (`__version__`) and
   `editors/vscode/package.json` (`version`), and add an entry to
   `editors/vscode/CHANGELOG.md`.
2. Commit, then tag and push:

   ```bash
   git tag v0.1.0 && git push origin v0.1.0
   ```

The workflow checks that the tag matches both versions, builds and checks the packages,
publishes to PyPI first (the extension tells users to `pip install logsite`), then the
extension stores, then creates the GitHub release with the wheel, sdist and `.vsix`
attached. A tag with a `-` (e.g. `v0.2.0-rc1`) is marked as a pre-release on GitHub.

## Checking a package locally

```bash
python -m build && twine check dist/*
cd editors/vscode && npm ci && npx vsce package   # logsite-<version>.vsix
code --install-extension logsite-<version>.vsix   # try it in your own VS Code
```
