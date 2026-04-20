# learn-claude-code

一个面向学习的最小化 Coding Agent 项目：用清晰、可读的 Python 代码实现“对话 -> 工具调用 -> 权限决策 -> 结果回写 -> 继续推理”的完整循环。

项目重点不是“功能最多”，而是把 Claude Code 一类 Agent 的核心机制拆解成容易理解的模块：
- Agent Loop
- Tool Use / Tool Result
- 权限管道（Permission Pipeline）
- Hook 机制
- 上下文压缩（Micro + Full Compact）
- Skill 按需加载
- 持久化 Memory
- 子代理（SubAgent）
- 会话内 TODO 与跨会话 Task 图

---

## 项目结构

```text
learn-claude-code/
├── main.py                      # 主循环入口
├── configs/
│   ├── __init__.py
│   └── configs.py               # 模型、压缩阈值、权限规则等
├── models/
│   └── anthropic_client.py      # Anthropic 客户端封装（读取 DMX_* 环境变量）
├── modules/
│   ├── hook.py                  # HookManager
│   ├── memory.py                # MemoryManager / DreamConsolidator(骨架)
│   ├── permission.py            # PermissionManager + BashSecurityValidator
│   ├── prompt.py                # SystemPromptBuilder
│   ├── retry.py                 # 退避重试 backoff
│   ├── skill.py                 # SkillManager
│   └── task.py                  # TaskManager（落盘任务图）
├── tools/
│   ├── __init__.py              # 工具 schema 与 handler 注册
│   ├── common.py                # bash/read/write/edit 实现
│   ├── compact.py               # micro/full compact + transcript 持久化
│   ├── subagent.py              # SubAgent
│   ├── todo.py                  # TodoManager（会话内计划）
│   └── utils.py                 # 路径安全工具
├── utils/
│   └── messages.py              # 消息标准化与文本提取
├── skills/
│   ├── cards/SKILL.md
│   └── web_scraper/
│       ├── SKILL.md
│       └── scrape.py
├── .hooks.json                  # Hook 配置
├── .hooks/                      # 示例 hook 脚本
├── .memory/                     # 持久化记忆目录
├── .tasks/                      # 持久化任务目录
├── .task_outputs/tool-results/  # 超大工具输出落盘目录
├── .transcripts/                # full compact 前对话快照
├── pyproject.toml
└── README.md
```

---

## 快速开始

### 1) 环境要求

