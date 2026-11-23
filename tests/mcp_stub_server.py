"""最小 MCP 服务器 stub（测试用）：stdio + 换行分隔 JSON-RPC 2.0。

支持：initialize（声明 tools/resources/prompts 能力）、
tools/list、tools/call（echo、add、boom）、
resources/list、resources/read（krel://greeting）、
prompts/list、prompts/get（review 模板）。
"""

import json
import os
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
    {
        "name": "sample_me",
        "description": "向客户端发起 sampling/createMessage 反向请求",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_roots",
        "description": "向客户端发起 roots/list 反向请求",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "probe_ping",
        "description": "向客户端发起 ping",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "probe_unknown",
        "description": "向客户端发起未知方法请求",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "elicit",
        "description": "向客户端发起 elicitation/create 反向请求",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["simple", "enum", "sensitive", "nested", "empty"],
                },
            },
        },
    },
]


REVERSE_ID = 99000


def elicitation_request(kind):
    if kind == "enum":
        return {
            "message": "请选择喜欢的颜色",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "color": {
                        "type": "string",
                        "title": "颜色",
                        "enum": ["red", "green", "blue"],
                    },
                },
                "required": ["color"],
            },
        }
    if kind == "sensitive":
        return {
            "message": "请输入 API Key",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "title": "API Key"},
                },
                "required": ["api_key"],
            },
        }
    if kind == "nested":
        return {
            "message": "请输入嵌套资料",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
        }
    if kind == "empty":
        return {
            "message": "是否继续执行？",
            "requestedSchema": {
                "type": "object",
                "properties": {},
            },
        }
    return {
        "message": "请输入 GitHub 用户名",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "title": "Username",
                    "minLength": 1,
                    "maxLength": 64,
                },
            },
            "required": ["username"],
        },
    }


def handle_reverse_probe(name, stdin, arguments=None):
    """向客户端发起反向请求并同步等待应答（stub 场景受控，直接 readline）。"""
    global REVERSE_ID
    REVERSE_ID += 1
    rid = REVERSE_ID
    if name == "sample_me":
        request = {"jsonrpc": "2.0", "id": rid, "method": "sampling/createMessage",
                   "params": {"messages": [{"role": "user", "content": {
                       "type": "text", "text": "说 SAMPLE"}}], "maxTokens": 100}}
    elif name == "list_roots":
        request = {"jsonrpc": "2.0", "id": rid, "method": "roots/list", "params": {}}
    elif name == "probe_ping":
        request = {"jsonrpc": "2.0", "id": rid, "method": "ping", "params": {}}
    elif name == "elicit":
        kind = str((arguments or {}).get("kind") or "simple")
        request = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "elicitation/create",
            "params": elicitation_request(kind),
        }
    else:
        request = {"jsonrpc": "2.0", "id": rid, "method": "custom/unsupported",
                   "params": {}}
    send(request)
    # 按 id 匹配等待应答；跳过通知等无关行（对客户端发送时序保持鲁棒）
    resp = None
    while resp is None:
        line = stdin.readline()
        if not line:
            return "(no response: stream closed)"
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            return f"(bad response: {line[:100]!r})"
        if candidate.get("id") == rid:
            resp = candidate
    if "error" in resp:
        return f"error {resp['error'].get('code')}: {resp['error'].get('message')}"
    result = resp.get("result") or {}
    if name == "sample_me":
        return f"sampled={result.get('content', {}).get('text', '')}|model={result.get('model', '')}"
    return json.dumps(result, ensure_ascii=False)


def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break
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
            requested = (msg.get("params") or {}).get("protocolVersion")
            protocol = os.environ.get(
                "KRELCODE_STUB_PROTOCOL_VERSION",
                str(requested or "2025-06-18"),
            )
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": protocol,
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
            elif name in (
                "sample_me", "list_roots", "probe_ping", "probe_unknown", "elicit",
            ):
                text = handle_reverse_probe(name, sys.stdin, args)
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}]}})
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
        elif method is None and mid is not None:
            pass  # 客户端对我方反向请求的应答（已在 handle_reverse_probe 内消费）
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
