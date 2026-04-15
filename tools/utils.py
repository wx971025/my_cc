import re
import json
from pathlib import Path
from fnmatch import fnmatch

from configs import WORKDIR, DEFAULT_RULES


READ_ONLY_TOOLS = {"read_file", "bash_readonly"}
WRITE_TOOLS = {"write_file", "edit_file", "bash"}

def safe_path(p: Path | str) -> Path:
    if isinstance(p, str):
        p = Path(p)
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


class BashSecurityValidator:
    """
    Validate bash commands for obviously dangerous patterns.
    The teaching version deliberately keeps this small and easy to read.
    First catch a few high-risk patterns, then let the permission pipeline
    decide whether to deny or ask the user.
    """

    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),         # shell metacharacters
        ("sudo", r"\bsudo\b"),                  # privilege escalation, 高风险
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),    # recursive delete, 高风险
        ("cmd_substitution", r"\$\("),          # command substitution
        ("ifs_injection", r"\bIFS\s*="),        # IFS manipulation
    ]

    def validate(self, command: str) -> list:
        """
        Check a bash command against all validators.
        Returns list of (validator_name, matched_pattern) tuples for failures.
        An empty list means the command passed all validators.
        """
        failures = []
        for name, pattern in self.VALIDATORS:
            if re.search(pattern, command):
                failures.append((name, pattern))
        return failures

    def is_safe(self, command: str) -> bool:
        """Convenience: returns True only if no validators triggered."""
        return len(self.validate(command)) == 0


    def describe_failures(self, command: str) -> str:
        """Human-readable summary of validation failures."""
        failures = self.validate(command)
        if not failures:
            return "No issues detected"
        parts = [f"{name} (pattern: {pattern})" for name, pattern in failures]
        return "Security flags: " + ", ".join(parts)


# -- Workspace trust --
def is_workspace_trusted(workspace: Path = None) -> bool:
    """
    Check if a workspace has been explicitly marked as trusted.
    The teaching version uses a simple marker file. A more complete system
    can layer richer trust flows on top of the same idea.
    """
    ws = workspace or WORKDIR
    trust_marker = ws / ".claude" / ".claude_trusted"
    return trust_marker.exists()


class PermissionManager:
    """
    Manages permission decisions for tool calls.
    Pipeline: deny_rules -> mode_check -> allow_rules -> ask_user
    The teaching version keeps the decision path short on purpose so readers
    can implement it themselves before adding more advanced policy layers.
    """

    def __init__(self, mode: str = "default", rules: list = None):
        MODES = ("default", "plan", "auto")
        if mode not in MODES:
            raise ValueError(f"Unknown mode: {mode}. Choose from {MODES}")
        self.mode = mode
        self.rules = rules or list(DEFAULT_RULES)

        # 当前被拒次数
        self.consecutive_denials = 0
        self.max_consecutive_denials = 3
        self.bash_validator = BashSecurityValidator()

    
    def _matches(self, rule: dict, tool_name:str, tool_input:dict) -> bool:
        """Check if a rule matches the tool call."""
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False
            
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False
        
        if "command" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["command"]):
                return False
        return True


    def check(self, tool_name: str, tool_input: dict) -> dict:
        """
        Returns: {"behavior": "allow"|"deny"|"ask", "reason": str}
        """
        # bash工具单独处理
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = self.bash_validator.validate(command)
            if failures:
                # 默认高风险指令，直接拒绝
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = self.bash_validator.describe_failures(command)
                    return {
                        "behavior": "deny",
                        "reason": f"Bash validator: {desc}"
                    }

                # 默认中风险指令, 发起询问
                desc = self.bash_validator.describe_failures(command)
                return {
                    "behavior": "ask",
                    "reason": f"Bash validator flagged: {desc}"
                }
        
        # 检查命中 deny 规则的工具, 直接拒绝
        for rule in self.rules:
            if rule['behavior'] != 'deny':
                continue

            if self._matches(rule, tool_name, tool_input):
                return {
                    "behavior": "deny",
                    "reason": f"Blocked by deny rule: {rule}"
                }
            
        
        if self.mode == "plan":
            # 计划模式: 拒绝所有写操作, 允许读操作
            if tool_name in WRITE_TOOLS:
                return {
                    "behavior": "deny",
                    "reason": "Plan mode: write operations are blocked"
                }
            return {
                "behavior": "allow", 
                "reason": "Plan mode: read-only allowed"
            }

        if self.mode == "auto":
            # 自动模式: 自动允许只读工具, 询问写操作
            if tool_name in READ_ONLY_TOOLS or tool_name == "read_file":
                return {
                    "behavior": "allow",
                    "reason": "Auto mode: read-only tool auto-approved"
                }
        
        # 放通所有allow的工具
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue

            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0
                return {
                    "behavior": "allow",
                    "reason": f"Matched allow rule: {rule}"
                }

        return {
            "behavior": "ask",
            "reason": f"No rule matched for {tool_name}, asking user"
        }


    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f"\n  [Permission] {tool_name}: {preview}")
        # 等待授权
        try:
            answer = input("  Allow? (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        
        if answer == "always":
            # Add permanent allow rule for this tool
            self.rules.append({"tool": tool_name, "path": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True
        
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True

        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            print(f"  [{self.consecutive_denials} consecutive denials -- "
                  "consider switching to plan mode]")
        return False
