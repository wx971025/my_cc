import json
import os
import subprocess
from pathlib import Path

from configs import WORKDIR


class HookManager:
    """
    Load and execute hooks from .hooks.json configuration.
    The hook manager does three simple jobs:
    - load hook definitions
    - run matching commands for an event
    - aggregate block / message results for the caller
    """
    HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
    HOOK_TIMEOUT = 30  # seconds

    def __init__(self, config_path: Path = None, sdk_mode: bool = False):
        self.hooks = {event: [] for event in self.HOOK_EVENTS}
        self._sdk_mode = sdk_mode
        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                for event in self.HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
                print(f"[Hooks] loaded from {config_path}")
            except Exception as e:
                print(f"[Hook config error: {e}]")
        else:
            print(f"[No hooks found in {config_path}]")

    def _is_workspace_trusted(self, workspace: Path = None) -> bool:
        """
        Check if a workspace has been explicitly marked as trusted.
        The teaching version uses a simple marker file. A more complete system
        can layer richer trust flows on top of the same idea.
        """
        ws = workspace or WORKDIR
        trust_marker = ws / ".claude" / ".claude_trusted"
        return trust_marker.exists()

    def _check_workspace_trust(self) -> bool:
        """
        Check whether the current workspace is trusted.
        The teaching version uses a simple trust marker file.
        In SDK mode, trust is treated as implicit.
        """
        if self._sdk_mode:
            return True
        return self._is_workspace_trusted()

    def run_hooks(self, event: str, context: dict = None) -> dict:
        """
        Execute all hooks for an event.
        Returns: {"blocked": bool, "messages": list[str]}
        - blocked: True if any hook returned exit code 1
        - messages: stderr content from exit-code-2 hooks (to inject)
        """
        result = {"blocked": False, "messages": []}

        # 在不可信的工作区中不使用hook
        if not self._check_workspace_trust():
            return result
        
        hooks = self.hooks.get(event, [])

        for hook_def in hooks:
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue
            
            command = hook_def.get("command", "")
            if not command:
                continue

            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")

                safe_input = {
                    k: (v[:10000] if isinstance(v, str) and len(v) > 10000 else v)
                    for k, v in context.get("tool_input", {}).items()
                }
                env["HOOK_TOOL_INPUT"] = json.dumps(safe_input, ensure_ascii=False)

                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(
                        context["tool_output"]
                    )[:10000]
            try:
                r = subprocess.run(
                    command, 
                    shell=True, 
                    cwd=WORKDIR, 
                    env=env,
                    capture_output=True, 
                    text=True, 
                    timeout=self.HOOK_TIMEOUT,
                )

                if r.returncode == 0:
                    if r.stdout.strip():
                        print(f"  [hook:{event}] {r.stdout.strip()[:100]}")

                    try:
                        hook_output = json.loads(r.stdout)
                        if "updatedInput" in hook_output and context:
                            context["tool_input"] = hook_output["updatedInput"]
                        if "additionalContext" in hook_output:
                            result["messages"].append(
                                hook_output["additionalContext"])
                        if "permissionDecision" in hook_output:
                            result["permission_override"] = (
                                hook_output["permissionDecision"])
                    except (json.JSONDecodeError, TypeError):
                        pass  # stdout was not JSON -- normal for simple hooks
                elif r.returncode == 1:
                    result["blocked"] = True
                    reason = r.stderr.strip() or "Blocked by hook"
                    result["block_reason"] = reason
                    print(f"  [hook:{event}] BLOCKED: {reason[:200]}")
                elif r.returncode == 2:
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)
                        print(f"  [hook:{event}] INJECT: {msg[:200]}")
            except subprocess.TimeoutExpired:
                print(f"  [hook:{event}] Timeout ({self.HOOK_TIMEOUT}s)")
            except Exception as e:
                print(f"  [hook:{event}] Error: {e}")
        return result
