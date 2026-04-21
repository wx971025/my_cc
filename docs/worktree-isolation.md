# Worktree 隔离机制

> 读完本文你应该能答上来：
> 1. 为什么任务板解决不了并行改同一个文件的问题？
> 2. 一条板任务从认领到结束，经过哪些文件 / 目录 / 状态字段？
> 3. worker 什么时候"进"车道、什么时候"出"车道？收尾时"保留"和"删除"怎么选？
> 4. git 不支持 `worktree` 时会怎么样？

相关代码：`modules/worktree.py` · `modules/teammate.py::_handle_board_task` · `modules/taskBoard.py` · `modules/task.py` · `tools/common.py`

---

## 1. 为什么要隔离

已经落地的 [任务板 + 认领机制](./multiagent-dataflow.md) 让多个同事 Agent 并行干活，但**所有 worker 共享同一个工作目录**。很快会出现三件事：

- 两个任务同时改同一个文件，互相覆盖
- A 任务做到一半，B 任务的改动已经把目录污染了
- 事后想单独回看 `#7` 任务改了什么，无法区分

任务板回答的是"做什么"，没有回答"**在哪做**"。worktree 隔离要做的就是后者：

> **每条被认领的板任务，自动在 `.worktrees/<name>/` 开一条独立的 git worktree，worker 所有文件操作都限制在这个目录里。**

---

## 2. 全景：两张表 + 一份事件日志 + 隔离目录

```mermaid
flowchart LR
    subgraph MA["主 Agent 线程"]
        direction TB
        PostTask["board_post_task()"]
        Poll["poll_events('lead')"]
        WT_Tools["worktree_list / info / closeout<br/>（人工审计 / 强制回收）"]
    end

    subgraph FS["磁盘产物"]
        direction TB
        subgraph Tasks[".tasks/"]
            TaskFile[("task_&lt;id&gt;.json<br/>status / owner<br/>worktree / worktree_state<br/>last_worktree / closeout")]
        end
        subgraph WT[".worktrees/"]
            Index[("index.json<br/>WorktreeRecord 列表")]
            Events[("events.jsonl<br/>生命周期审计")]
            Dirs[("&lt;name&gt;/<br/>真实隔离目录<br/>被 gitignored")]
        end
        TeamFS[(".team/<br/>inbox / pending / results")]
    end

    subgraph AW["alice worker 线程"]
        direction TB
        Claim["_try_claim_board_task()"]
        Handle["_handle_board_task()"]
        Exec["_exec_tool(cwd=_active_base)"]
        Pub["_publish_result()"]
    end

    subgraph WM["WorktreeManager"]
        direction TB
        Create["create(name, task_id)"]
        Bind["bind(task_id, name)"]
        Enter["enter(name)"]
        Closeout["closeout(name, action)"]
    end

    PostTask --> TaskFile
    Claim --> TaskFile
    Handle --> Create --> Dirs
    Create --> Index
    Create --> Events
    Handle --> Bind --> TaskFile
    Handle --> Enter --> Index
    Handle --> Exec
    Exec -->|bash/read/write/edit| Dirs
    Handle --> Closeout --> Index
    Closeout --> Events
    Closeout --> TaskFile
    Pub --> TeamFS
    Poll --> TeamFS
    WT_Tools --> Index
    WT_Tools --> Closeout

    classDef file fill:#fef3c7,stroke:#d97706
    class TaskFile,Index,Events,Dirs,TeamFS file
```

**两张表通过 `task_id` 一对一串联：**

```jsonc
// .tasks/task_7.json
{
  "id": 7,
  "subject": "refactor cron.py",
  "status": "in_progress",
  "owner": "alice",
  "worktree": "t7-alice",
  "worktree_state": "active",
  "last_worktree": "t7-alice",
  "closeout": null
}

// .worktrees/index.json
{
  "worktrees": [
    {
      "name": "t7-alice",
      "path": ".worktrees/t7-alice",
      "branch": "wt/t7-alice",
      "task_id": 7,
      "status": "active",
      "last_entered_at": 1776000000.0,
      "last_command_at": null,
      "last_command_preview": null,
      "closeout": null
    }
  ]
}
```

