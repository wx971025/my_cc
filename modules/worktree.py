"""

============================================================================
一、定位
============================================================================
任务板（.tasks/）回答"做什么"；这里的 worktree 注册表（.worktrees/）
回答"在哪做、互不踩"。两张表通过 task_id 串起来。

每条"执行车道"= 一个 git worktree 子目录 + 一个独立 branch + 一条 index 条目。
worker 接到一条板任务时：
    create → bind → enter → 工作 → closeout(remove|keep)
目标是让并行 worker 互相不污染工作区，事后能一眼分辨每条任务的改动范围。

============================================================================
二、磁盘布局
============================================================================
    .worktrees/
        index.json          注册表（WorktreeRecord 列表）
        events.jsonl        生命周期事件日志（每行一 json）
        <name>/             真实的 git worktree 子目录（被 gitignored）

============================================================================
三、记录字段
============================================================================
WorktreeRecord:
    name                 车道短名 (e.g. t7-alice)
    path                 相对 WORKDIR 的目录路径 (e.g. .worktrees/t7-alice)
    branch               对应的 git 分支 (e.g. wt/t7-alice)
    task_id              绑定的任务 id
    status               active / kept / removed
    last_entered_at      最近进入车道的时间戳
    last_command_at      最近在车道里跑命令的时间戳
    last_command_preview 最近一条命令的前 120 字
    closeout             {action, reason, at}

EventRecord (events.jsonl):
    event ts task_id worktree ... 可变字段
"""
import json
import subprocess
import threading
import time
from pathlib import Path

from configs import WORKDIR


# ===========================================================================
# 路径与常量
# ===========================================================================

WT_ROOT = WORKDIR / ".worktrees"
INDEX_PATH = WT_ROOT / "index.json"
EVENTS_PATH = WT_ROOT / "events.jsonl"

# index.json 可能被多个线程并发写（create/closeout 来自不同 worker）
_wt_lock = threading.Lock()


# ===========================================================================
# WorktreeManager
# ===========================================================================

