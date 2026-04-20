from typing import Callable, cast

from models.anthropic_client import anthropic_client as client
from configs import SUBAGENT_MODEL
from modules.skill import skill_manager
from modules.memory import memory_manager
from modules.task import task_manager
from modules.todo import todo_manager
from modules.backgroundTask import background_manager

from .common import run_read, run_write, run_bash, run_edit
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
        "name": "subagent", 
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
        "name": "load_skill",
        "description": "Load the full body of a named skill into the current context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}
            },
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
    {
        "name": "task_create", 
        "description": "Create a new task.",
        "input_schema": {   
            "type": "object", 
            "properties": 
                {
                    "subject": {"type": "string"}, 
                    "description": {"type": "string"}
                }, 
                "required": ["subject"]
        }
    },
    {
        "name": "task_update", 
        "description": "Update a task's status, owner, or dependencies.",
        "input_schema": {
            "type": "object", 
            "properties": {
                "task_id": {"type": "integer"}, 
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, 
                "owner": {"type": "string", "description": "Set when a teammate claims the task"}, 
                "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, 
                "addBlocks": {"type": "array", "items": {"type": "integer"}}
            }, 
            "required": ["task_id"]
        }
    },
    {
        "name": "task_list", 
        "description": "List all tasks with status summary.",
        "input_schema": {
            "type": "object", "properties": {}
        }
    },
    {
        "name": "task_get", 
        "description": "Get full details of a task by ID.",
        "input_schema": {
            "type": "object", 
            "properties": {
                "task_id": {"type": "integer"}
            }, 
            "required": ["task_id"]
        }
    },
    {
        "name": "background_run", 
        "description": "Run command in background thread. Returns task_id immediately.",
        "input_schema": {
            "type": "object", 
            "properties": {
                "command": {"type": "string"}
            }, 
            "required": ["command"]
        }
    },
    {
        "name": "check_background", 
        "description": "Check background task status. Omit task_id to list all.",
        "input_schema": {
            "type": "object", 
            "properties": {
                "task_id": {"type": "string"}
            }, 
            "required": ["task_id"]
        }
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
        "todo":       lambda **kw: todo_manager.update(kw["items"]),
    }
)

sub_agent = SubAgent(
    client, SUBAGENT_MODEL, 
    tools=SUB_AGENT_TOOLS, 
    tools_handlers=SUB_AGENT_TOOL_HANDLERS,
)

TOOL_HANDLERS = SUB_AGENT_TOOL_HANDLERS | cast(
    dict[str, Callable],
    {   
        "subagent":       lambda **kw: sub_agent.run_subagent(kw["prompt"]),
        "load_skill":     lambda **kw: skill_manager.load_full_skill_body(kw["name"]),
        "compact":        lambda **kw: "Compaction triggered.",
        "save_memory":    lambda **kw: memory_manager.save_memory(kw["name"], kw["description"], kw["type"], kw["content"]),
        "task_create":    lambda **kw: task_manager.create(kw["subject"], kw.get("description", "")),
        "task_update":    lambda **kw: task_manager.update(kw["task_id"], kw.get("status"), kw.get("owner"), kw.get("addBlockedBy"), kw.get("addBlocks")),
        "task_list":      lambda **kw: task_manager.list_all(),
        "task_get":       lambda **kw: task_manager.get(kw["task_id"]),
        "background_run":   lambda **kw: background_manager.run(kw["command"]),
        "check_background": lambda **kw: background_manager.check(kw.get("task_id")),
    }
)

READ_ONLY_TOOLS = {"read_file", "bash_readonly"}
WRITE_TOOLS = {"write_file", "edit_file", "bash"}
