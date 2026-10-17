// Thin client: all logic lives in the Python language server (`logsite serve`).
import { execFile } from "child_process";
import * as vscode from "vscode";
import { LanguageClient, LanguageClientOptions, ServerOptions } from "vscode-languageclient/node";

const INSTALL = 'pip install "logsite[lsp]"';

let client: LanguageClient | undefined;
let output: vscode.OutputChannel;

/** Resolves to true if `command args...` exits 0 within a few seconds. */
function runs(command: string, args: string[]): Promise<boolean> {
  return new Promise((resolve) => {
    execFile(command, args, { timeout: 10_000 }, (error) => resolve(!error));
  });
}

/** The interpreter selected in the Python extension, if it's installed. */
async function selectedPython(resource?: vscode.Uri): Promise<string | undefined> {
  const ext = vscode.extensions.getExtension("ms-python.python");
  if (!ext) {
    return undefined;
  }
  try {
    const api = ext.isActive ? ext.exports : await ext.activate();
    const envs = api?.environments;
    const active = envs?.getActiveEnvironmentPath(resource);
    const env = active && (await envs.resolveEnvironment(active));
    return env?.executable?.uri?.fsPath ?? active?.path;
  } catch (err) {
    output.appendLine(`Couldn't ask the Python extension for an interpreter: ${err}`);
    return undefined;
  }
}

/**
 * Where to run the server from, most specific first:
 * 1. the `logsite.serverCommand` setting,
 * 2. the Python interpreter selected in VS Code (`python -m logsite serve`), so a project's
 *    virtualenv works without extra setup,
 * 3. `logsite` on PATH.
 */
async function findServer(): Promise<string[] | undefined> {
  const configured = vscode.workspace.getConfiguration("logsite").get<string[]>("serverCommand");
  if (configured && configured.length > 0) {
    return configured;
  }
  const python = await selectedPython(vscode.workspace.workspaceFolders?.[0]?.uri);
  if (python && (await runs(python, ["-c", "import logsite.server"]))) {
    return [python, "-m", "logsite", "serve"];
  }
  if (await runs("logsite", ["--version"])) {
    return ["logsite", "serve"];
  }
  output.appendLine(
    `logsite not found${python ? ` in ${python}` : ""} or on PATH. Install it with: ${INSTALL}`,
  );
  return undefined;
}

async function notFound(): Promise<void> {
  const copy = "Copy install command";
  const settings = "Set server command";
  const choice = await vscode.window.showWarningMessage(
    `logsite: the language server isn't installed. Install it in your project's environment: ${INSTALL}`,
    copy,
    settings,
  );
  if (choice === copy) {
    await vscode.env.clipboard.writeText(INSTALL);
  } else if (choice === settings) {
    await vscode.commands.executeCommand("workbench.action.openSettings", "logsite.serverCommand");
  }
}

async function start(): Promise<void> {
  const server = await findServer();
  if (!server) {
    await notFound();
    return;
  }
  const [command, ...args] = server;
  output.appendLine(`Starting: ${server.join(" ")}`);
  const serverOptions: ServerOptions = { command, args };
  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: "file", language: "python" }],
    outputChannel: output,
  };
  client = new LanguageClient("logsite", "logsite", serverOptions, clientOptions);
  try {
    await client.start();
  } catch (err) {
    output.appendLine(`The server failed to start: ${err}`);
    client = undefined;
    const show = "Show output";
    if ((await vscode.window.showErrorMessage(`logsite failed to start: ${err}`, show)) === show) {
      output.show();
    }
  }
}

async function restart(): Promise<void> {
  await client?.stop();
  client = undefined;
  await start();
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  output = vscode.window.createOutputChannel("logsite");
  context.subscriptions.push(
    output,
    vscode.commands.registerCommand("logsite.restart", restart),
    vscode.commands.registerCommand("logsite.showOutput", () => output.show()),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("logsite.serverCommand")) {
        void restart();
      }
    }),
  );
  await start();
}

export async function deactivate(): Promise<void> {
  await client?.stop();
}
