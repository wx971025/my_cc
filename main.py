# Harness: the loop -- keep feeding real tool results back into the model.
"""
agent_loop.py - The Agent Loop
This file teaches the smallest useful coding-agent pattern:
    user message
      -> models reply
      -> if tool_use: execute tools
      -> write tool_result back to messages
      -> continue
It intentionally keeps the loop small, but still makes the loop state explicit
so later chapters can grow from the same structure.
"""
import sys
import atexit
import time
import json
atexit.register(lambda: sys.stdout.write("\033[0m"))
from anthropic import APIError

from models.anthropic_client import anthropic_client as client
from configs import (
    WORKDIR, 
    MODEL, 
    CONTEXT_LIMIT,
    PERSIST_TOOL_RESULT,
    DYNAMIC_BOUNDARY,
    MAX_RECOVERY_ATTEMPTS
)
from tools import TOOL_HANDLERS, TOOLS
from tools.common import run_read
from tools.compact import (
    micro_compact, 
    estimate_context_size, 
    compact_history, 
    persist_large_output,
    CompactState
)
from modules.permission import PermissionManager
from modules.hook import HookManager
from modules.skill import skill_manager
from modules.prompt import SystemPromptBuilder
from modules.retry import backoff_delay
from modules.todo import todo_manager
from modules.cron import cron_scheduler
from modules.teammate import teammate_manager, message_bus
from modules.mcp import mcp_registry
from utils.messages import extract_text, normalize_messages

try:
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

perms = PermissionManager()     # 工具执行权限管理
hooks = HookManager()           # 钩子管理

SYSTEM = SystemPromptBuilder(tools=TOOLS).build()   # 构建system_prompt

def init_workspace_trust():
    """
    Initialize workspace trust.
    """
    print(f"[init_workspace_trust] Initializing workspace trust for >>> {WORKDIR}")
    trust_marker = WORKDIR / ".claude" / ".claude_trusted"
    if trust_marker.exists():
        return True

    answer = input(f"Are you sure you want to initialize workspace trust? (y/n): ")
    if answer.lower() == "y":
        trust_marker.parent.mkdir(parents=True, exist_ok=True)
        trust_marker.touch()
        return True
    else:
        return False


