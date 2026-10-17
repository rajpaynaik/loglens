// Thin client: all logic lives in the Python language server (`logsite serve`).
import * as vscode from "vscode";
import { LanguageClient, LanguageClientOptions, ServerOptions } from "vscode-languageclient/node";

let client: LanguageClient | undefined;

function createClient(): LanguageClient {
  const [command, ...args] = vscode.workspace
    .getConfiguration("logsite")
    .get<string[]>("serverCommand", ["logsite", "serve"]);

  const serverOptions: ServerOptions = { command, args };
  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: "file", language: "python" }],
  };
  return new LanguageClient("logsite", "logsite", serverOptions, clientOptions);
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  client = createClient();
  await client.start();

  context.subscriptions.push(
    vscode.commands.registerCommand("logsite.restart", async () => {
      await client?.stop();
      client = createClient();
      await client.start();
    }),
  );
}

export async function deactivate(): Promise<void> {
  await client?.stop();
}
