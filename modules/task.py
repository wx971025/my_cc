import json
from pathlib import Path

from configs import WORKDIR

TASKS_DIR = WORKDIR / ".tasks"

class TaskManager:
    """Persistent TaskRecord store.
    Think "work graph on disk", not "currently running worker".
    """
    def __init__(self, tasks_dir: Path = TASKS_DIR):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    
    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0
    

    def _load(self, task_id: int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())


    def _save(self, task: dict):
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))

    
    def create(self, subject: str, description: str = "", claim_role: str = "") -> str:
        task = {
            "id": self._next_id, 
            "subject": subject, 
            "description": description,
            "status": "pending", 
            "blockedBy": [], 
            "blocks": [], 
            "owner": "",
            "claim_role": claim_role,
            # s18 worktree 隔离字段
            "worktree": "",              # 当前绑定的车道名
            "worktree_state": "unbound", # active / kept / removed / unbound
            "last_worktree": "",         # 最近一次用过的车道
            "closeout": None,            # 最近一次收尾动作 (action/reason/at)
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)


    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    
    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other tasks' blockedBy lists."""
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)


    def update(self, 
        task_id: int, 
        status: str = None, 
        owner: str = None,
        add_blocked_by: list = None, 
        add_blocks: list = None
    ) -> str:
        task = self._load(task_id)
        if owner is not None:
            task["owner"] = owner
        if status:
            if status not in ("pending", "in_progress", "completed", "deleted"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status

            if status == "completed":
                self._clear_dependency(task_id)

        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))

            # 给被阻塞的任务添加父级
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    pass
        # 保存任务
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    
    # -------------------------------------------------------------------
    # worktree 相关字段 setter（供 modules/worktree.py 回写）
    # -------------------------------------------------------------------

    def set_worktree(self, task_id: int, name: str, state: str) -> str:
        """
        绑定 / 解绑车道。state 取值: active / kept / removed / unbound
        绑定成功若任务尚 pending 自动提升为 in_progress。
        """
        try:
            task = self._load(task_id)
        except ValueError as e:
            return f"Error: {e}"
        task["worktree"] = name
        task["worktree_state"] = state
        if name:
            task["last_worktree"] = name
        if state == "active" and task.get("status") == "pending":
            task["status"] = "in_progress"
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def set_closeout(self, task_id: int, closeout: dict) -> str:
        """记录任务侧最近一次收尾动作（action / reason / at）。"""
        try:
            task = self._load(task_id)
        except ValueError as e:
            return f"Error: {e}"
        task["closeout"] = closeout
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)


    def list_all(self) -> str:
        """List all tasks in the order of their creation."""
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."

        lines = []
        for t in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[√]", "deleted": "[-]"}.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            owner = f" owner={t['owner']}" if t.get("owner") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)

task_manager = TaskManager(TASKS_DIR)
