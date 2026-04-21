"""
最小 MCP server 示例（stdio / JSON-RPC 2.0）

============================================================================
教学目的
============================================================================
本文件故意不依赖任何 MCP SDK，只用标准库实现最小骨架：
    · 逐行读 stdin 拿 JSON-RPC 消息
    · 支持 initialize / notifications/initialized / tools/list / tools/call
    · 暴露三个无副作用的 demo 工具：echo / now / env_get
配合 modules/mcp/client.py 的 MCPClient 就能被主 Agent 动态发现和调用。

真实世界的 MCP server 会多出 resources / prompts / elicitation / auth /
SSE transport 等，教学版刻意砍掉。

============================================================================
手动连通自测
============================================================================
    printf '%s\\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \\
                  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \\
      | python -u mcp_servers/echo_server.py

应看到两条 JSON 响应，分别含 serverInfo 和 tools 列表。
"""
import json
import os
import sys
import time


PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the input text back verbatim.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "now",
        "description": "Return the server process's current unix timestamp.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "env_get",
        "description": "Read a single environment variable from the server process.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
]


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _call_tool(name, args):
    args = args or {}
    if name == "echo":
        return {"content": [{"type": "text",
                             "text": str(args.get("text", ""))}]}
    if name == "now":
        return {"content": [{"type": "text", "text": str(time.time())}]}
    if name == "env_get":
        key = str(args.get("key", ""))
        return {"content": [{"type": "text",
                             "text": os.environ.get(key, "")}]}
    raise ValueError(f"Unknown tool: {name}")


def _handle(msg):
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "echo-server", "version": "0.1.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            return _ok(req_id, _call_tool(name, arguments))
        except Exception as e:
            return _err(req_id, -32000, f"Tool '{name}' failed: {e}")

    if req_id is None:
        return None
    return _err(req_id, -32601, f"Method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