### 为什么 `status` 和 `worktree_state` 必须分开

不是同一层东西：

| 字段 | 取值 | 回答的问题 |
|---|---|---|
| `task.status` | pending / in_progress / completed / deleted | 这件工作做到哪一步 |
| `task.worktree_state` | unbound / active / kept / removed | 这条执行车道现在是不是还活着 |

现实中"任务已 completed、worktree 还保留给 reviewer 看"非常常见，一个字段表达不了。

### 两层状态机

`task.status`：

```mermaid
stateDiagram-v2
    [*] --> pending: board_post_task
    pending --> in_progress: claim_task / set_worktree
    in_progress --> completed: complete_task
    in_progress --> pending: （理论上，实际无回退）
    pending --> deleted
    in_progress --> deleted
```

`task.worktree_state`：

```mermaid
stateDiagram-v2
    [*] --> unbound: task_create
    unbound --> active: wt.bind
    active --> kept: closeout keep / dirty 降级
    active --> removed: closeout remove 成功
    kept --> removed: 手动 worktree_closeout remove
    removed --> [*]
    note right of kept
        目录仍在磁盘
        可供 reviewer 查看
    end note
    note right of removed
        git worktree remove 已跑
        last_worktree 仍保留审计轨迹
    end note
```

### 为什么还需要 `events.jsonl`

worktree 生命周期跨很多步，**只看当前 status 排查不了为什么会变成 kept**。`events.jsonl` 每行一条记录，含 `event / ts / task_id / worktree / reason`，是这条审计链的完整留痕。

---

## 3. 完整时序：从挂板到结果回传

以"主 Agent 挂一条 backend 任务、alice 认领"为例。

```mermaid
sequenceDiagram
    autonumber
    participant L as 主 Agent (main)
    participant B as TaskBoard / Bus
    participant W as alice worker
    participant M as WorktreeManager
    participant G as git + 磁盘

    L->>B: board_post_task(subject, claim_role)
    B->>B: task_manager.create() → .tasks/task_7.json
    B->>W: inbox += board_notify（唤醒）
    Note over L: 主线程继续（非阻塞）<br/>可以继续派别的任务

    W->>B: read_inbox() → [board_notify]
    W->>B: _try_claim_board_task()
    B->>B: scan_unclaimed("backend") → [#7]
    B->>B: claim_task(#7, "alice")<br/>task.status=in_progress<br/>task.owner=alice

    W->>M: wt.create("t7-alice", 7)
    M->>G: git worktree add -b wt/t7-alice .worktrees/t7-alice HEAD
    G-->>M: ok
    M->>G: index.json += record
    M->>G: events.jsonl += worktree.create

    W->>M: wt.bind(7, "t7-alice")
    M->>G: task.worktree=t7-alice<br/>task.worktree_state=active<br/>task.last_worktree=t7-alice

    W->>M: wt.enter("t7-alice")
    M->>G: events.jsonl += worktree.enter
    W->>W: self._active_base = .worktrees/t7-alice

    loop 子 Agent Loop（_run_task）
        W->>W: model.messages.create()
        W->>W: _exec_tool(cwd/base=_active_base)
        W->>G: 改动只落 .worktrees/t7-alice/<br/>主 WORKDIR 不受污染
    end

    W->>W: _active_base = None
    W->>M: wt.closeout("t7-alice", action=remove, complete_task=True)
    M->>G: git status --porcelain（dirty check）
    alt clean
        M->>G: git worktree remove .worktrees/t7-alice
        G-->>M: ok → status=removed
    else dirty
        M->>M: 降级 action=keep<br/>reason="dirty: ..."<br/>status=kept
    else remove 失败
        M->>M: 降级 action=keep<br/>reason="remove-failed: ..."<br/>status=kept
    end
    M->>G: index.json 写回<br/>task.worktree_state + task.closeout<br/>task.status=completed
    M->>G: events.jsonl += worktree.closeout.{remove,keep}

    W->>B: _publish_result(lead, "board-7", result)
    B->>G: results/board-7.md
    B->>L: inbox[lead] += task_result_available

    Note over L: 下一次 agent_loop 顶部
    L->>B: poll_events("lead")
    B-->>L: [task_result_available]
    L->>L: 事件注入 messages，喂给模型
```

