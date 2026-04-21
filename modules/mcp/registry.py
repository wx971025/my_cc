"""
MCPRegistry: 读 .mcp.json → 启动所有 server → 把它们的工具合并进 agent 工具池。

============================================================================
一、定位
============================================================================
MCPClient 是"一根线"（单 server）；MCPRegistry 是"总机"（多 server + 工具池）。

它解决三件事:
    1) 从哪儿来：读 .mcp.json 的 mcpServers 字段，每条起一个 MCPClient
    2) 叫什么：给每个远端工具贴前缀 mcp__<server_alias>__<tool_name>，
             避免和本地工具撞名
    3) 怎么调：为每个远端工具生成一个本地 handler(**kwargs) → client.call_tool

============================================================================
二、和主 agent 的接口
============================================================================
bootstrap() 返回 (tools, handlers)，tools 直接 merge 到 TOOLS，handlers
merge 到 TOOL_HANDLERS。主循环 `main.py::agent_loop` 无需改一行：
    · 权限闸门 perms.check 仍会拦截 mcp__* 工具（没匹配规则默认 ask）
    · hooks PreToolUse / PostToolUse 照常触发
    · tool_result 按老路径回流，模型看不出本地 / 远端的区别

============================================================================
三、失败兜底
============================================================================
某个 server 启动或 list_tools 失败，不会让整个 agent 起不来；该 server 的
错误记在 self.errors，status() 随时可查，其他 server 的工具照常可用。
"""
import atexit
import json
import re

from configs import WORKDIR
from .client import MCPClient


MCP_CONFIG_PATH = WORKDIR / ".mcp.json"

# Anthropic tool name 限制: ^[a-zA-Z0-9_-]{1,64}$
MCP_PREFIX = "mcp__"
_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(name: str) -> str:
    """把 alias / tool name 里不合规字符替换成 _。仅做最保守清洗。"""
    return _NAME_SAFE.sub("_", name)


class MCPRegistry:
    """单例；tools/__init__.py 在模块 import 末尾触发 bootstrap。"""

    def __init__(self):
        self.clients: dict[str, MCPClient] = {}
        self.tools: list[dict] = []
        self.handlers: dict = {}
        self.errors: dict[str, str] = {}   # alias → last_error
        self._bootstrapped = False

    # -------------------------------------------------------------------
    # 配置
    # -------------------------------------------------------------------

    def _load_config(self) -> dict:
        """读 .mcp.json；不存在或损坏都返回空配置，不抛。"""
        if not MCP_CONFIG_PATH.exists():
            return {"mcpServers": {}}
        try:
            return json.loads(MCP_CONFIG_PATH.read_text())
        except Exception as e:
            print(f"[mcp] failed to read {MCP_CONFIG_PATH}: {e}")
            return {"mcpServers": {}}

    # -------------------------------------------------------------------
    # 启动
    # -------------------------------------------------------------------

    def bootstrap(self) -> tuple[list[dict], dict]:
        """
        启动所有 server 并收集工具。幂等：多次调用只做一次。
        返回 (tools_schema, handlers_dict)
        """
        if self._bootstrapped:
            return self.tools, self.handlers

        cfg = self._load_config()
        servers = cfg.get("mcpServers") or {}
        for alias, entry in servers.items():
            self._bootstrap_one(alias, entry)

        self._bootstrapped = True
        atexit.register(self.shutdown_all)
        return self.tools, self.handlers

    def _bootstrap_one(self, alias: str, entry: dict) -> None:
        if entry.get("disabled"):
            self.errors[alias] = "disabled in config"
            return

        command = entry.get("command")
        if not command:
            self.errors[alias] = "missing 'command' in config"
            return

        client = MCPClient(
            name=alias,
            command=command,
            args=entry.get("args") or [],
            env=entry.get("env") or {},
            cwd=entry.get("cwd"),
        )
        ack = client.start()
        if not ack.startswith("OK"):
            self.errors[alias] = client.last_error or ack
            print(f"[mcp] server '{alias}' failed: {self.errors[alias]}")
            return

        try:
            remote_tools = client.list_tools()
        except Exception as e:
            self.errors[alias] = f"list_tools failed: {e}"
            client.stop()
            print(f"[mcp] server '{alias}' list_tools failed: {e}")
            return

        self.clients[alias] = client
        safe_alias = _sanitize(alias)

        added = 0
        for t in remote_tools:
            raw_name = t.get("name") or ""
            if not raw_name:
                continue
            safe_name = _sanitize(raw_name)
            full = f"{MCP_PREFIX}{safe_alias}__{safe_name}"
            if len(full) > 64:
                # tool name 硬上限；截断但保留前缀可辨识
                full = full[:64]
            schema = t.get("inputSchema") or {
                "type": "object", "properties": {},
            }
            desc = t.get("description") or f"MCP tool {raw_name} from {alias}"
            self.tools.append({
                "name": full,
                "description": f"[mcp/{alias}] {desc}",
                "input_schema": schema,
            })
            self.handlers[full] = self._make_handler(alias, raw_name)
            added += 1

        print(f"[mcp] server '{alias}' ready ({added} tools)")

    def _make_handler(self, alias: str, tool_name: str):
        """
        生成闭包 handler。闭包捕获 alias/tool_name，执行时去 self.clients
        动态查 client，避免重连/替换场景下 handler 拿到过期对象。
        """
        def handler(**kwargs):
            client = self.clients.get(alias)
            if client is None or not client.initialized:
                return f"Error: mcp server '{alias}' not available"
            try:
                return client.call_tool(tool_name, kwargs)
            except Exception as e:
                return f"Error: mcp call '{alias}::{tool_name}' failed: {e}"
        return handler

    # -------------------------------------------------------------------
    # 运维视图
    # -------------------------------------------------------------------

    def status(self) -> str:
        if not self.clients and not self.errors:
            return "No MCP servers configured (missing or empty .mcp.json)."
        lines = ["MCP servers:"]
        for alias, _client in self.clients.items():
            prefix = f"{MCP_PREFIX}{_sanitize(alias)}__"
            tool_count = sum(1 for t in self.tools
                             if t["name"].startswith(prefix))
            lines.append(f"  {alias}: ready ({tool_count} tools)")
        for alias, err in self.errors.items():
            lines.append(f"  {alias}: ERROR — {err}")
        return "\n".join(lines)

    # -------------------------------------------------------------------
    # 收尾
    # -------------------------------------------------------------------

    def shutdown_all(self) -> None:
        """atexit 时兜底。单独调用也安全；幂等。"""
        for client in list(self.clients.values()):
            try:
                client.stop()
            except Exception:
                pass
        self.clients.clear()


mcp_registry = MCPRegistry()
