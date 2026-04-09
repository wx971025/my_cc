# Harness: the loop -- keep feeding real tool results back into the model.
"""
s01_agent_loop.py - The Agent Loop
This file teaches the smallest useful coding-agent pattern:
    user message
      -> model reply
      -> if tool_use: execute tools
      -> write tool_result back to messages
      -> continue
It intentionally keeps the loop small, but still makes the loop state explicit
so later chapters can grow from the same structure.
"""
import atexit
import sys
atexit.register(lambda: sys.stdout.write("\033[0m"))

from model.anthropic_client import anthropic_client as client
from configs import WORKDIR, MODEL
from tools import TOOL_HANDLERS, TOOLS, TODO
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


SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool for multi-step work.
Keep exactly one step in_progress when a task has multiple steps.
Refresh the plan as work advances. Prefer tools over prose."""


def agent_loop(messages: list):
    while True:
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
        used_todo = False
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                print(f"[main] tool_use: {block.name}, input: {block.input}")
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"[main] tool_result: {block.name}: {output[:200]}")
                results.append(
                    {
                        "type": "tool_result", 
                        "tool_use_id": block.id, 
                        "content": output
                    }
                )
                if block.name == "todo":
                    used_todo = True
        if used_todo:
            TODO.state.rounds_since_update = 0
        else:
            TODO.note_round_without_update()
            reminder = TODO.reminder()
            if reminder:
                results.insert(0, {"type": "text", "text": reminder})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\001\033[36m\002s01 >> \001\033[0m\002")
            if query.strip().lower() in ("q", "exit", ""):
                break
        except (EOFError, KeyboardInterrupt):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)
        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(f"Answer: {final_text}")
        print()
