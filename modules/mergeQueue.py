"""
merge_queue: worker 在车道里 commit 之后，patch 待 lead 评审 / 合并的登记表。

============================================================================
一、定位
============================================================================
worktree 负责"在哪做"、"做完清场"；mergeQueue 负责"把结果 integrate 回主分支"。
worker 在自己的车道里 `git commit` 之后：
    - 目录可以被 git worktree remove（留下干净的主 WORKDIR）
    - 分支 wt/<name> 上留着 commits
    - 同时在本模块登记一条 entry（status=ready_for_review）
lead 通过 merge_queue_list / merge_review / merge_integrate 三个工具
串行 integrate：merge 成功→删分支+complete_task；冲突→abort+标 conflicted；
reject→保留分支供复盘。

============================================================================
二、磁盘布局
============================================================================
    .team/
        merge_queue.json         { "entries": [ MergeRequest, ... ] }

MergeRequest 字段:
    task_id / worktree / branch / owner / subject
    commit_sha / base_sha / files_changed / diff_stat / summary
    status: ready_for_review | merged | conflicted | rejected
    created_at / updated_at
    merge_sha / conflict_files / reject_reason / last_error   (按状态补充)

============================================================================
三、并发
============================================================================
    _queue_lock   护 merge_queue.json 的 read-modify-write
    _merge_lock   护主分支 merge 操作的串行执行
"""
import json
import subprocess
import threading
import time

from configs import WORKDIR

MERGE_QUEUE_PATH = WORKDIR / ".team" / "merge_queue.json"

_queue_lock = threading.Lock()
_merge_lock = threading.Lock()


# ===========================================================================
# 文件读写
# ===========================================================================

def _load() -> dict:
    if not MERGE_QUEUE_PATH.exists():
        return {"entries": []}
    try:
        return json.loads(MERGE_QUEUE_PATH.read_text())
    except Exception:
        return {"entries": []}


def _save(data: dict) -> None:
    MERGE_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MERGE_QUEUE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _find(data: dict, task_id: int) -> dict | None:
    for e in data["entries"]:
        if e.get("task_id") == task_id:
            return e
    return None


# ===========================================================================
# 公开：登记 / 查询
# ===========================================================================

def enqueue(entry: dict) -> None:
    """
    worker 在车道 commit 后登记一条待 review 的 MR。
    同一 task_id 再次 enqueue 会覆盖旧条目（worker 重试时用）。
    """
    entry = dict(entry)
    entry.setdefault("status", "ready_for_review")
    entry.setdefault("created_at", time.time())
    with _queue_lock:
        data = _load()
        data["entries"] = [
            e for e in data["entries"]
            if e.get("task_id") != entry.get("task_id")
        ]
        data["entries"].append(entry)
        _save(data)


def _update(task_id: int, **kw) -> None:
    with _queue_lock:
        data = _load()
        entry = _find(data, task_id)
        if entry is None:
            return
        entry.update(kw)
        entry["updated_at"] = time.time()
        _save(data)


def list_all() -> str:
    """给 lead 的 merge_queue_list 工具用。摘要视图。"""
    data = _load()
    if not data["entries"]:
        return "Merge queue empty."
    lines = ["Merge queue:"]
    for e in data["entries"]:
        lines.append(
            f"  #{e.get('task_id')} [{e.get('status')}] "
            f"branch={e.get('branch')} owner={e.get('owner')} "
            f"files={len(e.get('files_changed', []))} "
            f"sha={(e.get('commit_sha') or '')[:8]}"
        )
    return "\n".join(lines)


def review(task_id: int) -> str:
    """给 lead 的 merge_review 工具用。全字段 dump。"""
    data = _load()
    entry = _find(data, task_id)
    if not entry:
        return f"Error: no merge request for task #{task_id}"
    return json.dumps(entry, indent=2, ensure_ascii=False)


# ===========================================================================
# 公开：integrate (merge / reject)
# ===========================================================================

