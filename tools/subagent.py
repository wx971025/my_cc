import re
from pathlib import Path
from anthropic import Anthropic

from configs import WORKDIR


class AgentSkillTemplete:
    """
    Parse agent definition from markdown frontmatter.
    Real Claude Code loads agent definitions from .claude/agents/*.md.
    Frontmatter fields: name, tools, disallowedTools, skills, hooks,
    model, effort, permissionMode, maxTurns, memory, isolation, color,
    background, initialPrompt, mcpServers.
    3 sources: built-in, custom (.claude/agents/), plugin-provided.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self.config = {}
        self.system_prompt = ""
        self._parse()

    def _parse(self):
        text = self.path.read_text()
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            self.system_prompt = text
            return
        for line in match.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                self.config[k.strip()] = v.strip()
        self.system_prompt = match.group(2).strip()
        self.name = self.config.get("name", self.name)


class SubAgent:
    SUBAGENT_SYSTEM = (
        f"You are a coding subagent at {str(WORKDIR)}. "
        f"Complete the given task, then summarize your findings."
    )

    def __init__(self, 
        client: Anthropic, 
        model: str, 
        system_prompt: str = SUBAGENT_SYSTEM, 
        tools: dict = None,
        tools_handlers: dict = None,
    ):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt or self.SUBAGENT_SYSTEM
        self.tools = tools
        self.tools_handlers = tools_handlers

    def run_subagent(self, prompt: str) -> str:
        sub_messages = [{"role": "user", "content": prompt}]
        for _ in range(30):  # safety limit
            response = self.client.messages.create(
                model=self.model, system=self.system_prompt, messages=sub_messages,
                tools=self.tools, max_tokens=8000,
            )
            sub_messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[subagent] tool_use: {block.name}, input: {block.input}")
                    handler = self.tools_handlers.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    print(f"[subagent] output: {output}")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
            sub_messages.append({"role": "user", "content": results})
        # Only the final text returns to the parent -- child context is discarded
        return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"
