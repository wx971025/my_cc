#!/bin/bash
# PostToolUse hook: 在 bash 命令执行后进行记录和检查
#
# 环境变量（由 HookManager 注入）:
#   HOOK_EVENT       - 事件名 (PostToolUse)
#   HOOK_TOOL_NAME   - 工具名 (bash)
#   HOOK_TOOL_INPUT  - 工具输入 JSON
#   HOOK_TOOL_OUTPUT - 工具执行结果（截断至 10000 字符）
#
# 退出码约定:
#   0 - 正常（可选 JSON stdout）
#   2 - 将 stderr 内容作为消息注入对话

COMMAND=$(echo "$HOOK_TOOL_INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('command',''))" 2>/dev/null)
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# 记录执行结果到审计日志
OUTPUT_PREVIEW=$(echo "$HOOK_TOOL_OUTPUT" | head -c 200)
echo "[$TIMESTAMP] cmd: $COMMAND | output: $OUTPUT_PREVIEW" >> .hooks/audit.log

# 示例：如果输出中包含 error/failed，注入提醒让模型注意
if echo "$HOOK_TOOL_OUTPUT" | grep -qiE '\b(error|failed|traceback)\b'; then
    echo "The command output contains errors. Please review carefully before proceeding." >&2
    exit 2
fi

exit 0