**关键时刻：**

| 步骤 | 动作 | 为什么重要 |
|---|---|---|
| 7-10 | `wt.create + bind + enter` | 隔离的门槛。过了这一步 worker 的所有写入都出不了 `.worktrees/t7-alice/` |
| 12-14 | `_exec_tool(cwd/base=_active_base)` | 隔离真正落地的地方。子进程 cwd 和 `safe_path_at` 的 base 同时被设成车道目录 |
| 16-23 | `wt.closeout` | 对应 s18 原文强调的"显式 closeout 决策"。无论 clean / dirty / remove-failed，都会落 `closeout = {action, reason, at}` 和对应的 event |
| 24-26 | `_publish_result` | 和邮箱派发任务**共用同一条回传管道**。主 Agent `poll_events` 视角看不出任务来源，`request_id=board-*` 前缀是唯一标识 |

---

## 4. 隔离是怎么落到每一次 `bash` 上的

关键在 `_TeammateWorker._exec_tool` 这一层路由：

```python
base = self._active_base   # Path or None

if tool_name == "bash":
    return run_bash(args["command"], cwd=base)
if tool_name == "write_file":
    return run_write(args["path"], args["content"], base=base)
if tool_name == "edit_file":
    return run_edit(args["path"], args["old_text"], args["new_text"], base=base)
if tool_name == "read_file":
    return run_read(path=args["path"], base=base)
```

```mermaid
flowchart TD
    Tool[worker 收到 tool_use 块]
    Route["_exec_tool(name, args)"]
    Check{self._active_base?}
    Old["run_bash(cmd)<br/>cwd=os.getcwd()<br/>safe_path(WORKDIR/p)"]
    New["run_bash(cmd, cwd=base)<br/>safe_path_at(p, base)"]
    MainDir[("主 WORKDIR<br/>（和老行为一致）")]
    LaneDir[(".worktrees/t7-alice/<br/>隔离沙箱")]

    Tool --> Route --> Check
    Check -- None<br/>（邮箱任务 / git 不可用） --> Old --> MainDir
    Check -- Path<br/>（板任务 + git 可用） --> New --> LaneDir

    classDef safe fill:#dcfce7,stroke:#16a34a
    classDef shared fill:#fee2e2,stroke:#dc2626
    class LaneDir safe
    class MainDir shared
```

**两条不变量**：

- `_active_base=None` → 完全退化到老行为，直投邮箱任务、不走板任务的 worker 都不受影响
- `_active_base=Path` → `run_bash(cwd=...)` 让子进程 cwd 是车道目录；`safe_path_at(p, base)` 把相对路径相对 base 解析，同时保留 WORKDIR 沙箱（车道目录本身也在 WORKDIR 下，不会触发逸出检测）

这也是为什么 worker 里**所有**落地动作必须走这 4 个 runner —— 直接 `open()` 绕过就废了隔离。

---

## 5. closeout 的决策树

```mermaid
flowchart TD
    Start([_handle_board_task 调用<br/>closeout action=remove<br/>complete_task=True])
    Dirty{git status --porcelain<br/>在车道目录里<br/>有未提交改动？}
    RemoveTry[git worktree remove 目录]
    RemoveOK{成功？}
    DegradeDirty[降级: action=keep<br/>reason='dirty: ...']
    DegradeFail[降级: action=keep<br/>reason='remove-failed: ...']
    Removed[status=removed<br/>目录消失]
    Kept[status=kept<br/>目录保留]
    Writeback["写回三处:<br/>① index.json[rec]<br/>② task.worktree_state + task.closeout<br/>③ 若 complete_task=True<br/>&nbsp;&nbsp;&nbsp;task.status=completed"]
    Event[events.jsonl +=<br/>worktree.closeout.remove<br/>或 worktree.closeout.keep]

    Start --> Dirty
    Dirty -- 有 --> DegradeDirty --> Kept
    Dirty -- 无 --> RemoveTry --> RemoveOK
    RemoveOK -- 是 --> Removed
    RemoveOK -- 否 --> DegradeFail --> Kept
    Removed --> Writeback
    Kept --> Writeback
    Writeback --> Event

    classDef ok fill:#dcfce7,stroke:#16a34a
    classDef degrade fill:#fef3c7,stroke:#d97706
    class Removed ok
    class DegradeDirty,DegradeFail degrade
```

