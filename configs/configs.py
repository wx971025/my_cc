import os
from pathlib import Path

# 工作目录
WORKDIR = Path(os.getcwd())

# 参数配置
    # 上下文限制
CONTEXT_LIMIT = 50000
    # 保持最近3个工具结果可见
KEEP_RECENT_TOOL_RESULTS = 3
    # 是否持久化工具结果
PERSIST_TOOL_RESULT = True
    # 超过30000字符的工具结果将被持久化
PERSIST_THRESHOLD = 30000
    # 持久化后的工具结果预览字符数
PREVIEW_CHARS = 2000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

# skill目录
SKILL_DIR = WORKDIR / "skills"
SKILL_DIR.mkdir(parents=True, exist_ok=True)

# harness
HARNESS_DIR = WORKDIR / "docs/harness.md"
if not HARNESS_DIR.exists():
    HARNESS_DIR = None

# 模型
MODEL = "claude-sonnet-4-6"
SUBAGENT_MODEL = "claude-sonnet-4-6"

# -- Permission rules --
DEFAULT_RULES = [
    # Always deny dangerous patterns
    {"tool": "bash", "command": "rm -rf /", "behavior": "deny"},
    {"tool": "bash", "command": "sudo *", "behavior": "deny"},

    # default ask command permission
    {"tool": "bash", "command": "*", "behavior": "ask"},

    # Allow reading anything
    {"tool": "read_file", "path": "*", "behavior": "allow"},
]


MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = ("user", "feedback", "project", "reference")
MAX_INDEX_LINES = 200
