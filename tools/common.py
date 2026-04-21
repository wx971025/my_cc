import subprocess
import os
from pathlib import Path

from .utils import safe_path_at
from .compact import CompactState, track_recent_file


def run_bash(command: str, cwd: str | Path | None = None) -> str:
    """
    shell 命令执行。

    cwd:
        - None     → 用 os.getcwd()（兼容老行为）
        - Path/str → 用作子进程 cwd（worktree 车道目录）
    """
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd) if cwd else os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
    output = (result.stdout + result.stderr).strip()

    return output[:50000] if output else "(no output)"


def run_read(
    *,
    state: CompactState | None = None,
    path: str = "",
    limit: int | None = None,
    base: Path | None = None,
) -> str:
    """
    base: 相对路径的解析基点；None 时仍然相对 WORKDIR（兼容老行为）。
    """
    try:
        if path == "":
            return ""
        if state:
            track_recent_file(state, path)
        lines = safe_path_at(path, base).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        output = "\n".join(lines)
        return output
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, base: Path | None = None) -> str:
    try:
        fp = safe_path_at(path, base)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str, base: Path | None = None) -> str:
    try:
        fp = safe_path_at(path, base)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"
