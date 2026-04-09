from typing import Callable, cast

from model.anthropic_client import anthropic_client as client
from configs import WORKDIR, SUBAGENT_MODEL

from .common import run_read, run_write, run_bash, run_edit
from .todo import TODO
from .subagent import SubAgent


SUB_AGENT_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "todo",
        "description": "Rewrite the current session plan for multi-step work.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "Optional present-continuous label.",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["items"],
        },
    },
]

TOOLS = SUB_AGENT_TOOLS + [
    {
        "name": "task", 
        "description": ("Spawn a subagent with fresh context. "
                        "It shares the filesystem but not conversation history."),
        "input_schema": {
            "type": "object", 
            "properties": {
                "prompt": {
                    "type": "string"
                }, 
                "description": {
                    "type": "string", 
                    "description": "Short description of the task",
                },
            }, 
            "required": ["prompt"],
        },      
    },
]


SUB_AGENT_TOOL_HANDLERS = cast(
    dict[str, Callable],
    {
        "bash":       lambda **kw: run_bash(kw["command"]),
        "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
        "todo":       lambda **kw: TODO.update(kw["items"]),
    }
)
SUB_AGENT = SubAgent(
    client, SUBAGENT_MODEL, 
    tools=SUB_AGENT_TOOLS, 
    tools_handlers=SUB_AGENT_TOOL_HANDLERS,
)

TOOL_HANDLERS = SUB_AGENT_TOOL_HANDLERS | cast(
    dict[str, Callable],
    {
        "task":       lambda **kw: SUB_AGENT.run_subagent(kw["prompt"]),
    }
)
