import json
import time
import threading

from configs import WORKDIR

_claim_lock = threading.Lock()


TASKS_DIR = WORKDIR / ".tasks"
CLAIM_EVENTS_PATH = TASKS_DIR / "claim_events.jsonl"

def _append_claim_event(payload: dict):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    with CLAIM_EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def publish_task(subject: str, description: str = "", claim_role: str = "") -> str:
    """
    主 Agent 往共享任务板挂一条新任务。

    落地点: .tasks/task_<id>.json  (复用 TaskManager)
    唤醒  : 给所有在册同事 inbox 投一条 'board_notify'，让空闲 worker 立刻回
           主循环顶上重扫任务板（不必等 WORKER_IDLE_TIMEOUT 的轮询）。
    """
    from modules.task import task_manager

    record = task_manager.create(subject, description, claim_role)

    try:
        from modules.teammate import teammate_manager, message_bus, LEAD_NAME
        for name in teammate_manager.member_names():
            if name == LEAD_NAME:
                continue
            message_bus.send(
                LEAD_NAME, name,
                f"new board task: {subject}",
                msg_type="board_notify",
            )
    except Exception as e:
        print(f"[taskBoard] publish_task notify failed: {e}")

    return record


def complete_task(task_id: int, owner: str | None = None) -> str:
    """
    worker 完成板上任务后的收尾：把状态从 in_progress → completed。

    owner=None 时保留已有 owner 不变（task_manager.update 仅在 owner is not None
    时才覆写）。从 WorktreeManager.closeout 里调用就用 None，避免把真实 owner 清掉。

    加锁是为了和另一侧可能并发的 task_manager.update 解耦
    （TaskManager.update 在 status=completed 时会扫描所有任务清 blockedBy）。
    """
    from modules.task import task_manager

    with _claim_lock:
        return task_manager.update(task_id, status="completed", owner=owner)


def _task_allows_role(task: dict, role: str | None) -> bool:
    required_role = task.get("claim_role") or task.get("required_role") or ""
    if not required_role:
        return True
    return bool(role) and role == required_role


def is_claimable_task(task: dict, role: str | None = None) -> bool:
    return (
        task.get("status") == "pending"     # 挂起的任务
        and not task.get("owner")            # 没有被认领
        and not task.get("blockedBy")        # 没有被阻塞
        and _task_allows_role(task, role)    # 允许认领的角色
    )


def scan_unclaimed_tasks(role: str | None = None) -> list:
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if is_claimable_task(task, role):
            unclaimed.append(task)
    return unclaimed


def claim_task(
    task_id: int,
    owner: str,
    role: str | None = None,
    source: str = "manual",
) -> str:
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found"
        task = json.loads(path.read_text())
        if not is_claimable_task(task, role):
            return f"Error: Task {task_id} is not claimable for role={role or '(any)'}"
        task["owner"] = owner
        task["status"] = "in_progress"
        task["claimed_at"] = time.time()
        task["claim_source"] = source
        path.write_text(json.dumps(task, indent=2))
    _append_claim_event({
        "event": "task.claimed",
        "task_id": task_id,
        "owner": owner,
        "role": role,
        "source": source,
        "ts": time.time(),
    })
    return f"Claimed task #{task_id} for {owner} via {source}"



def make_identity_block(name: str, role: str, team_name: str) -> dict:
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


def ensure_identity_context(messages: list, name: str, role: str, team_name: str):
    if messages and "<identity>" in str(messages[0].get("content", "")):
        return
    messages.insert(0, make_identity_block(name, role, team_name))
    messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
