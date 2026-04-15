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
atexit.register(lambda: sys.stdout.write("\033[0m"))

from models.anthropic_client import anthropic_client as client
from configs import (
    WORKDIR, 
    MODEL, 
    HARNESS_DIR, 
    SKILL_DIR, 
    CONTEXT_LIMIT,
    PERSIST_TOOL_RESULT
)
from tools import TOOL_HANDLERS, TOOLS, TODO
from tools.common import run_read
from tools.skill import SkillRegistry
from tools.compact_messages import (
    micro_compact, 
    estimate_context_size, 
    compact_history, 
    persist_large_output,
    CompactState
)
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

SKILL_REGISTRY = SkillRegistry(SKILL_DIR)

SYSTEM = f"""You are a coding agent at {str(WORKDIR)}.
Use the todo tool for multi-step work.
Keep exactly one step in_progress when a task has multiple steps.
Refresh the plan as work advances. Prefer tools over prose.

- Keep working step by step, and use compact if the conversation gets too long.

- Use load_skill when a task needs specialized instructions before you act.

Skills available:
{SKILL_REGISTRY.describe_available()}
"""
if HARNESS_DIR:
    SYSTEM += f"\n\nHere is the harness prompt for this project:{HARNESS_DIR.read_text()}"


def agent_loop(messages: list, state: CompactState):
    while True:
        messages[:] = micro_compact(messages)

        if estimate_context_size(messages) > CONTEXT_LIMIT:
            print("[auto compact...]")
            messages[:] = compact_history(messages, state)

        print(f"[main] Thinking ...")
        response = client.messages.create(
            model=MODEL, 
            system=SYSTEM,
            messages=normalize_messages(messages),
            tools=TOOLS, 
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        manual_compact = False
        used_todo = False
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                print(f"[main] tool_use: {block.name}, input: {block.input}")
                try:
                    if block.name == "read_file":
                        output = run_read(state=state, **block.input)
                    else:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"

                    if PERSIST_TOOL_RESULT:
                        output = persist_large_output(block.id, output)

                except Exception as e:
                    output = f"Error: {e}"

                print(f"[main] tool_result: {block.name}: {output[:200]}")

                # Agent使用todo工具
                if block.name == "todo":
                    used_todo = True
                
                # Agent主动压缩
                if block.name == "compact":
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
            TODO.state.rounds_since_update = 0
        else:
            TODO.note_round_without_update()
            reminder = TODO.reminder()
            if reminder:
                results.insert(0, {"type": "text", "text": reminder})

        messages.append({"role": "user", "content": results})

        if manual_compact:
            print("[main] manual compact...")
            messages[:] = compact_history(messages, state, focus=compact_focus)
            print(f"[main] Compact summary: {state.last_summary}")

        


if __name__ == "__main__":
    history = []
    compact_state = CompactState()

    while True:
        try:
            query = input("\001\033[36m\002s01 >> \001\033[0;0m\002")
            if query.strip().lower() in ("q", "exit", ""): break
        except (EOFError, KeyboardInterrupt):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history, compact_state)
        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(f"Answer: {final_text}")
        print()