**三条路径共同的收尾写回：**

1. `index.json[rec].status` = `removed` / `kept`
2. `index.json[rec].closeout` = `{action, reason, at}`
3. `task.worktree_state` = `removed` / `kept`
4. `task.worktree` = `""`（remove 时清空，keep 时保留）
5. `task.last_worktree` = `<name>`（**两条路径都保留**，便于事后审计）
6. `task.closeout` = `{...}`（镜像一份到任务记录）
7. `complete_task=True` 时 `task.status = completed`
8. 追加一行事件 `worktree.closeout.remove` 或 `worktree.closeout.keep`

---

## 6. 降级策略：git 不可用时会怎样

`WorktreeManager.__init__` 启动时先跑一次 `_probe_git`：

```mermaid
flowchart TD
    Init[WorktreeManager 启动]
    P1["git rev-parse --is-inside-work-tree<br/>返回 'true'?"]
    P2["git worktree list<br/>返回码 0?"]
    Enabled[disabled=False]
    Disabled[disabled=True]
    Normal[正常路径<br/>create/enter/run/closeout<br/>都可用]
    Fallback[禁用路径<br/>create 等方法立即返回<br/>Error: git not available]
    Worker["_handle_board_task<br/>检测 ack 是否以 'Created' 开头"]
    LaneMode[进入隔离模式<br/>_active_base = 车道目录]
    MainMode[Fallback 回主 WORKDIR<br/>_active_base = None<br/>事件日志/index.json 不写]

    Init --> P1
    P1 -- 否 --> Disabled
    P1 -- 是 --> P2
    P2 -- 否 --> Disabled
    P2 -- 是 --> Enabled
    Enabled --> Normal
    Disabled --> Fallback
    Normal --> Worker
    Fallback --> Worker
    Worker -- "ack 以 Created 开头" --> LaneMode
    Worker -- 其它 --> MainMode

    classDef bad fill:#fef3c7,stroke:#d97706
    classDef good fill:#dcfce7,stroke:#16a34a
    class Disabled,Fallback,MainMode bad
    class Enabled,Normal,LaneMode good
```

两个检测都必须通过才算"能用"。光第一条不够：**CentOS 7 自带的 git 1.8.3.1 认得仓库但不认识 `worktree` 子命令**（该功能 git 2.5 / 2015 年才引入）。

`disabled=True` 的 worker 行为：`_handle_board_task` 检测到 `create` 返回 `Error:` 开头，打印 `worktree unavailable ...; fallback to main WORKDIR`，`_active_base` 保持 `None`，任务照常完成，**对外表现和没加 worktree 层时一模一样**。已在 CentOS 7 环境上验证过。

---

## 7. 主 Agent 可用的 worktree 工具

大部分时候不需要手动介入 —— worker 自动开、自动收。遇到特殊情况有三个工具：

| 工具 | 作用 |
|---|---|
| `worktree_list` | 列所有车道（含 removed 的历史）及 `task_id`、`branch`、`status` |
| `worktree_info(name)` | 看单条详情：`last_entered_at` / `last_command_preview` / `closeout` |
| `worktree_closeout(name, action, reason?, complete_task?)` | 人工收尾：想保留 review 就 `keep`，想强制回收就 `remove`。dirty 检测和降级逻辑对手动调用同样生效 |

---

## 8. 初学者最常踩的坑

```mermaid
mindmap
  root((Worktree 易踩坑))
    任务记录丢字段
      task.worktree 为空
      看不出在哪做
      解法 set_worktree 两表同步
    工具绕过 runner
      直接 open 路径
      隔离形同虚设
      解法 所有文件工具走 _exec_tool
    只会 remove
      不懂 closeout 的意义
      没有 reason
      解法 closeout 字段 + 事件日志
    删前不查 dirty
      误删未提交改动
      解法 closeout 内 git status
    没有事件日志
      事后无法排查
      解法 events.jsonl 每步一行
    kept 当垃圾堆
      越堆越多
      解法 默认 remove
      必要时加 cron 清理
```

