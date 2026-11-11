"""最小 MCP 服务器 stub（测试用）：stdio + 换行分隔 JSON-RPC 2.0。

支持：initialize（声明 tools/resources/prompts 能力）、
tools/list、tools/call（echo、add、boom）、
resources/list、resources/read（krel://greeting）、
prompts/list、prompts/get（review 模板）。
"""

import json
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


TOOLS = [
    {
        "name": "echo",
        "description": "原样返回 text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "两数相加",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "boom",
        "description": "总是失败",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "stub-server", "version": "0.1.0"},
            }})
        elif method == "notifications/initialized":
            pass  # 通知无需响应
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(args.get("text", ""))}]}})
            elif name == "add":
                total = float(args.get("a", 0)) + float(args.get("b", 0))
                text = str(int(total)) if total == int(total) else str(total)
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}]}})
            elif name == "boom":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "boom 工具内部错误"}],
                    "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": f"unknown tool: {name}"}})
        elif method == "resources/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"resources": [
                {"uri": "krel://greeting", "name": "greeting",
                 "mimeType": "text/plain", "description": "一段问候语"},
                {"uri": "krel://numbers", "name": "numbers", "mimeType": "text/plain"},
            ]}})
        elif method == "resources/read":
            uri = (msg.get("params") or {}).get("uri")
            if uri == "krel://greeting":
                send({"jsonrpc": "2.0", "id": mid, "result": {"contents": [
                    {"uri": uri, "mimeType": "text/plain", "text": "你好，MCP 资源！"}]}})
            elif uri == "krel://numbers":
                send({"jsonrpc": "2.0", "id": mid, "result": {"contents": [
                    {"uri": uri, "mimeType": "text/plain", "text": "1 2 3"}]}})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32002, "message": f"resource not found: {uri}"}})
        elif method == "prompts/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"prompts": [
                {"name": "review", "description": "代码评审提示",
                 "arguments": [{"name": "path", "required": True}]},
            ]}})
        elif method == "prompts/get":
            params = msg.get("params") or {}
            pname = params.get("name")
            pargs = params.get("arguments") or {}
            if pname == "review":
                path = pargs.get("path", "?")
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "description": "代码评审提示",
                    "messages": [{"role": "user", "content": {
                        "type": "text", "text": f"请评审文件 {path}，重点看错误处理。"}}]}})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": f"unknown prompt: {pname}"}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