- Python >= 3.12
- 推荐使用 [uv](https://github.com/astral-sh/uv)

### 2) 安装依赖

```bash
uv sync
```

### 3) 配置环境变量

```bash
cp .env.example .env
```

当前客户端 `models/anthropic_client.py` 读取的是：
- `DMX_API_KEY`
- `DMX_BASE_URL`

如果缺失会在启动时报错。

### 4) （可选）信任工作区以启用 Hook

```bash
mkdir -p .claude
touch .claude/.claude_trusted
```

### 5) 启动

```bash
python main.py
```

---

## Agent Loop（main.py）

主循环的关键路径：

```text
user query
  -> micro_compact
  -> context size check (auto full compact if needed)
  -> client.messages.create(...)
  -> assistant tool_use?
      -> permission check
      -> pre hook
      -> run tool handler
      -> post hook
      -> append tool_result
  -> optional manual compact
  -> continue
```

几个关键细节：
- 每轮调用模型前都先做 `micro_compact`。
- 若估算上下文超限，触发 `compact_history`（自动全量压缩）。
- 若 API 返回 overlong_prompt，也会进入压缩恢复分支。
- `compact` 工具可主动触发一次手动全量压缩。

---

## 可用工具（tools/__init__.py）

### 子代理与主代理都可用
- `bash`
- `bash_readonly`
- `read_file`
- `write_file`
- `edit_file`
- `todo`

### 仅主代理可用
- `task`（启动子代理）
- `load_skill`
- `compact`
- `save_memory`
- `task_create`
- `task_update`
- `task_list`
- `task_get`

---

## 权限系统（modules/permission.py）

决策流程：

```text
bash 安全校验 -> deny 规则 -> mode 检查 -> allow 规则 -> ask 用户
```

### Bash 安全校验
内置正则检测：
- shell 元字符
- `sudo`
- `rm` 递归删除模式
- 命令替换 `$()`
- IFS 注入

其中高风险（如 `sudo`、`rm` 递归）会直接 `deny`。

### 模式
- `default`：按规则决策
- `plan`：拒绝写操作，只允许只读工具
- `auto`：只读工具自动放行，其他仍可能询问

---

## Hook 机制（modules/hook.py）

支持事件：
- `SessionStart`
- `PreToolUse`
- `PostToolUse`

配置文件：`.hooks.json`

Hook 通过环境变量获取上下文（如 `HOOK_TOOL_NAME`、`HOOK_TOOL_INPUT`）。
返回码语义：
- `0`：正常
- `1`：阻断工具执行
- `2`：向上下文注入提示消息

默认仅在工作区可信（存在 `.claude/.claude_trusted`）时启用。

---

## 上下文压缩（tools/compact.py）

### 1) Micro Compact
- 扫描历史 `tool_result`
- 仅保留最近 `KEEP_RECENT_TOOL_RESULTS` 条完整输出
- 更早且较长输出替换为占位文本

### 2) Full Compact
触发条件：
- 循环开头估算超限
- overlong_prompt 恢复分支
- 工具 `compact` 主动触发

流程：
1. 原始消息先写入 `.transcripts/transcript_*.jsonl`
2. 调模型生成可继续工作的摘要
3. 用一条带摘要的 user message 替换历史
4. 记录 `CompactState.last_summary`

### 大输出落盘
当工具输出超过 `PERSIST_THRESHOLD`：
- 完整输出写入 `.task_outputs/tool-results/<tool_use_id>.txt`
- 消息中只保留预览片段（`PREVIEW_CHARS`）

---

## Skill 系统（modules/skill.py）

- 启动时扫描 `skills/**/SKILL.md`
- 读取 frontmatter 中的 `name` / `description`
- `load_skill` 工具按名称返回完整 skill 文档

技能文档格式示例：

```markdown
---
name: my_skill
description: 一句话描述用途
---

# Skill Title
...
```

---

## Memory 系统（modules/memory.py）

- `save_memory` 工具把记忆存为 `.memory/*.md`（frontmatter + content）
- 自动重建 `.memory/MEMORY.md` 索引
- `SystemPromptBuilder` 会把记忆注入系统提示词

记忆类型：
- `user`
- `feedback`
- `project`
- `reference`

`DreamConsolidator` 当前是教学骨架，流程存在但未接入真实 LLM 整理逻辑。

---

## 任务管理：Todo vs Task

### Todo（会话内）
`tools/todo.py`：轻量计划列表，约束：
- 最多 12 项
- 最多 1 项 `in_progress`
- 多轮不更新会自动提醒

### Task（跨会话落盘）
`modules/task.py` + `task_*` 工具：
- 任务持久化到 `.tasks/task_<id>.json`
- 支持状态、owner、依赖关系（blockedBy / blocks）

---

## 子代理（tools/subagent.py）

`task` 工具会启动子代理：
- 子代理使用独立对话上下文
- 共享同一工作目录
- 最多 30 轮工具循环
- 最终只把摘要文本返回主代理

---

## Prompt 组装（modules/prompt.py）

`SystemPromptBuilder` 会按模块拼接：
- core instruction
- tool 列表
- skill 列表
- memory 内容
- 可能存在的 `CLAUDE.md` 指令
- dynamic context（日期、目录、模型、平台）

并使用 `DYNAMIC_BOUNDARY` 作为静态/动态分隔。

---

## 常用命令

启动后在交互中可使用：
- `/help`
- `/prompt`
- `/sections`
- `/skills`
- `q` / `exit`

---

## 关键配置（configs/configs.py）

- `MODEL` / `SUBAGENT_MODEL`
- `CONTEXT_LIMIT`
- `KEEP_RECENT_TOOL_RESULTS`
- `PERSIST_TOOL_RESULT`
- `PERSIST_THRESHOLD`
- `PREVIEW_CHARS`
- `DEFAULT_RULES`
- `MAX_RECOVERY_ATTEMPTS`

---

## 依赖

见 `pyproject.toml`，核心包括：
- `anthropic`
- `colorama`
- `beautifulsoup4`
- `markdownify`

---

## License

MIT