---

## 9. 快速复现（git ≥ 2.5 环境）

```python
# 主 Agent 交互：
spawn_teammate(name="alice", role="backend")
board_post_task(subject="refactor cron.py", claim_role="backend")
board_post_task(subject="add hero section", claim_role="frontend")
# 等一会儿
worktree_list
list_pending_tasks
task_list
```

预期观察顺序：

```mermaid
timeline
    title alice 处理一条 backend 板任务
    t0 : 主 Agent board_post_task × 2
       : 挂板 + 唤醒
    t1 : alice inbox 有 board_notify
       : 扫板 → claim #1
       : frontend 的任务跳过
    t2 : 车道打开
       : .worktrees/t1-alice/
       : worktree_list 显示 [active]
    t3 : 车道内跑 bash / write_file
       : 改动只落车道目录
       : 主 WORKDIR 不受污染
    t4 : 任务完成
       : closeout action=remove 成功
       : 磁盘上目录消失
    t5 : worktree_list 显示 [removed]
       : task #1 status=completed
       : last_worktree 保留审计轨迹
    t6 : 主 Agent poll_events 收到事件
       : request_id=board-1
       : summary 注入下一轮 context
```

如果任务在车道里没 commit 留了脏改动：
- 目录被保留（`status=kept`、`closeout.reason` 前缀 `dirty:`）
- 主 Agent 可以 `worktree_info(name="t1-alice")` 看 `last_command_preview` 帮助排查
- 想强制清理要先 commit/stash，再 `worktree_closeout(name="t1-alice", action="remove")`

---

## 10. 这层在整套 multi-agent 里处在哪

```mermaid
flowchart LR
    s15["s15 团队<br/>spawn / dispatch"]
    s17["s17 任务板 + 认领"]
    s18["<b>s18 Worktree 隔离（本文）</b>"]
    s19["s19 MCP"]

    s15 -->|直投邮箱通道| s17
    s17 -->|谁领什么| s18
    s18 -->|每条任务独立车道| s19

    s15 -.说明.-> S15D["回答：派单通道"]
    s17 -.说明.-> S17D["回答：做什么 / 谁做"]
    s18 -.说明.-> S18D["回答：在哪做 / 互不踩"]
    s19 -.说明.-> S19D["回答：外部能力接入"]

    classDef cur fill:#fef3c7,stroke:#d97706,stroke-width:2px
    class s18 cur
```

任务板解决了"活怎么分下去"，worktree 给这些并行活划了独立车道。有了这两层，一组 agent 并行改同一个仓库才成为可能。

---

## 附：关键字段速查

| 字段位置 | 字段名 | 含义 |
|---|---|---|
| `task_<id>.json` | `worktree` | 当前绑定的车道名；remove 后置空 |
| `task_<id>.json` | `worktree_state` | `unbound` / `active` / `kept` / `removed` |
| `task_<id>.json` | `last_worktree` | 最近一次用过的车道名；remove 后仍保留 |
| `task_<id>.json` | `closeout` | `{action, reason, at}`，镜像自 WorktreeRecord |
| `index.json[*]` | `name` | 车道短名，约定 `t<task_id>-<worker_name>` |
| `index.json[*]` | `path` | 相对 WORKDIR 的目录路径 |
| `index.json[*]` | `branch` | 对应 git 分支，约定 `wt/<name>` |
| `index.json[*]` | `status` | `active` / `kept` / `removed` |
| `index.json[*]` | `last_entered_at` | worker 切入车道的时间戳 |
| `index.json[*]` | `last_command_at` | 车道里最近一次执行命令的时间戳 |
| `index.json[*]` | `last_command_preview` | 最近一条命令的前 120 字 |
| `events.jsonl` | `event` | `worktree.create` / `.enter` / `.run` / `.closeout.remove` / `.closeout.keep` / `.create_failed` |