def agent_loop(messages: list, state: CompactState):
    while True:
        messages[:] = micro_compact(messages)

        if estimate_context_size(messages) > CONTEXT_LIMIT:
            print("[auto compact...]")
            messages[:] = compact_history(messages, state)

        # 多Agent, 找同事Agent通知给主Agent的产物
        team_events = teammate_manager.poll_events("lead")
        if team_events:
            lines = ["<team-events>"]
            for ev in team_events:
                if ev.get("type") == "task_result_available":
                    lines.append(
                        f"- [result] request_id={ev['request_id']} "
                        f"from={ev['from']} path={ev['result_path']}\n"
                        f"  summary: {ev.get('summary', '')}"
                    )
                elif ev.get("type") == "task_result_stale":
                    lines.append(
                        f"- [stale] request_id={ev.get('request_id')} "
                        f"from={ev.get('from')} note={ev.get('note')}"
                    )
                else:
                    lines.append(
                        f"- [{ev.get('type')}] from={ev.get('from')}: "
                        f"{str(ev.get('content', ''))[:200]}"
                    )
            lines.append(
                "Use read_file on result_path to fetch the full result when needed."
            )
            lines.append("</team-events>")
            team_payload = "\n".join(lines)

            if messages and messages[-1].get("role") == "user":
                last_content = messages[-1].get("content")
                if isinstance(last_content, str):
                    messages[-1]["content"] = (
                        f"{last_content}\n\n{team_payload}" if last_content.strip() else team_payload
                    )
                elif isinstance(last_content, list):
                    last_content.append({"type": "text", "text": team_payload})
                else:
                    messages[-1]["content"] = f"{str(last_content)}\n\n{team_payload}"
            else:
                messages.append({"role": "user", "content": team_payload})

        # 找定时任务的cron
        notifications = cron_scheduler.drain_notifications()
        if notifications:
            for note in notifications:
                print(f"[Cron notification] {note[:100]}")

            cron_payload = "<cron-notifications>\n" + "\n".join(notifications) + "\n</cron-notifications>"

            if messages and messages[-1].get("role") == "user":
                last_content = messages[-1].get("content")
                if isinstance(last_content, str):
                    if last_content.strip():
                        messages[-1]["content"] = f"{last_content}\n\n{cron_payload}"
                    else:
                        messages[-1]["content"] = cron_payload
                elif isinstance(last_content, list):
                    last_content.append({"type": "text", "text": cron_payload})
                else:
                    messages[-1]["content"] = f"{str(last_content)}\n\n{cron_payload}"
            else:
                messages.append({"role": "user", "content": cron_payload})
        
        for attempt in range(MAX_RECOVERY_ATTEMPTS + 1):
            try:
                print(f"[main] Thinking ...")
                response = client.messages.create(
                    model=MODEL, 
                    system=SYSTEM,
                    messages=normalize_messages(messages),
                    tools=TOOLS, 
                    max_tokens=8000,
                )
                break
            except APIError as e:
                error_body = str(e).lower()

                # prompt超长压缩
                if "overlong_prompt" in error_body or ("prompt" in error_body and "long" in error_body):
                    print(f"[Recovery] Prompt too long. Compacting... (attempt {attempt + 1})")
                    messages[:] = compact_history(messages, state)
                    continue
                
                # 重试
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    delay: float = backoff_delay(attempt)
                    print(f"[Recovery] API error: {e}. "
                          f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RECOVERY_ATTEMPTS})")
                    time.sleep(delay)
                    continue

                # 重试耗尽
                print(f"[Error] API call failed after {MAX_RECOVERY_ATTEMPTS} retries: {e}")
                return
            except (ConnectionError, TimeoutError, OSError) as e:
                # 回滚重试
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    delay = backoff_delay(attempt)
                    print(f"[Recovery] Connection error: {e}. "
                          f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RECOVERY_ATTEMPTS})")
                    time.sleep(delay)
                    continue
                print(f"[Error] Connection failed after {MAX_RECOVERY_ATTEMPTS} retries: {e}")
                return
        if response is None:
            print("[Error] No response received.")
            return
        
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        manual_compact = False
        used_todo = False
        deny_flag = False

        for block in response.content:
            if block.type == "tool_use":
                decision = perms.check(block.name, block.input or {})
                if decision["behavior"] == "deny":      # 系统拒绝授权该操作
                    output = f"Permission denied: {decision['reason']}"
                    deny_flag = True
                    print(f"  [DENIED] {block.name}: {decision['reason']}")

                elif decision["behavior"] == "ask" and not perms.ask_user(block.name, block.input or {}):
                    # 用户拒绝授权该工具
                    output = f"Permission denied by user for {block.name}"
                    deny_flag = True
                    print(f"  [USER DENIED] {block.name}")
                else:
                    # 执行工具
                    tool_input = dict(block.input or {})
                    ctx = {"tool_name": block.name, "tool_input": tool_input}

                    # PreToolUse hook
                    pre_result = hooks.run_hooks("PreToolUse", ctx)

                    hook_prefix = "\n".join(f"[Hook]: {m}" for m in pre_result.get("messages", []))

                    if pre_result.get("blocked"):
                        reason = pre_result.get("block_reason", "Blocked by hook")
                        output = f"{hook_prefix}\nTool blocked by PreToolUse hook: {reason}".strip()
                    else:
                        handler = TOOL_HANDLERS.get(block.name)
                        effective_input = ctx["tool_input"]
                        print(f"[main] tool_use: {block.name}, input: {effective_input}")
                        try:
                            if block.name == "read_file":
                                output = run_read(state=state, **effective_input)
                            else:
                                output = handler(**effective_input) if handler else f"Unknown tool: {block.name}"
                            if PERSIST_TOOL_RESULT:
                                output = persist_large_output(block.id, output)
                        except Exception as e:
                            output = f"Error: {e}"
                        if hook_prefix:
                            output = f"{hook_prefix}\n{output}"
                    
                    ctx["tool_output"] = output
                    post_result = hooks.run_hooks("PostToolUse", ctx)
                    for msg in post_result.get("messages", []):
                        output += f"\n[Hook note]: {msg}"

                print(f"[main] tool_result: {block.name}: {output[:200]}")

                # Agent使用todo工具
                if block.name == "todo" and not deny_flag:
                    used_todo = True
                
                # Agent主动压缩
                if block.name == "compact" and not deny_flag:
                    manual_compact = True
                    compact_focus = (block.input or {}).get("focus")

                results.append(
                    {
                        "type": "tool_result", 
                        "tool_use_id": block.id, 
                        "content": str(output)
                    }
                )
        
        if used_todo:
            todo_manager.state.rounds_since_update = 0
        else:
            todo_manager.note_round_without_update()
            reminder = todo_manager.reminder()
            if reminder:
                results.insert(0, {"type": "text", "text": reminder})

        messages.append({"role": "user", "content": results})

        if manual_compact:
            print("[main] manual compact...")
            messages[:] = compact_history(messages, state, focus=compact_focus)
            print(f"[main] Compact summary: {state.last_summary}")



if __name__ == "__main__":
    if not init_workspace_trust():
        print("[main] Workspace trust not initialized. Exiting...")
        exit(1)
    
    cron_scheduler.start()
    print("[Cron scheduler running. Background checks every second.]")
    print("[Commands: /cron to list tasks, /test to fire a test notification]")


    section_count = SYSTEM.count("\n# ")
    print(f"[System prompt assembled: {len(SYSTEM)} chars, ~{section_count} sections]")

    history = []
    compact_state = CompactState()

    while True:
        try:
            query = input("\001\033[36m\002Agent >> \001\033[0;0m\002")
            if query.strip().lower() in ("q", "exit", ""): 
                break

            if query.strip().lower() == "/help":
                print("--- Help ---")
                print("  /prompt: Show the system prompt")
                print("  /sections: Show the system prompt sections")
                print("  /help: Show this help message")
                print("  /skills: Show the available skills")
                print("  /mcp: Show MCP server status")
                print("  /q: Exit the agent")
                print("--- End ---")
                continue

            if query.strip().lower() == "/prompt":
                print("--- System Prompt ---")
                print(SYSTEM)
                print("--- End ---")
                continue
            
            if query.strip() == "/team":
                print(teammate_manager.list_all())
                continue

            if query.strip() == "/inbox":
                print(teammate_manager.list_pending())
                continue

                
            if query.strip() == "/sections":
                for line in SYSTEM.splitlines():
                    if line.startswith("# ") or line == DYNAMIC_BOUNDARY:
                        print(f"  {line}")
                continue

            
            if query.strip().lower() == "/skills":
                print("--- Skills ---")
                print(skill_manager.skill_describe_available())
                print("--- End ---")
                continue
        
            if query.strip() == "/cron":
                print(cron_scheduler.list_tasks())
                continue

            if query.strip() == "/mcp":
                print(mcp_registry.status())
                continue

            if query.strip() == "/test":
                cron_scheduler.queue.put("[Scheduled task test-0000]: This is a test notification.")
                print("[Test notification enqueued. It will be injected on your next message.]")
                continue

        except (EOFError, KeyboardInterrupt):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history, compact_state)
        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(f"Answer: {final_text}")
        print()
