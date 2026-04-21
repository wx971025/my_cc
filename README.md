# learn-claude-code

一个用于学习和演进的 Python Coding Agent 项目。  
核心目标不是“功能最全”，而是把现代 Agent 的关键控制面拆成可读、可改、可验证的模块。

当前版本已经覆盖：
- 主循环（对话 -> tool_use -> tool_result 回流）
- 权限闸门与 Hook
- 上下文压缩与大输出落盘
- Skill / Memory / Task / Cron
- 多 Agent 团队协作（派单 + 任务板认领）
- Worktree 隔离执行车道
- Merge Queue（Lead 统一评审/集成）
- MCP 外部工具接入（stdio，统一工具池）

---

## 1. 快速开始

### 1.1 环境要求
- Python 3.12+
- 推荐 `uv`

### 1.2 安装依赖
```bash
uv sync
```

### 1.3 配置环境变量
```bash
cp .env.example .env
```

`models/anthropic_client.py` 读取：
- `DMX_API_KEY`
- `DMX_BASE_URL`

### 1.4 初始化工作区信任（可选，但建议）
```bash
mkdir -p .claude
touch .claude/.claude_trusted
```

### 1.5 启动
```bash
python main.py
```

---

## 2. 核心架构总览

### 2.1 主循环（`main.py`）
每轮循环做的事：
1. `micro_compact` 微压缩
2. 注入队友事件（`teammate_manager.poll_events`）
3. 注入 cron 通知
4. 调模型（带工具定义）
5. 若有 `tool_use`：权限检查 -> hooks -> handler 执行 -> 回写 `tool_result`
6. 必要时 full compact（自动或手动）

### 2.2 统一控制面
无论工具来自哪里（本地 / MCP）都走同一条路径：
- 同一个 `TOOLS` 工具池
- 同一个 `TOOL_HANDLERS` 路由
- 同一个权限系统（`PermissionManager`）
- 同一个 Hook 管道（`PreToolUse` / `PostToolUse`）
- 同一个 `tool_result` 回流

这点是工程上最关键的稳定性来源。

---

## 3. 目录结构（重点）

```text
learn-claude-code/
├── main.py
├── configs/
├── models/
├── modules/
│   ├── permission.py
│   ├── hook.py
│   ├── prompt.py
│   ├── retry.py
│   ├── skill.py
│   ├── memory.py
│   ├── task.py
│   ├── taskBoard.py
│   ├── teammate.py
│   ├── worktree.py
│   ├── mergeQueue.py
│   └── mcp/
│       ├── __init__.py
│       ├── client.py
│       └── registry.py
├── tools/
│   ├── __init__.py
│   ├── common.py
│   ├── compact.py
│   └── subagent.py
├── mcp_servers/
│   └── echo_server.py
├── .mcp.json
├── .hooks.json
├── .memory/
├── .tasks/
├── .team/
├── .worktrees/
└── README.md
```

---

## 4. 工具体系

工具注册在 `tools/__init__.py`，分三层：

- **子代理与主代理共享工具**：`bash`、`read_file`、`write_file`、`edit_file`、`todo` 等
- **主代理专用工具**：任务管理、团队管理、任务板、worktree、merge queue 等
- **MCP 动态工具**：启动时读取 `.mcp.json`，自动注入命名为  
  `mcp__<server_alias>__<tool_name>`

---

## 5. 多 Agent 协作（推荐流程）

### 5.1 角色
- **Lead Agent**：派单、收集结果、评审并集成改动
- **Worker Agent**：执行任务，必要时使用 worktree 车道隔离改动

### 5.2 两种派单方式
- 直投：`assign_task`（指定某个 teammate）
- 任务板：`board_post_task`（任意符合角色的 worker 自主认领）

### 5.3 结果回传
worker 完成后写 `.team/results/*.md`，lead 在下一轮 `poll_events` 收到事件，再按需 `read_file` 查看完整正文。

---

## 6. Worktree 隔离 + Merge Queue

### 6.1 Worktree（`modules/worktree.py`）
每条板任务可绑定独立车道（`wt/<name>` + `.worktrees/<name>`），降低并行污染风险。

关键动作：
- `create` / `bind` / `enter` / `closeout`
- dirty 检查：`remove` 前若有未提交改动会降级为 `keep`
- 生命周期记录在 `.worktrees/index.json` 和 `.worktrees/events.jsonl`

### 6.2 Merge Queue（`modules/mergeQueue.py`）
worker 在车道里提交后，向 merge queue 登记“待评审 patch”；lead 决策：
- `merge`：合并进主分支（串行锁保护），成功后可完结任务
- `reject`：标记拒绝，分支保留供复盘

可用工具：
- `merge_queue_list`
- `merge_review`
- `merge_integrate`

---

## 7. MCP 外部能力接入

### 7.1 配置文件（`.mcp.json`）
最小示例：

```json
{
  "mcpServers": {
    "demo": {
      "command": "python",
      "args": ["-u", "mcp_servers/echo_server.py"]
    }
  }
}
```

### 7.2 启动流程
`tools/__init__.py` 在 import 末尾调用 `mcp_registry.bootstrap()`：
1. 读取 `.mcp.json`
2. `subprocess.Popen` 拉起每个 MCP server
3. `initialize` + `tools/list`
4. 把远端工具注入主工具池

### 7.3 调试命令
- 交互内输入 `/mcp` 查看 server 状态
- 工具 `mcp_status` 也可输出同样信息

### 7.4 教学边界
当前实现聚焦 `tools-first`：
- 仅 stdio transport
- 覆盖 `initialize` / `tools/list` / `tools/call`
- 未展开 resources/prompts/auth/elicitation（可后续扩展）

---

## 8. 权限、Hook、压缩

### 8.1 权限系统（`modules/permission.py`）
决策顺序：
`bash 安全校验 -> deny 规则 -> mode 检查 -> allow 规则 -> ask_user`

模式：
- `default`
- `plan`（禁写）
- `auto`（只读自动放行）

### 8.2 Hook（`modules/hook.py`）
支持：
- `SessionStart`
- `PreToolUse`
- `PostToolUse`

配置在 `.hooks.json`。  
返回码可用于阻断工具执行或注入提示。

### 8.3 压缩（`tools/compact.py`）
- Micro compact：压缩旧 `tool_result` 文本体积
- Full compact：上下文超限或手动触发 `compact`
- 大输出可落盘到 `.task_outputs/tool-results/`

---

## 9. 常用交互命令

在 `python main.py` 交互中可用：
- `/help`
- `/prompt`
- `/sections`
- `/skills`
- `/team`
- `/inbox`
- `/cron`
- `/test`
- `/mcp`
- `q` / `exit`

---

## 10. 典型实操片段

### 10.1 启动团队并挂板
```text
spawn_teammate(name="alice", role="backend")
spawn_teammate(name="bob", role="frontend")
board_post_task(subject="refactor cron retry", claim_role="backend")
board_post_task(subject="landing hero section", claim_role="frontend")
```

### 10.2 Lead 处理 merge queue
```text
merge_queue_list
merge_review(task_id=7)
merge_integrate(task_id=7, strategy="merge")
```

### 10.3 查看 MCP 状态
```text
/mcp
```

---


## License

MIT
