"""
后台任务管理器
"""
import threading
import json
import uuid
import subprocess
from pathlib import Path

from configs import WORKDIR, now


RUNTIME_DIR = WORKDIR / ".runtime-tasks"
RUNTIME_DIR.mkdir(exist_ok=True)

STALL_THRESHOLD_S = 45


class NotificationQueue:
    """
    Priority-based notification queue with same-key folding.
    Folding means a newer message can replace an older message with the
    same key, so the context is not flooded with stale updates.
    """
    PRIORITIES = {"immediate": 0, "high": 1, "medium": 2, "low": 3}

    def __init__(self):
        self._queue = []  # list of (priority, key, message)
        self._lock = threading.Lock()

    
    def push(self, message: str, priority: str = "medium", key: str = None):
        with self._lock:
            if key:
                self._queue = [(p, k, m) for p, k, m in self._queue if k != key]
            self._queue.append((self.PRIORITIES.get(priority, 2), key, message))
            self._queue.sort(key=lambda x: x[0])

    def drain(self) -> list[str]:
        with self._lock:
            messages = [m for _, _, m in self._queue]
            self._queue.clear()
            return messages


class BackgroundManager:
    def __init__(self):
        self.dir = RUNTIME_DIR
        self.tasks = {}  # task_id -> {status, result, command, started_at}
        self._notification_queue = []  # completed task results
        self._lock = threading.Lock()

    def _record_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _output_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.log"

    def _persist_task(self, task_id: str):
        """持久化任务到.json文件"""
        record = dict(self.tasks[task_id])
        self._record_path(task_id).write_text(
            json.dumps(record, indent=2, ensure_ascii=False)
        )

    def _preview(self, output: str, limit: int = 500) -> str:
        """预览结果"""
        compact = " ".join((output or "(no output)").split())
        return compact[:limit]

    
    def _execute(self, task_id: str, command: str):
        """Thread target: run subprocess, capture output, push to queue."""
        try:
            """执行"""
            r = subprocess.run(
                command, 
                shell=True, 
                cwd=WORKDIR,
                capture_output=True, 
                text=True, 
                timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"
        final_output = output or "(no output)"

        preview = self._preview(final_output)
        output_path = self._output_path(task_id)
        output_path.write_text(final_output)

        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = final_output
        self.tasks[task_id]["finished_at"] = now()
        self.tasks[task_id]["result_preview"] = preview
        self._persist_task(task_id)

        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "preview": preview,
                "output_file": str(output_path.relative_to(WORKDIR)),
            })


    def run(self, command: str) -> str:
        """Start a background thread, return task_id immediately."""
        task_id = str(uuid.uuid4())[:8]     # uuid一个随机的task_id
        output_file = self._output_path(task_id)
        self.tasks[task_id] = {
            "id": task_id,
            "status": "running",
            "result": None,
            "command": command,
            "started_at": now(),
            "finished_at": None,
            "result_preview": "",
            "output_file": str(output_file.relative_to(WORKDIR)),
        }
        self._persist_task(task_id)
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return (
            f"Background task {task_id} started: {command[:80]} "
            f"(output_file={output_file.relative_to(WORKDIR)})"
        )


    def check(self, task_id: str = None) -> str:
        """Check status of one task or list all."""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"Error: Unknown task {task_id}"
            visible = {
                "id": t["id"],
                "status": t["status"],
                "command": t["command"],
                "result_preview": t.get("result_preview", ""),
                "output_file": t.get("output_file", ""),
            }
            return json.dumps(visible, indent=2, ensure_ascii=False)

        lines = []
        for tid, t in self.tasks.items():
            lines.append(
                f"{tid}: [{t['status']}] {t['command'][:60]} "
                f"-> {t.get('result_preview') or '(running)'}"
            )
        return "\n".join(lines) if lines else "No background tasks."


    def drain_notifications(self) -> list:
        """清空提醒队列"""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs


    def detect_stalled(self) -> list[str]:
        """
        返回所有卡死的任务
        """
        stalled = []
        for task_id, info in self.tasks.items():
            if info["status"] != "running":
                continue
            elapsed = now() - info.get("started_at", now())
            if elapsed > STALL_THRESHOLD_S:
                stalled.append(task_id)
        return stalled

background_manager = BackgroundManager()