def _detect_target_branch() -> str:
    """主 WORKDIR 当前所在分支 = merge 的目标。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=WORKDIR, capture_output=True, text=True, timeout=5,
        )
        name = proc.stdout.strip()
        return name if name and name != "HEAD" else ""
    except Exception:
        return ""


def integrate(task_id: int, strategy: str, reason: str = "") -> str:
    """
    处理一条 MR:
        strategy='merge'  → `git merge --no-ff --no-edit wt/<name>` 到主 WORKDIR 当前分支
                           无冲突: status=merged, 删除 worker 分支, complete_task
                           有冲突: git merge --abort, status=conflicted, 返回冲突文件
        strategy='reject' → 标 rejected（分支/commit 保留供复盘）
    merge 路径持 _merge_lock，保证主分支不会被多个 integrate 并发写坏。

    注意:
        - 拒绝 merge 不会动 task.status；如需推进流程由 lead 自行决定
          (例如调用 worktree_closeout 强制回收、或重新 dispatch)
        - 仅 merge 成功会调用 complete_task，把任务从 in_progress → completed
    """
    if strategy not in ("merge", "reject"):
        return f"Error: invalid strategy '{strategy}'"

    data = _load()
    entry = _find(data, task_id)
    if not entry:
        return f"Error: no merge request for task #{task_id}"
    if entry.get("status") in ("merged", "rejected"):
        return f"Error: task #{task_id} already {entry['status']}"

    branch = entry.get("branch")
    if not branch:
        return "Error: entry missing branch"

    if strategy == "reject":
        _update(task_id, status="rejected",
                reject_reason=reason or "(no reason)")
        return (f"Rejected MR #{task_id} (branch={branch}); "
                f"reason={reason or '(none)'}. "
                f"Branch kept for inspection. "
                f"Run worktree_closeout / git branch -D manually if needed.")

    # strategy == merge
    with _merge_lock:
        target = _detect_target_branch()
        if not target:
            return "Error: cannot detect target branch in main WORKDIR"

        # 主 WORKDIR 不能 dirty，否则 merge 行为难以预测
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=WORKDIR, capture_output=True, text=True, timeout=10,
        )
        if status.stdout.strip():
            return ("Error: main WORKDIR is dirty; "
                    "commit/stash before integrating")

        merge_proc = subprocess.run(
            ["git", "merge", "--no-ff", "--no-edit",
             "-m", f"Merge {branch} into {target} (board #{task_id})",
             branch],
            cwd=WORKDIR, capture_output=True, text=True, timeout=60,
        )
        if merge_proc.returncode != 0:
            conflicts = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=WORKDIR, capture_output=True, text=True, timeout=10,
            ).stdout.strip().splitlines()
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=WORKDIR, capture_output=True, text=True, timeout=10,
            )
            err = (merge_proc.stderr or merge_proc.stdout).strip()[:300]
            _update(task_id, status="conflicted",
                    conflict_files=conflicts, last_error=err)
            return (f"Conflict merging {branch}: {len(conflicts)} file(s). "
                    f"Merge aborted. Files: {conflicts[:5]}")

        merge_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=WORKDIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        # 成功后尝试删除 worker 分支（残留 branch 堆积会让 git worktree 状态混乱）
        del_proc = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=WORKDIR, capture_output=True, text=True, timeout=10,
        )

        _update(task_id, status="merged",
                merge_sha=merge_sha,
                merged_into=target,
                branch_deleted=(del_proc.returncode == 0))

    # 任务完结 放在锁外，避免 task_manager 的锁嵌套
    from modules.taskBoard import complete_task
    try:
        complete_task(task_id, owner=None)
    except Exception as e:
        print(f"[merge_queue] complete_task failed for #{task_id}: {e}")

    return (f"Merged #{task_id}: {branch} → {target}; "
            f"sha={merge_sha[:8]}; "
            f"branch_deleted={del_proc.returncode == 0}")
