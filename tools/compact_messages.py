import json
import time
from typing import List
from dataclasses import dataclass, field
from pathlib import Path


from models.anthropic_client import anthropic_client as client
from configs import (
    MODEL, 
    PERSIST_TOOL_RESULT,
    PERSIST_THRESHOLD, 
    PREVIEW_CHARS, 
    WORKDIR, 
    TRANSCRIPT_DIR,
    TOOL_RESULTS_DIR,
    KEEP_RECENT_TOOL_RESULTS
)


@dataclass
class CompactState:
    has_compacted: bool = False
    last_summary: str = ""
    recent_files: list[str] = field(default_factory=list)


def estimate_context_size(messages: list) -> int:
    # 预估上下文大小
    return len(str(messages))


def collect_tool_result_blocks(messages: List) -> list[tuple[int, int, dict]]:
    blocks = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue

        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((message_index, block_index, block))
    return blocks


def micro_compact(messages: List) -> List:
    # 压缩工具结果，只保留最近KEEP_RECENT_TOOL_RESULTS个工具结果
    tool_results = collect_tool_result_blocks(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    
    for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        content = block.get("content", "")
        if not isinstance(content, str) or len(content) <= 120:
            continue
        block["content"] = "[Earlier tool result compacted. Re-run the tool if you need full detail.]"
    return messages


def track_recent_file(state: CompactState, path: str) -> None:
    # 跟踪最近访问的文件, 保持最近5个文件
    if path in state.recent_files:
        state.recent_files.remove(path)
    state.recent_files.append(path)
    if len(state.recent_files) > 5:
        state.recent_files[:] = state.recent_files[-5:]


def persist_large_output(tool_use_id: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output

    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stored_path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not stored_path.exists():
        stored_path.write_text(output)
    
    preview = output[:PREVIEW_CHARS]
    rel_path = stored_path.relative_to(WORKDIR)

    return (
        "<persisted-output>\n"
        f"Full output saved to: {rel_path}\n"
        "Preview:\n"
        f"{preview}\n"
        "</persisted-output>"
    )


def _write_transcript(messages: List) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as handle:
        for message in messages:
            handle.write(json.dumps(message, default=str) + "\n")
    return path

def _summarize_history(messages: list) -> str:
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve:\n"
        "1. The current goal\n"
        "2. Important findings and decisions\n"
        "3. Files read or changed\n"
        "4. Remaining work\n"
        "5. User constraints and preferences\n"
        "Be compact but concrete.\n\n"
        f"{conversation}"
    )
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    return response.content[0].text.strip()


def compact_history(messages: list, state: CompactState, focus: str | None = None) -> list:
    transcript_path = _write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = _summarize_history(messages)
    if focus:
        summary += f"\n\nFocus to preserve next: {focus}"
    if state.recent_files:
        recent_lines = "\n".join(f"- {path}" for path in state.recent_files)
        summary += f"\n\nRecent files to reopen if needed:\n{recent_lines}"
    state.has_compacted = True
    state.last_summary = summary
    return [{
        "role": "user",
        "content": (
            "This conversation was compacted so the agent can continue working.\n\n"
            f"{summary}"
        ),
    }]