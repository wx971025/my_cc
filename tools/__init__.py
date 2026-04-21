import json
from typing import Callable, cast

from models.anthropic_client import anthropic_client as client
from configs import SUBAGENT_MODEL
from modules.skill import skill_manager
from modules.memory import memory_manager
from modules.task import task_manager
from modules.todo import todo_manager
from modules.backgroundTask import background_manager
from modules.cron import cron_scheduler
from modules.teammate import teammate_manager, message_bus, VALID_MSG_TYPES
from modules.taskBoard import publish_task, scan_unclaimed_tasks
from modules.worktree import worktree_manager
from modules import mergeQueue as merge_queue
from modules.mcp import mcp_registry

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
    {
        "name": "cron_create", 
        "description": "Schedule a recurring or one-shot task with a cron expression.",
        "input_schema": {
            "type": "object", 
            "properties": {
                "cron": {"type": "string", "description": "5-field cron expression: 'min hour dom month dow'"},
                "prompt": {"type": "string", "description": "The prompt to inject when the task fires"},
                "recurring": {"type": "boolean", "description": "true=repeat, false=fire once then delete. Default true."},
                "durable": {"type": "boolean", "description": "true=persist to disk, false=session-only. Default false."},
            },
            "required": ["cron", "prompt"]
        }
    },
    {
        "name": "cron_delete", 
        "description": "Delete a scheduled task by ID.",
        "input_schema": {
            "type": "object", 
            "properties": {
                "id": {"type": "string", "description": "Task ID to delete"},
            }, "required": ["id"]
        }
    },
    {
        "name": "cron_list", 
        "description": "List all scheduled tasks.",
        "input_schema": {
            "type": "object", 
            "properties": {},
        }
    },
    {
        "name": "cron_stop", 
        "description": "Stop the cron scheduler.",
        "input_schema": {
            "type": "object", 
            "properties": {},
        }
    },
    {
        "name": "spawn_teammate",
        "description": (
            "Spawn an idle teammate worker that runs in its own thread and waits for tasks. "
            "Spawning does NOT start any work; call assign_task to dispatch work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string", "description": "Short role description (e.g. 'backend-engineer')"},
            },
            "required": ["name", "role"],
        },
    },
    {
        "name": "assign_task",
        "description": (
            "Dispatch a task to an already-spawned teammate. Returns a request_id. "
            "When the teammate finishes, a task_result_available event will be injected "
            "in a future loop turn with a result_path pointer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Target teammate name"},
                "task": {"type": "string", "description": "Full task description for the teammate"},
            },
            "required": ["name", "task"],
        },
    },
    {
        "name": "list_teammates",
        "description": "List all teammates with name, role, status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_pending_tasks",
        "description": "List dispatched teammate tasks and their statuses.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "shutdown_teammate",
        "description": "Ask a teammate worker to shut down gracefully.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "send_message",
        "description": "Send a message to a teammate's inbox (as 'lead').",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
                "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)},
            },
            "required": ["to", "content"],
        },
    },
    {
        "name": "broadcast",
        "description": "Send a message to all teammates (as 'lead').",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "board_post_task",
        "description": (
            "Post a task onto the shared task board for any eligible teammate to claim. "
            "Use claim_role to restrict which role can pick it up (e.g. 'backend'). "
            "Teammates scan the board whenever their personal inbox is empty, so this "
            "is the right tool for parallelizable / role-agnostic work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "claim_role": {
                    "type": "string",
                    "description": "Optional. Only teammates with this role may claim.",
                },
            },
            "required": ["subject"],
        },
    },
    {
        "name": "board_list_unclaimed",
        "description": (
            "List pending, unclaimed tasks currently on the shared board "
            "(optionally filtered by role)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
            },
        },
    },
    {
        "name": "worktree_list",
        "description": (
            "List all isolated worktree lanes with status, bound task_id and branch. "
            "Worktrees are created automatically when a teammate claims a board task."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "worktree_info",
        "description": (
            "Inspect one worktree lane by name. Shows path, branch, task_id, status, "
            "last_entered_at, last_command_preview, closeout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "worktree_closeout",
        "description": (
            "Manually close out an isolated worktree lane. "
            "action='keep' retains the directory (for review); "
            "action='remove' runs `git worktree remove`. "
            "Dirty worktrees are auto-downgraded to keep. "
            "Set complete_task=true to also mark the bound task as completed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "action": {"type": "string", "enum": ["keep", "remove"]},
                "reason": {"type": "string"},
                "complete_task": {"type": "boolean"},
            },
            "required": ["name", "action"],
        },
    },
    {
        "name": "merge_queue_list",
        "description": (
            "List pending merge requests: worker commits sitting on wt/* "
            "branches, waiting for the lead agent to review and integrate. "
            "Each entry shows task_id, branch, owner, file count, status "
            "(ready_for_review / merged / conflicted / rejected)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "merge_review",
        "description": (
            "Show full details of one merge request: files_changed list, "
            "diff_stat, commit_sha, base_sha, worker summary. Use this "
            "before deciding to integrate or reject."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "mcp_status",
        "description": (
            "List configured MCP servers and their tool counts. Use this "
            "to check whether external capabilities declared in .mcp.json "
            "came up correctly; failed servers show their last_error."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "merge_integrate",
        "description": (
            "Integrate one merge request into the main branch.\n"
            "strategy='merge'  → runs `git merge --no-ff wt/<name>` "
            "serially; on conflict aborts and marks entry 'conflicted' "
            "with the conflicting file list returned. "
            "On success deletes the worker branch and marks the bound "
            "task completed.\n"
            "strategy='reject' → marks the request rejected; the branch "
            "and its commits stay on disk for later inspection. "
            "The bound task is NOT auto-completed for 'reject' (the lead "
            "can decide to re-dispatch, closeout manually, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "strategy": {"type": "string", "enum": ["merge", "reject"]},
                "reason": {
                    "type": "string",
                    "description": "Free-form reason, surfaced back to worker.",
                },
            },
            "required": ["task_id", "strategy"],
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
        "cron_create": lambda **kw: cron_scheduler.create(kw["cron"], kw["prompt"], kw.get("recurring", True), kw.get("durable", False)),
        "cron_delete": lambda **kw: cron_scheduler.delete(kw["id"]),
        "cron_list":   lambda **kw: cron_scheduler.list_tasks(),
        "cron_stop":   lambda **kw: cron_scheduler.stop(),
        "spawn_teammate":     lambda **kw: teammate_manager.spawn(kw["name"], kw["role"]),
        "assign_task":        lambda **kw: teammate_manager.dispatch(kw["name"], kw["task"]),
        "list_teammates":     lambda **kw: teammate_manager.list_all(),
        "list_pending_tasks": lambda **kw: teammate_manager.list_pending(),
        "shutdown_teammate":  lambda **kw: teammate_manager.shutdown(kw["name"]),
        "send_message":       lambda **kw: message_bus.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
        "broadcast":          lambda **kw: message_bus.broadcast("lead", kw["content"], teammate_manager.member_names()),
        "board_post_task":    lambda **kw: publish_task(kw["subject"], kw.get("description", ""), kw.get("claim_role", "")),
        "board_list_unclaimed": lambda **kw: json.dumps(scan_unclaimed_tasks(kw.get("role")), indent=2, ensure_ascii=False),
        "worktree_list":      lambda **kw: worktree_manager.list_all(),
        "worktree_info":      lambda **kw: worktree_manager.info(kw["name"]),
        "worktree_closeout":  lambda **kw: worktree_manager.closeout(
            kw["name"], kw["action"],
            reason=kw.get("reason", ""),
            complete_task=bool(kw.get("complete_task", False)),
        ),
        "merge_queue_list":   lambda **kw: merge_queue.list_all(),
        "merge_review":       lambda **kw: merge_queue.review(kw["task_id"]),
        "merge_integrate":    lambda **kw: merge_queue.integrate(
            kw["task_id"], kw["strategy"], kw.get("reason", ""),
        ),
        "mcp_status":         lambda **kw: mcp_registry.status(),
    }
)


# ---------------------------------------------------------------------------
# MCP: 启动 .mcp.json 声明的外部 server，把它们的工具合并进主工具池
# ---------------------------------------------------------------------------
# 设计意图：
#   MCP 工具必须和本地工具处在同一控制面 —— 同一个 TOOLS 列表、同一个
#   TOOL_HANDLERS 字典、同一条权限闸门、同一条 tool_result 回流。
#   因此我们在模块 import 末尾就把远端工具 merge 进来，主循环无需感知。
#
# 命名：    mcp__<server_alias>__<tool_name>  （见 modules/mcp/registry.py）
# 失败：    启动失败的 server 只记 self.errors，不影响其他工具
# 退出：    registry 用 atexit 兜底关掉所有子进程
_mcp_tools, _mcp_handlers = mcp_registry.bootstrap()
if _mcp_tools:
    TOOLS = TOOLS + _mcp_tools
    TOOL_HANDLERS = {**TOOL_HANDLERS, **_mcp_handlers}


READ_ONLY_TOOLS = {"read_file", "bash_readonly"}
WRITE_TOOLS = {"write_file", "edit_file", "bash"}
