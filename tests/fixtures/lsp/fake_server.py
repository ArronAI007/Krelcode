from __future__ import annotations

import json
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, value = line.decode().split(":", 1)
        headers[name.lower()] = value.strip()
    length = int(headers["content-length"])
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def write_message(value):
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        write_message({"jsonrpc": "2.0", "id": request_id, "result": {
            "capabilities": {"hoverProvider": True, "definitionProvider": True,
                             "referencesProvider": True},
            "serverInfo": {"name": "fake-lsp", "version": "1"},
        }})
    elif method == "initialized":
        continue
    elif method == "textDocument/didOpen":
        uri = params.get("textDocument", {}).get("uri", "")
        write_message({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                       "params": {"uri": uri, "diagnostics": [{
                           "range": {"start": {"line": 0, "character": 0},
                                     "end": {"line": 0, "character": 1}},
                           "severity": 2, "source": "fake", "message": "fake warning",
                       }]}})
    elif method == "textDocument/diagnostic":
        write_message({"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32601, "message": "not implemented"}})
    elif method == "textDocument/hover":
        position = params.get("position", {})
        write_message({"jsonrpc": "2.0", "id": request_id, "result": {
            "contents": {"kind": "markdown", "value": "**int**\n\nfake hover"},
            "range": {"start": position, "end": position},
        }})
    elif method == "textDocument/definition":
        uri = params.get("textDocument", {}).get("uri", "")
        write_message({"jsonrpc": "2.0", "id": request_id, "result": [{
            "uri": uri,
            "range": {"start": {"line": 1, "character": 2},
                      "end": {"line": 1, "character": 5}},
        }]})
    elif method == "textDocument/references":
        uri = params.get("textDocument", {}).get("uri", "")
        write_message({"jsonrpc": "2.0", "id": request_id, "result": [{
            "uri": uri,
            "range": {"start": {"line": 2, "character": 1},
                      "end": {"line": 2, "character": 2}},
        }, {
            "uri": "file:///outside/secret.py",
            "range": {"start": {"line": 0, "character": 0}},
        }]})
    elif method == "shutdown":
        write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
    elif method == "exit":
        break
    elif request_id is not None:
        write_message({"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32601, "message": "method not found"}})