class WorktreeManager:
    """
    线程安全：所有 read-modify-write index.json 的方法都走 _wt_lock。

    git 不可用兜底：
        构造时探测一次 `git rev-parse --is-inside-work-tree`，失败则 self.disabled=True。
        disabled 状态下 create/enter/run/closeout 全部返回提示字符串而不抛异常，
        worker 侧只需看返回值是否以 "Created"/"OK" 开头决定是否进入隔离模式。
    """

    def __init__(self):
        WT_ROOT.mkdir(parents=True, exist_ok=True)
        self.disabled = not self._probe_git()
        self._ensure_index()

    # -------------------------------------------------------------------
    # 内部：索引 / 事件日志 / git 探测
    # -------------------------------------------------------------------

    @staticmethod
    def _probe_git() -> bool:
        """
        真正能用 = ①当前目录是 git 仓库 ② git 支持 `worktree` 子命令（≥2.5）。
        只满足 ① 时旧 git（如 CentOS 7 的 1.8.3）会把 `worktree list` 当成未知
        命令，我们应当直接 disable 而不是在跑任务时才失败，避免事件日志噪音。
        """
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=WORKDIR, capture_output=True, text=True, timeout=5,
            )
            if proc.returncode != 0 or proc.stdout.strip() != "true":
                return False
        except Exception:
            return False
        try:
            proc2 = subprocess.run(
                ["git", "worktree", "list"],
                cwd=WORKDIR, capture_output=True, text=True, timeout=5,
            )
            return proc2.returncode == 0
        except Exception:
            return False

    def _ensure_index(self):
        if not INDEX_PATH.exists():
            INDEX_PATH.write_text(json.dumps({"worktrees": []}, indent=2))

    def _load_index(self) -> dict:
        try:
            return json.loads(INDEX_PATH.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return {"worktrees": []}

    def _save_index(self, idx: dict):
        INDEX_PATH.write_text(json.dumps(idx, indent=2, ensure_ascii=False))

    def _emit(self, event: str, **kw):
        payload = {"event": event, "ts": time.time(), **kw}
        WT_ROOT.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _find(self, idx: dict, name: str) -> dict | None:
        for rec in idx["worktrees"]:
            if rec["name"] == name:
                return rec
        return None

    # -------------------------------------------------------------------
    # 公开：查询
    # -------------------------------------------------------------------

    def path_of(self, name: str) -> Path | None:
        """返回绝对路径；找不到或已 removed 返回 None。"""
        idx = self._load_index()
        rec = self._find(idx, name)
        if not rec or rec.get("status") == "removed":
            return None
        return (WORKDIR / rec["path"]).resolve()

    def info(self, name: str) -> str:
        idx = self._load_index()
        rec = self._find(idx, name)
        if not rec:
            return f"Error: worktree '{name}' not found"
        return json.dumps(rec, indent=2, ensure_ascii=False)

    def list_all(self) -> str:
        idx = self._load_index()
        if not idx["worktrees"]:
            return "No worktrees."
        lines = [f"Worktrees (disabled={self.disabled}):"]
        for rec in idx["worktrees"]:
            lines.append(
                f"  {rec['name']} [{rec['status']}] task=#{rec.get('task_id')} "
                f"branch={rec.get('branch')} path={rec.get('path')}"
            )
        return "\n".join(lines)

    # -------------------------------------------------------------------
    # 公开：生命周期
    # -------------------------------------------------------------------

    def create(self, name: str, task_id: int) -> str:
        """
        建一条新车道：
          1) git worktree add -b wt/<name> .worktrees/<name> HEAD
             分支已存在 → 回退到 attach：git worktree add <path> <branch>
          2) 写 WorktreeRecord 到 index.json
          3) 发事件 worktree.create
        """
        if self.disabled:
            return "Error: git not available; worktree disabled"

        with _wt_lock:
            idx = self._load_index()
            if self._find(idx, name):
                return f"Error: worktree '{name}' already exists"

            rel_path = Path(".worktrees") / name
            abs_path = WORKDIR / rel_path
            branch = f"wt/{name}"

            if abs_path.exists():
                return f"Error: path {rel_path} already exists"

            # 第一次尝试：新建分支
            create_cmd = ["git", "worktree", "add", "-b", branch, str(abs_path), "HEAD"]
            proc = subprocess.run(
                create_cmd, cwd=WORKDIR,
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                # 分支可能已经存在；尝试 attach
                attach_cmd = ["git", "worktree", "add", str(abs_path), branch]
                proc2 = subprocess.run(
                    attach_cmd, cwd=WORKDIR,
                    capture_output=True, text=True, timeout=60,
                )
                if proc2.returncode != 0:
                    err = (proc.stderr + "\n" + proc2.stderr).strip()[:300]
                    self._emit("worktree.create_failed", name=name,
                               task_id=task_id, error=err)
                    return f"Error: git worktree add failed: {err}"

            record = {
                "name": name,
                "path": str(rel_path),
                "branch": branch,
                "task_id": task_id,
                "status": "active",
                "last_entered_at": None,
                "last_command_at": None,
                "last_command_preview": None,
                "closeout": None,
            }
            idx["worktrees"].append(record)
            self._save_index(idx)

        self._emit("worktree.create", name=name, task_id=task_id, branch=branch)
        return f"Created worktree '{name}' at {rel_path} on branch {branch}"

    def bind(self, task_id: int, name: str) -> str:
        """把任务记录和车道绑起来：task.worktree / worktree_state=active / last_worktree。"""
        from modules.task import task_manager
        return task_manager.set_worktree(task_id, name, state="active")

    def enter(self, name: str) -> str:
        """记录 last_entered_at；不跑任何命令。worker 切 _active_base 用。"""
        with _wt_lock:
            idx = self._load_index()
            rec = self._find(idx, name)
            if not rec:
                return f"Error: worktree '{name}' not found"
            if rec["status"] != "active":
                return f"Error: worktree '{name}' is {rec['status']}, cannot enter"
            rec["last_entered_at"] = time.time()
            self._save_index(idx)

        self._emit("worktree.enter", name=name, task_id=rec.get("task_id"))
        return f"Entered worktree '{name}'"

    def run(self, name: str, command: str, timeout: int = 120) -> str:
        """
        在车道目录里执行 shell 命令。主要给主 Agent 的 worktree 工具用；
        同事 Agent 侧通常不走这里，而是 _TeammateWorker._exec_tool 里
        直接用 run_bash(cwd=_active_base)。
        """
        path = self.path_of(name)
        if path is None:
            return f"Error: worktree '{name}' not available"

        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(path),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Error: Timeout ({timeout}s)"
        except (FileNotFoundError, OSError) as e:
            return f"Error: {e}"

        with _wt_lock:
            idx = self._load_index()
            rec = self._find(idx, name)
            if rec:
                rec["last_command_at"] = time.time()
                rec["last_command_preview"] = command[:120]
                self._save_index(idx)

        self._emit("worktree.run", name=name,
                   task_id=rec.get("task_id") if rec else None,
                   command_preview=command[:120])

        output = (proc.stdout + proc.stderr).strip()
        return output[:50000] if output else "(no output)"

    # -------------------------------------------------------------------
    # 公开：在车道内部打 commit（为后续 merge 流程做准备）
    # -------------------------------------------------------------------

    def _detect_main_branch(self) -> str:
        """
        粗糙判断主分支名：优先 main，其次 master，再不行用主 WORKDIR 当前 branch。
        返回可喂给 `git merge-base` 的名字；完全检测不到就回 HEAD。
        """
        try:
            for candidate in ("main", "master"):
                proc = subprocess.run(
                    ["git", "rev-parse", "--verify", candidate],
                    cwd=WORKDIR, capture_output=True, text=True, timeout=5,
                )
                if proc.returncode == 0:
                    return candidate
            proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=WORKDIR, capture_output=True, text=True, timeout=5,
            )
            name = proc.stdout.strip()
            return name if name and name != "HEAD" else "HEAD"
        except Exception:
            return "HEAD"

    def commit_lane(self, name: str, message: str) -> dict:
        """
        在车道里把工作区改动打成一个 commit。
            1) git status --porcelain → 无改动直接返回 had_changes=False
            2) git add -A && git commit -m <message>
            3) 收集 commit_sha / base_sha / files_changed / diff_stat

        返回字典（永远不抛异常，方便调用方兜底）:
            ok: bool               整个流程是否成功
            had_changes: bool      是否真的产生了新 commit
            commit_sha: str
            base_sha: str          wt 分支与主分支的分叉点
            files_changed: list
            diff_stat: str
            error: str             ok=False 时的原因
        """
        empty = {
            "ok": False, "had_changes": False,
            "commit_sha": "", "base_sha": "",
            "files_changed": [], "diff_stat": "", "error": "",
        }
        if self.disabled:
            return {**empty, "error": "git disabled"}

        abs_path = self.path_of(name)
        if abs_path is None:
            return {**empty, "error": f"worktree '{name}' not available"}

        def _run(cmd, timeout=30):
            return subprocess.run(
                cmd, cwd=str(abs_path),
                capture_output=True, text=True, timeout=timeout,
            )

        try:
            status = _run(["git", "status", "--porcelain"])
        except Exception as e:
            return {**empty, "error": f"git status failed: {e}"}

        if not status.stdout.strip():
            # 干净工作区，没活干
            return {**empty, "ok": True}

        add = _run(["git", "add", "-A"])
        if add.returncode != 0:
            return {**empty, "had_changes": True,
                    "error": f"git add failed: {add.stderr.strip()[:200]}"}

        commit = _run(["git", "commit", "-m", message,
                       "--no-verify", "--allow-empty-message"])
        if commit.returncode != 0:
            return {**empty, "had_changes": True,
                    "error": f"git commit failed: {commit.stderr.strip()[:200]}"}

        sha = _run(["git", "rev-parse", "HEAD"]).stdout.strip()

        main_branch = self._detect_main_branch()
        base_sha = ""
        files: list[str] = []
        diff_stat = ""
        if main_branch and main_branch != "HEAD":
            mb = _run(["git", "merge-base", "HEAD", main_branch])
            base_sha = mb.stdout.strip() if mb.returncode == 0 else ""
        if base_sha:
            files = _run(
                ["git", "diff", "--name-only", f"{base_sha}..HEAD"]
            ).stdout.strip().splitlines()
            diff_stat = _run(
                ["git", "diff", "--stat", f"{base_sha}..HEAD"]
            ).stdout.strip()

        self._emit("worktree.commit", name=name,
                   sha=sha, files_count=len(files))

        return {
            "ok": True,
            "had_changes": True,
            "commit_sha": sha,
            "base_sha": base_sha,
            "files_changed": files,
            "diff_stat": diff_stat,
            "error": "",
        }

    # -------------------------------------------------------------------
    # 公开：收尾
    # -------------------------------------------------------------------

    def closeout(
        self,
        name: str,
        action: str,
        reason: str = "",
        complete_task: bool = False,
    ) -> str:
        """
        结束一条车道。
            action='keep'   → 目录保留，status→kept
            action='remove' → `git worktree remove`，status→removed
        安全降级:
            · 若车道里有未提交改动，action='remove' 会自动降级成 'keep'，
              reason 前缀为 'dirty:'，避免误删。
            · `git worktree remove` 失败同样降级为 'keep'，reason 带错误摘要。
        complete_task=True 时额外把对应任务 status 置 completed。
        """
        if action not in ("keep", "remove"):
            return f"Error: invalid action '{action}'"

        with _wt_lock:
            idx = self._load_index()
            rec = self._find(idx, name)
            if not rec:
                return f"Error: worktree '{name}' not found"
            if rec["status"] in ("removed",):
                return f"Error: worktree '{name}' already removed"
            task_id = rec.get("task_id")
            abs_path = WORKDIR / rec["path"]

            # dirty check
            if action == "remove" and abs_path.exists():
                try:
                    proc = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=abs_path, capture_output=True, text=True, timeout=10,
                    )
                    if proc.stdout.strip():
                        action = "keep"
                        reason = (f"dirty: {reason}" if reason
                                  else "dirty: uncommitted changes")
                except Exception as e:
                    action = "keep"
                    reason = f"dirty-check-failed: {e}"

            # 执行 action
            if action == "remove":
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", str(abs_path)],
                        cwd=WORKDIR, capture_output=True, text=True,
                        timeout=30, check=True,
                    )
                    rec["status"] = "removed"
                except subprocess.CalledProcessError as e:
                    err = (e.stderr or str(e)).strip()[:120]
                    action = "keep"
                    reason = f"remove-failed: {err}"
                    rec["status"] = "kept"
            else:
                rec["status"] = "kept"

            closeout_record = {
                "action": action,
                "reason": reason,
                "at": time.time(),
            }
            rec["closeout"] = closeout_record
            self._save_index(idx)

        # 回写到任务板
        from modules.task import task_manager
        wt_state = "removed" if action == "remove" else "kept"
        task_manager.set_worktree(task_id, name if action == "keep" else "",
                                  state=wt_state)
        task_manager.set_closeout(task_id, closeout_record)

        if complete_task:
            from modules.taskBoard import complete_task as _ct
            _ct(task_id, owner=None)

        self._emit(
            f"worktree.closeout.{action}",
            name=name, task_id=task_id, reason=reason,
            complete_task=complete_task,
        )
        return (f"Closeout '{name}' action={action} task=#{task_id} "
                f"complete_task={complete_task} reason={reason or '(none)'}")


worktree_manager = WorktreeManager()
