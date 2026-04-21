"""modules.mcp — 外部 MCP 能力接入层。

对上游只暴露两个对象：
    mcp_registry  单例；tools/__init__.py 在模块 import 末尾 bootstrap
    MCP_PREFIX    工具名前缀常量（"mcp__"），路由/权限代码按需复用
"""
from .registry import mcp_registry, MCP_PREFIX

__all__ = ["mcp_registry", "MCP_PREFIX"]
