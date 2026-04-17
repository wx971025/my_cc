from typing import Callable, cast

from models.anthropic_client import anthropic_client as client
from configs import SUBAGENT_MODEL, SKILL_DIR
from modules.skill import skill_manager
from modules.memory import memory_manager

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
        "name": "bash_readonly",
        "description": "Run a shell command, but do not allow any changes to the filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
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
    }
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
    {
        "name": "load_skill",
        "description": "Load the full body of a named skill into the current context.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "compact",
        "description": "Summarize earlier conversation so work can continue in a smaller context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string"},
            },
        },
    },
    {
        "name": "save_memory", 
        "description": "Save a persistent memory that survives across sessions.",
        "input_schema": {
            "type": "object", 
            "properties": {
                "name": {"type": "string", "description": "Short identifier (e.g. prefer_tabs, db_schema)"},
                "description": {"type": "string", "description": "One-line summary of what this memory captures"},
                "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"], "description": "user=preferences, feedback=corrections, project=non-obvious project conventions or decision reasons, reference=external resource pointers"},
                "content": {"type": "string", "description": "Full memory content (multi-line OK)"},
            }, 
            "required": ["name", "description", "type", "content"]
        },
    },
]


SUB_AGENT_TOOL_HANDLERS = cast(
    dict[str, Callable],
    {
        "bash":       lambda **kw: run_bash(kw["command"]),
        "bash_readonly": lambda **kw: run_bash(kw["command"]),
        "read_file":  lambda **kw: run_read(state=kw["state"], path=kw["path"], limit=kw.get("limit")),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
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
        "todo":       lambda **kw: TODO.update(kw["items"]),
        "task":       lambda **kw: SUB_AGENT.run_subagent(kw["prompt"]),
        "load_skill": lambda **kw: skill_manager.load_full_skill_body(kw["name"]),
        "compact":    lambda **kw: "Compaction triggered.",
        "save_memory":  lambda **kw: memory_manager.save_memory(kw["name"], kw["description"], kw["type"], kw["content"]),
    }
)

READ_ONLY_TOOLS = {"read_file", "bash_readonly"}
WRITE_TOOLS = {"write_file", "edit_file", "bash"}
