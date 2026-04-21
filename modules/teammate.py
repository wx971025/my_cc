"""
Teammate orchestration.

============================================================================
一、总览
============================================================================
本模块把「主 Agent 派活 → 同事 Agent 干活 → 结果回传」落到
  文件 jsonl + threading.Event + 磁盘产物  这一套组合上。

从角色划分看，代码分成三段，**互不混写**：

    1) 共享基础层
         - 路径常量 / config.json 读写 / 成员注册  (_ensure_dirs / _load_config / _set_status)
         - 消息总线 MessageBus                   (双方通信都走它)

    2) 【同事 Agent 侧】 _TeammateWorker
         - 每个 spawn 创建一个实例，跑在自己的 daemon 线程里
         - 职责：idle 等单 → 接到 assign_task → 跑子 Agent Loop → 写结果文件 → 回 idle
         - 主循环 run() 的阻塞点有两处，下面详述

    3) 【主 Agent 侧】 TeammateManager
         - 在主线程被 main.py 调用，**全部方法都是非阻塞的**
         - 职责：spawn / dispatch / poll_events / shutdown / 视图方法
         - 主 Agent 本身的阻塞点不在这里（在 main.py 的 input() 和 client.messages.create）

============================================================================
二、目录布局 (都在 WORKDIR 下)
============================================================================
    .team/
        config.json                  成员注册表 (name / role / status)
        inbox/<name>.jsonl           每个人的收件箱，一行一条 json
        pending/<rid>.json           主 Agent 派发登记 (原文 + 状态)
        results/<rid>.md             同事 Agent 产出 (frontmatter + 正文)
        results/processed/<rid>.md   主 Agent 感知并注入过的结果归档

============================================================================
三、阻塞点速查
============================================================================
    同事 Agent 侧 (_TeammateWorker.run):
        ┌─ 空闲阶段: message_bus.wait_for_inbox(self.name, timeout=WORKER_IDLE_TIMEOUT)
        │    靠 threading.Event 唤醒，不是忙轮询；timeout 仅用于周期性检查 stop_event
        └─ 执行阶段: client.messages.create(...)
             网络阻塞；此时 shutdown 信号要等这次调用返回后才能生效

    主 Agent 侧 (TeammateManager):
        无。spawn / dispatch / poll_events / shutdown 都是"立即返回"。
        主 Agent 的真正阻塞在 main.py：
          - input(...)              等用户输入
          - client.messages.create  等模型回包

============================================================================
四、一次完整派单时序
============================================================================
    主Agent(main thread)    pending/       inbox/           worker thread       results/
        │ spawn(n,r)                                          ▲ 启动
        │─────────────────────────────────────────────────────▶│ wait_for_inbox 阻塞
        │
        │ dispatch(n, task)
        │──▶ pending/rid.json    assign_task(rid)
        │────────────────────▶ inbox/n.jsonl ────────────────▶│ 被 Event 唤醒
        │    (非阻塞返回 rid)                                  │ _run_task():
        │                                                      │   调模型 + 工具 ...
        │                                                      │──▶ results/rid.md
        │                                                      │ 发 task_result_available
        │                         inbox/lead.jsonl ◀───────────│ (只带指针+摘要)
        │                                                      │ 回到 wait_for_inbox
        │
        │ poll_events("lead")
        │◀── 结构化事件列表 (结果指针 + 摘要)
        │    副作用: results/rid.md → results/processed/rid.md
        │            pending.status = acknowledged
"""
import json
import threading
import time
import uuid
from pathlib import Path

from models.anthropic_client import anthropic_client as client
from configs import WORKDIR, MODEL

# ===========================================================================
# 路径与常量
# ===========================================================================

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
PENDING_DIR = TEAM_DIR / "pending"
RESULTS_DIR = TEAM_DIR / "results"
CONFIG_PATH = TEAM_DIR / "config.json"
PROCESSED_DIR = RESULTS_DIR / "processed"


