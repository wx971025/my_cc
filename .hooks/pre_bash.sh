#!/bin/bash
# PreToolUse hook: 在 bash 命令执行前进行安全检查
#
# 环境变量（由 HookManager 注入）:
#   HOOK_EVENT      - 事件名 (PreToolUse)
#   HOOK_TOOL_NAME  - 工具名 (bash)
#   HOOK_TOOL_INPUT - 工具输入 JSON，如 {"command": "ls -la"}
#
# 退出码约定:
#   0 - 放行（stdout 若为 JSON 可携带 updatedInput / additionalContext）
#   1 - 阻止执行（stderr 作为阻止原因返回给模型）
#   2 - 放行，但将 stderr 内容作为消息注入对话

COMMAND=$(echo "$HOOK_TOOL_INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('command',''))" 2>/dev/null)

# 阻止 curl 下载可执行文件
if echo "$COMMAND" | grep -qE 'curl.*\|\s*bash'; then
    echo "Blocked: piping curl to bash is not allowed" >&2
    exit 1
fi

# 对 git push 注入提醒
if echo "$COMMAND" | grep -qE '\bgit\s+push\b'; then
    echo "Reminder: make sure all tests pass before pushing." >&2
    exit 2
fi

# 记录即将执行的命令
echo "[pre_bash] will run: $COMMAND" >> .hooks/audit.log

exit 0
