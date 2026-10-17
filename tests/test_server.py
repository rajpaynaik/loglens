"""The language server end to end, over stdio like an editor."""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("pygls")

CODE = """import logging
logger = logging.getLogger(__name__)

def charge(order):
    logger.info("charging order %s", order.id)
    logger.debug(f"order {order.id}")
"""


class Client:
    def __init__(self, root):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "logsite", "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=root,
        )
        self.next_id = 0

    def send(self, msg):
        body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()

    def read(self):
        headers = {}
        while (line := self.proc.stdout.readline().strip()) != b"":
            key, _, value = line.partition(b":")
            headers[key.lower()] = value.strip()
        return json.loads(self.proc.stdout.read(int(headers[b"content-length"])))

    def request(self, method, params):
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        while True:
            msg = self.read()
            if msg.get("id") == self.next_id and "method" not in msg:
                return msg["result"]
            if "method" in msg and "id" in msg:  # a request from the server: acknowledge it
                self.send({"id": msg["id"], "result": None})

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    def close(self):
        self.proc.kill()
        self.proc.wait()


def test_a_service_below_the_workspace_root_uses_its_own_config(tmp_path):
    """Opening a parent folder (or a monorepo) still finds each service's logsite.toml."""
    service = tmp_path / "services" / "pay"
    service.mkdir(parents=True)
    (service / "app.py").write_text(CODE)
    now = datetime.now(timezone.utc).isoformat()
    lines = [{"timestamp": now, "message": "charging order 7", "lineno": 5, "pathname": "app.py"}]
    (service / "prod.log").write_text("\n".join(json.dumps(x) for x in lines * 3) + "\n")
    (service / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "prod.log"\n')

    uri = (service / "app.py").as_uri()
    client = Client(tmp_path)
    try:
        client.request(
            "initialize",
            {"processId": None, "rootUri": tmp_path.as_uri(), "capabilities": {}},
        )
        client.notify("initialized", {})
        doc = {"uri": uri, "languageId": "python", "version": 1, "text": CODE}
        client.notify("textDocument/didOpen", {"textDocument": doc})

        lenses = []
        for _ in range(50):  # counts load in the background
            lenses = client.request("textDocument/codeLens", {"textDocument": {"uri": uri}})
            if lenses:
                break
            time.sleep(0.1)
        assert [lens["command"]["title"] for lens in lenses] == [
            "$(graph) 3 in 24h",
            "no logs in 24h",
        ]
        hover = client.request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": 4, "character": 10}},
        )
        assert "3 in the last 24h" in hover["contents"]["value"]
    finally:
        client.close()


TB = """Traceback (most recent call last):
  File "/srv/pay/app.py", line 6, in charge
    raise TimeoutError("inventory timed out after 3s")
TimeoutError: inventory timed out after 3s"""


def test_the_line_that_raised_shows_its_production_errors(tmp_path):
    code = CODE + '\n\ndef reserve():\n    raise TimeoutError("inventory timed out after 3s")\n'
    (tmp_path / "app.py").write_text(code)
    now = datetime.now(timezone.utc).isoformat()
    tb = TB.replace("line 6, in charge", "line 10, in reserve")
    line = {"timestamp": now, "level": "ERROR", "message": "charging order 7", "exc_info": tb}
    (tmp_path / "prod.log").write_text(
        "\n".join([json.dumps({**line, "lineno": 5, "pathname": "app.py"})] * 2) + "\n"
    )
    (tmp_path / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "prod.log"\n')

    uri = (tmp_path / "app.py").as_uri()
    client = Client(tmp_path)
    try:
        client.request(
            "initialize", {"processId": None, "rootUri": tmp_path.as_uri(), "capabilities": {}}
        )
        client.notify("initialized", {})
        doc = {"uri": uri, "languageId": "python", "version": 1, "text": code}
        client.notify("textDocument/didOpen", {"textDocument": doc})
        lenses = []
        for _ in range(50):
            lenses = client.request("textDocument/codeLens", {"textDocument": {"uri": uri}})
            if lenses:
                break
            time.sleep(0.1)
        raised = [lens for lens in lenses if lens["range"]["start"]["line"] == 9]
        assert raised[0]["command"]["title"] == "$(error) TimeoutError 2 (new) in 24h"
        assert raised[0]["range"]["start"]["character"] == 4  # under the indentation

        hover = client.request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": 9, "character": 8}},
        )["contents"]["value"]
        assert "errors raised here in production" in hover
        assert (
            "`TimeoutError: inventory timed out after 3s`: 2 in 24h (new), logged at `app.py:5`"
            in hover
        )
        hover = client.request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": 4, "character": 10}},
        )["contents"]["value"]
        assert "Errors logged here:" in hover and "raised at `app.py:10`" in hover
    finally:
        client.close()