LEAD_NAME = "lead"
WORKER_MAX_TURNS = 50            # worker 执行单任务时子 Loop 的最大轮数（防死循环）
WORKER_IDLE_TIMEOUT = 5.0        # worker 空闲 wait_for_inbox 的超时秒数

VALID_MSG_TYPES = {
    # lead -> worker
    "assign_task",
    "shutdown_request",
    "plan_approval_response",
    "board_notify",                 # 任务板新增任务的通知（只为唤醒 worker，无需具体处理）
    # worker -> lead
    "task_result_available",
    "shutdown_response",
    "plan_approval",
    # 任一方向
    "message",
    "broadcast",
}


# ===========================================================================
# 共享基础层：目录 / 配置文件读写（worker 和 manager 都会用）
# ===========================================================================

# config.json 会被多个线程并发修改（worker 写自己的 status，manager 写新成员），
# 用一把模块级互斥锁护住 read-modify-write，避免互相覆盖。
_config_lock = threading.Lock()


def _ensure_dirs() -> None:
    TEAM_DIR.mkdir(exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def _load_config() -> dict:
    """读 .team/config.json；不存在或损坏都返回空模板。"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"team_name": "default", "members": []}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def _upsert_member(name: str, role: str, status: str) -> None:
    """新增或更新成员。整个 read-modify-write 上锁。"""
    with _config_lock:
        cfg = _load_config()
        for m in cfg["members"]:
            if m["name"] == name:
                m["role"] = role
                m["status"] = status
                break
        else:
            cfg["members"].append({"name": name, "role": role, "status": status})
        _save_config(cfg)


def _set_status(name: str, status: str) -> None:
    """仅更新 status 字段；成员不存在则忽略。"""
    with _config_lock:
        cfg = _load_config()
        for m in cfg["members"]:
            if m["name"] == name:
                m["status"] = status
                _save_config(cfg)
                return


# ===========================================================================
# 共享基础层：MessageBus（文件 jsonl + threading.Event 唤醒）
# ===========================================================================

class MessageBus:
    """
    轻量消息总线。双机制：

      - jsonl 文件   负责持久化（进程被 kill 也不丢消息）
      - Event 字典   负责唤醒（send 即 set，让 wait_for_inbox 立刻返回）

    假设：每个 inbox 只有一个消费者（worker 自己的 inbox 只有该 worker 消费；
    lead 的 inbox 只有主 Agent 消费）。所以 "read 全部 + 清空" 的非原子写法
    在本项目语境下是安全的。
    """

    def __init__(self, inbox_dir: Path = INBOX_DIR):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        # _events[name] 代表「name 这个收件人的唤醒信号」
        self._events: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()

    def _event_for(self, name: str) -> threading.Event:
        """按需懒创建某个 recipient 的 Event。"""
        with self._events_lock:
            evt = self._events.get(name)
            if evt is None:
                evt = threading.Event()
                self._events[name] = evt
            return evt

    # ---------- 写端：send / broadcast ----------

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict | None = None,
    ) -> str:
        """投一条消息到 recipient 的 jsonl，并唤醒其阻塞线程。"""
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"

        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)

        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        # 唤醒：若 recipient 当前阻塞在 wait_for_inbox，这里 set 会让它立刻返回
        self._event_for(to).set()
        return f"Sent {msg_type} to {to}"

    def broadcast(self, sender: str, content: str, teammates: list[str]) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"

    # ---------- 读端：read_inbox / wait_for_inbox ----------

    def read_inbox(self, name: str) -> list:
        """读光 inbox 并清空。返回 list[dict]。"""
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages: list[dict] = []
        for line in inbox_path.read_text().strip().splitlines():
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        inbox_path.write_text("")
        return messages

    def wait_for_inbox(self, name: str, timeout: float | None = None) -> bool:
        """
        【阻塞】阻塞当前线程直到 recipient 收到新消息，或超时。

        返回:
            True  被 send 唤醒
            False 超时
        注意 clear() 紧跟在 wait 后面：我们只关心「醒了」这个事实，
        具体消息让调用方下一轮自己 read_inbox 去取。
        """
        evt = self._event_for(name)
        triggered = evt.wait(timeout=timeout)
        evt.clear()
        return triggered


message_bus = MessageBus()


# ===========================================================================
# 【同事 Agent 侧】 _TeammateWorker
#
#     每个 spawn 产生一个实例 + 一个 daemon 线程。
#     实例方法只做 "worker 自己要做的事"：等单、跑任务、发结果、处理 shutdown。
#     下划线开头表示对外不可见；外界要启动/派单/停用都走 TeammateManager。
# ===========================================================================

class _TeammateWorker:
    """
    生命周期:
        idle ──(assign_task)──▶ working ──(完成)──▶ idle
          │
          └──(shutdown_request / stop_event)──▶ shutdown

    阻塞点（全部在 run() 这条线上）:
      ① idle:    message_bus.wait_for_inbox(self.name, timeout=WORKER_IDLE_TIMEOUT)
      ② working: client.messages.create(...)
    """

    def __init__(self, name: str, role: str, stop_event: threading.Event):
        self.name = name
        self.role = role
        self.stop_event = stop_event
        # _active_base=None → 工具在主 WORKDIR 里执行（和老行为一致）
        # _active_base=Path → 工具在 worktree 车道目录里执行（_handle_board_task 设置）
        self._active_base: Path | None = None
        self.sys_prompt = (
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            "You are a teammate worker. Wait for assign_task messages, "
            "then complete the task and produce a concise final answer. "
            "Use send_message if you need to ask the lead for clarification."
        )

    # -------------------------------------------------------------------
    # [Worker] 主循环：线程入口
    # -------------------------------------------------------------------

    def run(self) -> None:
        """
        线程入口。调度顺序:
            ① drain 自己 inbox → 处理 shutdown / assign_task
            ② inbox 里没可执行任务 → 扫任务板，尝试认领一条 pending
            ③ 都没有 → 阻塞在 wait_for_inbox（board_notify 也会把它唤醒）
        """
        print(f"[teammate:{self.name}] spawned, idle")

        while not self.stop_event.is_set():
            msgs = message_bus.read_inbox(self.name)

            # ① 处理 inbox 里的可执行消息
            handled_actionable = False
            shutdown_requested = False
            for msg in msgs:
                if self.stop_event.is_set():
                    break
                msg_type = msg.get("type")

                if msg_type == "shutdown_request":
                    self._handle_shutdown(msg)
                    shutdown_requested = True
                    break

                if msg_type == "assign_task":
                    self._handle_assign_task(msg)
                    handled_actionable = True
                    continue

                if msg_type == "board_notify":
                    # 纯唤醒信号：无 payload，本轮结尾会自然去扫任务板
                    continue

                # 其它类型（闲聊 / broadcast 等）本版本只记录，不处理
                print(f"[teammate:{self.name}] ignored msg type={msg_type}")

            if shutdown_requested:
                break
            if handled_actionable:
                continue

            # ② inbox 里没有可干的活 → 去任务板兜底领一条
            if self._try_claim_board_task():
                continue

            # ③ 彻底 idle → 阻塞等唤醒（新邮件 / board_notify / 超时）
            message_bus.wait_for_inbox(self.name, timeout=WORKER_IDLE_TIMEOUT)

        _set_status(self.name, "shutdown")
        print(f"[teammate:{self.name}] stopped")

    # -------------------------------------------------------------------
    # [Worker] 消息 handler
    # -------------------------------------------------------------------

    def _handle_shutdown(self, msg: dict) -> None:
        """收到 shutdown_request：回 ack + 标记 stop_event。"""
        sender = msg.get("from", LEAD_NAME)
        message_bus.send(
            self.name, sender,
            "Acknowledged shutdown.", "shutdown_response",
        )
        self.stop_event.set()

    def _handle_assign_task(self, msg: dict) -> None:
        """
        收到 assign_task 的完整处理流程：
            切 working -> 跑子 Loop -> 写结果 + 发事件 -> 切回 idle
        异常兜底：即便 _run_task 抛异常也要把结果落盘，否则 lead 永远看不到 ack。
        """
        request_id = msg.get("request_id") or f"r-{uuid.uuid4().hex[:8]}"
        task = msg.get("content", "")
        sender = msg.get("from", LEAD_NAME)

        print(f"[teammate:{self.name}] start task {request_id}")
        _set_status(self.name, "working")
        try:
            result = self._run_task(task)
        except Exception as e:
            result = f"(worker crashed: {e})"
        self._publish_result(sender, request_id, result)
        _set_status(self.name, "idle")
        print(f"[teammate:{self.name}] finished task {request_id}")

    # -------------------------------------------------------------------
    # [Worker] 任务板兜底：扫板 → 认领 → 执行 → 回传
    # -------------------------------------------------------------------

    def _try_claim_board_task(self) -> bool:
        """
        扫一次任务板，尝试认领一条和自己 role 匹配的 pending 任务。
        返回 True 表示"确实领到了并跑完了"，调用方应 continue 主循环。
        竞争语义：claim_task 内部加了 _claim_lock，两个 worker 抢同一条只会有一个成功。
        """
        from modules.taskBoard import scan_unclaimed_tasks, claim_task

        candidates = scan_unclaimed_tasks(self.role)
        if not candidates:
            return False

        picked = candidates[0]
        ack = claim_task(
            picked["id"], owner=self.name,
            role=self.role, source="self_claim",
        )
        if not ack.startswith("Claimed"):
            # 被别人抢走 / 状态已变；这轮放弃，下一轮再试
            return False

        self._handle_board_task(picked)
        return True

    def _handle_board_task(self, task: dict) -> None:
        """
        跑一条已认领的板上任务，完整生命周期：
            create_worktree → bind → enter → _run_task (cwd=车道)
                → closeout(remove/keep) → publish_result

        车道策略：
            · 能开成功 → self._active_base 指向车道目录，所有文件工具走隔离
            · 开失败 / git 不可用 → fallback 回主 WORKDIR（老行为），
              同时把降级原因记在事件里，便于事后排查
        任务状态：
            · 有车道 → 由 closeout(complete_task=True) 统一完结任务
            · 无车道 → 回退到直接 complete_task
        """
        from modules.taskBoard import complete_task
        from modules.worktree import worktree_manager as wt

        task_id = task["id"]
        request_id = f"board-{task_id}"
        wt_name = f"t{task_id}-{self.name}"

        # ---------- 开车道 ----------
        create_ack = wt.create(wt_name, task_id)
        wt_ok = create_ack.startswith("Created")
        if wt_ok:
            wt.bind(task_id, wt_name)
            wt.enter(wt_name)
            self._active_base = wt.path_of(wt_name)
            print(f"[teammate:{self.name}] worktree on: {self._active_base}")
        else:
            print(f"[teammate:{self.name}] worktree unavailable ({create_ack}); "
                  f"fallback to main WORKDIR")
            self._active_base = None

        # pending 登记（和邮箱派发的任务共用同一张表）
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        (PENDING_DIR / f"{request_id}.json").write_text(
            json.dumps({
                "request_id": request_id,
                "to": self.name,
                "from": LEAD_NAME,
                "task": f"[board #{task_id}] {task.get('subject', '')}",
                "status": "dispatched",
                "dispatched_at": time.time(),
                "source": "board_claim",
                "board_task_id": task_id,
                "worktree": wt_name if wt_ok else "",
            }, indent=2, ensure_ascii=False)
        )

        prompt = (
            f"[Board Task #{task_id}] {task.get('subject', '')}\n\n"
            f"{task.get('description', '')}"
        ).strip()

        # ---------- 跑任务 ----------
        print(f"[teammate:{self.name}] claimed board task #{task_id}")
        _set_status(self.name, "working")
        try:
            result = self._run_task(prompt)
        except Exception as e:
            result = f"(worker crashed: {e})"

        # ---------- 收尾 ----------
        # 先关车道隔离，避免后续 commit / _publish_result 被隔离沙箱影响
        self._active_base = None

        # 在车道里 commit，把 worker 改动固化成一个 patch 分支
        # 之所以放在 closeout 之前：commit 后工作区 clean，closeout(remove) 才不会
        # 被 dirty check 降级成 kept，车道目录可以被干净回收。
        mr_info: dict | None = None
        if wt_ok:
            commit_info = wt.commit_lane(
                wt_name,
                f"[board #{task_id}] {task.get('subject', '')}".strip(),
            )
            if commit_info.get("had_changes"):
                mr_info = {
                    "task_id": task_id,
                    "worktree": wt_name,
                    "branch": f"wt/{wt_name}",
                    "owner": self.name,
                    "subject": task.get("subject", ""),
                    "commit_sha": commit_info["commit_sha"],
                    "base_sha": commit_info["base_sha"],
                    "files_changed": commit_info["files_changed"],
                    "diff_stat": commit_info["diff_stat"],
                    "summary": (result or "")[:500],
                }
                from modules.mergeQueue import enqueue as _mq_enqueue
                _mq_enqueue(mr_info)
                print(
                    f"[teammate:{self.name}] queued MR for #{task_id} "
                    f"({len(commit_info['files_changed'])} files, "
                    f"sha={commit_info['commit_sha'][:8]})"
                )
            elif not commit_info.get("ok"):
                print(f"[teammate:{self.name}] commit_lane failed: "
                      f"{commit_info.get('error')}")

        # closeout 策略:
        #   · 进入隔离 + 有 patch → 只 remove 目录，任务留在 in_progress 等 lead merge
        #   · 进入隔离 + 无改动   → 老路径 remove + complete_task
        #   · 未进入隔离         → 改动已落主 WORKDIR，直接 complete_task
        if wt_ok:
            try:
                ack = wt.closeout(
                    wt_name,
                    action="remove",
                    reason="auto after board task",
                    complete_task=(mr_info is None),
                )
                print(f"[teammate:{self.name}] {ack}")
            except Exception as e:
                print(f"[teammate:{self.name}] closeout failed: {e}")
                # 兜底：没有挂 MR 的情况至少把任务标 completed
                if mr_info is None:
                    try:
                        complete_task(task_id, owner=self.name)
                    except Exception:
                        pass
        else:
            try:
                complete_task(task_id, owner=self.name)
            except Exception as e:
                print(f"[teammate:{self.name}] complete_task failed: {e}")

        # 把 MR 信息拼进回传正文，让 lead 在 poll_events 看到摘要就能决策
        if mr_info:
            files_preview = ", ".join(mr_info["files_changed"][:8]) or "(none)"
            if len(mr_info["files_changed"]) > 8:
                files_preview += f" (+{len(mr_info['files_changed']) - 8} more)"
            header = (
                f"[merge-request queued]\n"
                f"  task_id   : #{task_id}\n"
                f"  branch    : {mr_info['branch']}\n"
                f"  commit    : {mr_info['commit_sha'][:12]}\n"
                f"  files ({len(mr_info['files_changed'])}): {files_preview}\n"
                f"  diff_stat : {mr_info['diff_stat'] or '(n/a)'}\n"
                f"Next step: merge_queue_list / merge_review({task_id}) / "
                f"merge_integrate({task_id}, 'merge').\n\n"
                f"--- worker summary ---\n"
            )
            result = header + (result or "")

        self._publish_result(LEAD_NAME, request_id, result)
        _set_status(self.name, "idle")
        print(f"[teammate:{self.name}] finished board task #{task_id}")

    # -------------------------------------------------------------------
    # [Worker] 子 Agent Loop（执行单个任务）
    # -------------------------------------------------------------------

    def _run_task(self, task: str) -> str:
        """
        针对一个 task 跑一个"小 Agent Loop"，直到模型 stop_reason != tool_use。

        阻塞点: client.messages.create(...) —— 每一轮都阻塞等网络。
        每轮开头会清一次自己的 inbox：任务进行中 lead 追加的消息（比如澄清、取消）
        会被拼进 messages 让模型看到。
        """
        messages: list[dict] = [{"role": "user", "content": task}]
        tools = self._tools_schema()
        final_text = ""

        for _ in range(WORKER_MAX_TURNS):
            if self.stop_event.is_set():
                return "(task aborted by shutdown)"

            # 把任务进行中追加的消息并入 context
            extra_msgs = message_bus.read_inbox(self.name)
            for m in extra_msgs:
                messages.append({"role": "user", "content": json.dumps(m, ensure_ascii=False)})

            try:
                # —— 阻塞点 ② ——
                response = client.messages.create(
                    model=MODEL,
                    system=self.sys_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=8000,
                )
            except Exception as e:
                return f"Error calling model: {e}"

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                final_text = "".join(
                    getattr(b, "text", "") for b in response.content
                ).strip()
                break

            # 执行这一轮模型要调的所有工具，把 tool_result 作为 user 消息回投
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    output = self._exec_tool(block.name, block.input)
                    print(f"  [{self.name}] {block.name}: {str(output)[:120]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    })
            messages.append({"role": "user", "content": tool_results})

        return final_text or "(no final text)"

    def _tools_schema(self) -> list[dict]:
        """worker 子 Loop 可用的工具。刻意比主 Agent 少，职责明确。"""
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "old_text": {"type": "string"},
                                             "new_text": {"type": "string"}},
                              "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message",
             "description": "Send a message to another teammate (use to='lead' to ask the main agent).",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"},
                                             "msg_type": {"type": "string",
                                                          "enum": list(VALID_MSG_TYPES)}},
                              "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain your own inbox.",
             "input_schema": {"type": "object", "properties": {}}},
        ]

    def _exec_tool(self, tool_name: str, args: dict) -> str:
        """
        工具路由：按 tool_name dispatch 到实际实现。

        隔离语义：
            若 self._active_base 不为 None（即当前 worker 正在 worktree 车道里
            跑一条板任务），所有文件系统相关工具都以 _active_base 作为 cwd / base，
            保证改动不会逸出到主 WORKDIR。_active_base=None 时行为完全和老版一致。

        注意: `tools.common` 在函数体内 lazy import，不要放到模块顶层。
        因为 tools/__init__.py 会反过来 import 本模块，顶层 import 会循环引用。
        """
        from tools.common import run_bash, run_read, run_write, run_edit  # lazy

        base = self._active_base  # Path or None

        if tool_name == "bash":
            return run_bash(args["command"], cwd=base)
        if tool_name == "read_file":
            return run_read(path=args["path"], base=base)
        if tool_name == "write_file":
            return run_write(args["path"], args["content"], base=base)
        if tool_name == "edit_file":
            return run_edit(args["path"], args["old_text"], args["new_text"], base=base)
        if tool_name == "send_message":
            return message_bus.send(
                self.name, args["to"], args["content"],
                args.get("msg_type", "message"),
            )
        if tool_name == "read_inbox":
            return json.dumps(message_bus.read_inbox(self.name), indent=2, ensure_ascii=False)
        return f"Unknown tool: {tool_name}"

    # -------------------------------------------------------------------
    # [Worker] 发布结果
    # -------------------------------------------------------------------

    def _publish_result(self, to: str, request_id: str, result: str) -> None:
        """
        1) 把完整正文写到 .team/results/<rid>.md
        2) 更新 pending/<rid>.json 状态 -> result_ready
        3) 向 lead 投一条 task_result_available 事件（只带指针 + 摘要）

        只发指针的原因: 避免把大段结果塞进主 Agent 的下一轮 context。
        主 Agent 如需要完整内容，自己在必要时 read_file 拉取即可。
        """
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        result_path = RESULTS_DIR / f"{request_id}.md"

        non_empty = [line.strip() for line in result.splitlines() if line.strip()]
        summary = " ".join(non_empty[:3])[:200] or "(no summary)"

        frontmatter = (
            "---\n"
            f"request_id: {request_id}\n"
            f"from: {self.name}\n"
            f"to: {to}\n"
            f"summary: {summary}\n"
            "---\n\n"
        )
        result_path.write_text(frontmatter + result)

        pending_path = PENDING_DIR / f"{request_id}.json"
        if pending_path.exists():
            try:
                pending = json.loads(pending_path.read_text())
                pending["status"] = "result_ready"
                pending["completed_at"] = time.time()
                pending_path.write_text(json.dumps(pending, indent=2, ensure_ascii=False))
            except Exception:
                pass

        message_bus.send(
            self.name, to, summary, "task_result_available",
            extra={
                "request_id": request_id,
                "result_path": str(result_path.relative_to(WORKDIR)),
            },
        )


# ===========================================================================
# 【主 Agent 侧】 TeammateManager
#
#     在主线程被 main.py 调用。所有方法都是"立即返回、不阻塞"。
#     严格按用途分组:  生命周期 / 派单 / 感知 / 视图
#     主 Agent 的真实阻塞点不在这个类（详见文件头说明）。
# ===========================================================================

class TeammateManager:
    """主 Agent 侧的调度器。单例。"""

    def __init__(self):
        _ensure_dirs()
        # name -> Thread / Event；stop_events[name] 用来请求 worker 停掉
        self.threads: dict[str, threading.Thread] = {}
        self.stop_events: dict[str, threading.Event] = {}

    # -------------------------------------------------------------------
    # [Manager] 生命周期: spawn / shutdown
    # -------------------------------------------------------------------

    def spawn(self, name: str, role: str) -> str:
        """
        创建一个处于 idle 的 worker 线程，**不带任何任务**。

        "先启动再派发" 的顺序是刻意的：
          - spawn 只负责让 worker 活起来并阻塞在 wait_for_inbox
          - 真正的任务由 dispatch 通过 assign_task 消息送进去
        这样可以支持对同一个 worker 派多个任务、插入澄清消息等。
        """
        existing = self.threads.get(name)
        if existing and existing.is_alive():
            return f"'{name}' is already running."

        _upsert_member(name, role, "idle")

        stop_event = threading.Event()
        worker = _TeammateWorker(name, role, stop_event)
        thread = threading.Thread(target=worker.run, daemon=True)

        self.stop_events[name] = stop_event
        self.threads[name] = thread
        thread.start()
        return f"Spawned idle teammate '{name}' (role: {role})"

    def shutdown(self, name: str, sender: str = LEAD_NAME) -> str:
        """请 worker 优雅退出（给它的 inbox 发 shutdown_request）。"""
        if name not in self.threads:
            return f"No teammate '{name}'."
        message_bus.send(sender, name, "Please shutdown.", "shutdown_request")
        return f"Shutdown request sent to '{name}'."

    # -------------------------------------------------------------------
    # [Manager] 派单: dispatch
    # -------------------------------------------------------------------

    def dispatch(self, name: str, task: str, sender: str = LEAD_NAME) -> str:
        """
        派发一个任务给已经 spawn 的 worker。**立即返回** request_id。

        副作用（按顺序）:
            1. 校验 worker 存在且存活
            2. 生成 request_id
            3. 写 pending/<rid>.json 登记
            4. 向 worker inbox 投 assign_task（带 request_id）
            5. MessageBus 内部 set event，worker 从 wait_for_inbox 醒来处理
        """
        cfg = _load_config()
        if not any(m["name"] == name for m in cfg["members"]):
            return f"Error: teammate '{name}' not found. Call spawn_teammate first."

        thread = self.threads.get(name)
        if not thread or not thread.is_alive():
            return f"Error: teammate '{name}' is not running."

        request_id = f"r-{uuid.uuid4().hex[:8]}"
        record = {
            "request_id": request_id,
            "to": name,
            "from": sender,
            "task": task,
            "status": "dispatched",
            "dispatched_at": time.time(),
        }
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        (PENDING_DIR / f"{request_id}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))

        message_bus.send(
            sender, name, task, "assign_task",
            extra={"request_id": request_id},
        )
        return f"Dispatched task {request_id} -> {name}"

    # -------------------------------------------------------------------
    # [Manager] 感知: poll_events
    # -------------------------------------------------------------------

    def poll_events(self, lead_name: str = LEAD_NAME) -> list[dict]:
        """
        主 Agent 每轮 loop 开头调一次（非阻塞，无消息就返回 []）。

        工作分两种情况:
            - task_result_available  → 调 _consume_result_event 归档 + 构造事件
            - 其它消息               → 原样透传

        返回的每条事件只含 "指针 + 摘要" 这种小体积信息，不塞完整结果，
        保证主 Agent 的 context 不被大段文本撑爆。
        """
        events: list[dict] = []
        raw = message_bus.read_inbox(lead_name)

        for msg in raw:
            if msg.get("type") == "task_result_available":
                events.append(self._consume_result_event(msg))
            else:
                events.append({
                    "type": msg.get("type") or "message",
                    "from": msg.get("from"),
                    "content": msg.get("content", ""),
                })

        return events

    def _consume_result_event(self, msg: dict) -> dict:
        """消化 task_result_available:
            - 确认文件还在
            - 更新 pending.status = acknowledged
            - 把 results/<rid>.md 搬到 results/processed/<rid>.md 归档
            - 返回给上层的结构化事件（含归档后的 result_path）
        """
        request_id = msg.get("request_id")
        result_rel = msg.get("result_path")
        result_path = WORKDIR / result_rel if result_rel else None

        if not result_path or not result_path.exists():
            return {
                "type": "task_result_stale",
                "request_id": request_id,
                "from": msg.get("from"),
                "note": "Result file missing; possibly already processed.",
            }

        pending_path = PENDING_DIR / f"{request_id}.json"
        if pending_path.exists():
            try:
                pending = json.loads(pending_path.read_text())
                pending["status"] = "acknowledged"
                pending["acknowledged_at"] = time.time()
                pending_path.write_text(json.dumps(pending, indent=2, ensure_ascii=False))
            except Exception:
                pass

        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        target = PROCESSED_DIR / result_path.name
        if target.exists():
            target.unlink()  # 幂等：同名覆盖
        result_path.rename(target)

        return {
            "type": "task_result_available",
            "request_id": request_id,
            "from": msg.get("from"),
            "summary": msg.get("content", ""),
            "result_path": str(target.relative_to(WORKDIR)),
        }

    # -------------------------------------------------------------------
    # [Manager] 视图: 给主 Agent 和 / 命令用
    # -------------------------------------------------------------------

    def list_all(self) -> str:
        """列出当前所有成员及其状态。"""
        cfg = _load_config()
        if not cfg["members"]:
            return "No teammates."
        lines = [f"Team: {cfg['team_name']}"]
        for m in cfg["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def list_pending(self) -> str:
        """列出派单登记表。完成、已确认、在途都能看到。"""
        entries = sorted(PENDING_DIR.glob("*.json"))
        if not entries:
            return "No pending tasks."
        lines = ["Pending tasks:"]
        for p in entries:
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            lines.append(
                f"  {data.get('request_id')} -> {data.get('to')} "
                f"[{data.get('status')}]: {str(data.get('task', ''))[:60]}"
            )
        return "\n".join(lines)

    def member_names(self) -> list[str]:
        """供 broadcast 使用: 所有成员名的列表。"""
        return [m["name"] for m in _load_config()["members"]]


teammate_manager = TeammateManager()
