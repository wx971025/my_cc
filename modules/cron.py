import os
import json
import time
import threading
import uuid
from queue import Empty
from datetime import datetime, timedelta
from queue import Queue
from pathlib import Path

from configs import WORKDIR, now


SCHEDULED_TASKS_FILE = WORKDIR / ".claude" / "scheduled_tasks.json"
CRON_LOCK_FILE = WORKDIR / ".claude" / "cron.lock"
AUTO_EXPIRY_DAYS = 7
JITTER_MINUTES = [0, 30]  # avoid these exact minutes for recurring tasks
JITTER_OFFSET_MAX = 4     # offset range in minutes


class CronLock:
    """
    PID-file-based lock to prevent multiple sessions from firing the same cron job.
    """
    def __init__(self, lock_path: Path = CRON_LOCK_FILE):
        self._lock_path = lock_path


    def acquire(self) -> bool:
        """
        Try to acquire the cron lock. Returns True on success.
        If a lock file exists, check whether the PID inside is still alive.
        If the process is dead the lock is stale and we can take over.
        """
        if self._lock_path.exists():
            try:
                stored_pid = int(self._lock_path.read_text().strip())
                # PID liveness probe: send signal 0 (no-op) to check existence
                os.kill(stored_pid, 0)
                # Process is alive -- lock is held by another session
                return False
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                # Stale lock (process dead or PID unparseable) -- remove it
                pass
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.write_text(str(os.getpid()))
        return True

    def release(self):
        """Remove the lock file if it belongs to this process."""
        try:
            if self._lock_path.exists():
                stored_pid = int(self._lock_path.read_text().strip())
                if stored_pid == os.getpid():
                    self._lock_path.unlink()
        except (ValueError, OSError):
            pass


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """Match a single cron field against a value."""
    if field == "*":
        return True

    for part in field.split(","):
        # Handle step: */N or N-M/S
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
        if part == "*":
            # */N -- check if value is on the step grid
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            # Range: N-M
            start, end = part.split("-", 1)
            start, end = int(start), int(end)
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            # Exact value
            if int(part) == value:
                return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """
    Check if a 5-field cron expression matches a given datetime.
    Fields: minute hour day-of-month month day-of-week
    Supports: * (any), */N (every N), N (exact), N-M (range), N,M (list)
    No external dependencies -- simple manual matching.
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    values = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday()]
    # Python weekday: 0=Monday; cron: 0=Sunday. Convert.
    cron_dow = (dt.weekday() + 1) % 7
    values[4] = cron_dow
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for field, value, (lo, hi) in zip(fields, values, ranges):
        if not _field_matches(field, value, lo, hi):
            return False
    return True


class CronScheduler:
    """
    定时任务调度器（教学版）。

    这个类的核心目标：在“后台线程”里每分钟检查一次任务是否命中，
    命中后把通知写入队列，由“主线程”在合适时机拉取并展示/处理。

    你可以把它理解为三个层次：
    1) 数据层：self.tasks
       - 内存中的任务列表（每个元素是一个 dict）
       - 可选持久化到 .claude/scheduled_tasks.json（durable 任务）
    2) 调度层：_check_loop + _check_tasks
       - 后台线程每秒醒一次，但只在“分钟变化”时真正执行匹配
    3) 通信层：self.queue (Queue)
       - 后台线程只负责产出通知（queue.put）
       - 主线程负责消费通知（drain_notifications）

    关于并发与“为什么这里几乎没显式加锁”：
    - Queue 本身是线程安全的，跨线程传消息推荐优先用它，而不是共享变量。
    - tasks 列表目前采用“简单共享”模式：后台线程读/少量写，主线程增删任务。
      教学版为了可读性没有再套 Lock。
    - 如果后续并发写入更复杂（高频 create/delete），建议引入 threading.Lock，
      把对 self.tasks 的读写包在同一把锁里，避免竞态。
    """
    def __init__(self):
        self.tasks = []        # 任务列表（共享状态）：[{id, cron, prompt, recurring, ...}, ...]
        self.queue = Queue()   # 线程安全通知队列：后台线程 put，主线程 get

        # Event 是“线程间信号灯”：
        # - 默认 unset(False)
        # - 调 stop() 时 set(True)
        # - 后台循环里用 is_set()/wait() 观察这个信号并优雅退出
        #
        # 这里用 Event 比“全局布尔变量 + sleep”更好：
        # 1) 语义更清晰（就是停止信号）
        # 2) wait(timeout) 可被 set() 立即唤醒，退出更快
        self._stop_event = threading.Event()

        self._thread = None  # 后台调度线程对象（threading.Thread）
        self._last_check_minute = -1  # 防止同一分钟内重复触发

    
    def _load_durable(self):
        """
        从磁盘加载 durable 任务。

        约定：
        - 仅 durable=True 的任务会落盘；
        - 启动时只恢复 durable 任务, session-only 任务不会恢复。
        """
        if not SCHEDULED_TASKS_FILE.exists():
            return
        try:
            data = json.loads(SCHEDULED_TASKS_FILE.read_text())
            self.tasks = [t for t in data if t.get("durable")]
        except Exception as e:
            print(f"[Cron] Error loading tasks: {e}")


    def _save_durable(self):
        """
        将当前 durable 任务写回磁盘。

        注意：这是“全量重写”文件，不是增量 append。
        好处是逻辑简单、可读性高；代价是任务非常多时 I/O 会变重。
        """
        durable = [t for t in self.tasks if t.get("durable")]
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULED_TASKS_FILE.write_text(
            json.dumps(durable, indent=2, ensure_ascii=False) + "\n"
        )

    
    def _check_tasks(self, now: datetime):
        """
        扫描任务并触发命中的任务。

        参数 now 由 _check_loop 传入，表示“本轮检查的当前时间”。
        处理顺序：
        1) 先做过期清理（长期 recurring 任务自动过期）
        2) 再做 cron 匹配
        3) 命中后写通知队列、更新 last_fired
        4) one-shot 命中后移除
        5) 若有删除动作则持久化 durable 任务
        """
        expired = []
        fired_oneshots = []
        for task in self.tasks:
            # 自动过期策略：recurring 任务超过 AUTO_EXPIRY_DAYS 天自动删除
            # 目的：防止无限累积的旧任务长期占用调度扫描成本。
            age_days = (time.time() - task["createdAt"]) / 86400
            if task["recurring"] and age_days > AUTO_EXPIRY_DAYS:
                expired.append(task["id"])
                continue

            # jitter（抖动）用于错峰：
            # 如果很多任务都卡在 :00 / :30，所有实例会同一时刻触发，
            # 容易造成“瞬时尖峰”。这里给任务加一个确定性的分钟偏移。
            check_time = now
            jitter = task.get("jitter_offset", 0)
            if jitter:
                check_time = now - timedelta(minutes=jitter)
            if cron_matches(task["cron"], check_time):
                notification = (
                    f"[Scheduled task {task['id']}]: {task['prompt']}"
                )
                self.queue.put(notification)
                task["last_fired"] = time.time()
                print(f"[Cron] Fired: {task['id']}")
                if not task["recurring"]:
                    # one-shot 只执行一次：命中后加入待删除列表
                    fired_oneshots.append(task["id"])

        # 清理过期任务与已触发 one-shot 任务
        if expired or fired_oneshots:
            remove_ids = set(expired) | set(fired_oneshots)
            self.tasks = [t for t in self.tasks if t["id"] not in remove_ids]
            for tid in expired:
                print(f"[Cron] Auto-expired: {tid} (older than {AUTO_EXPIRY_DAYS} days)")
            for tid in fired_oneshots:
                print(f"[Cron] One-shot completed and removed: {tid}")
            self._save_durable()

    
    def _check_loop(self):
        """
        后台线程主循环。

        关键点：
        - 线程每秒醒一次（wait(timeout=1)），但只有“分钟变化”才真正扫描任务；
        - 用 _stop_event 控制退出，不用 while True + sleep 粗暴轮询；
        - 该线程是 daemon=True，主进程退出时不会阻塞进程结束。
        """
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute = now.hour * 60 + now.minute

            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)

            # wait(1) 有两个作用：
            # 1) 正常情况下每秒轮询一次
            # 2) 若 stop() 调用 set()，wait 会立刻返回，线程尽快退出
            self._stop_event.wait(timeout=1)

    
    def detect_missed_tasks(self) -> list[dict]:
        """
        启动补偿：检测“离线期间错过的任务”。

        场景：
        - 程序关机/退出后，定时线程不运行；
        - 下次启动时，需要判断是否错过了本应触发的 cron 点。

        实现：
        - 以 last_fired 为起点，按“分钟粒度”向前扫描到 now（最多 24 小时）
        - 只要命中一次就标记该任务 missed（不继续找第二次）
        - 由调用方决定是补跑还是忽略
        """
        now = datetime.now()
        missed = []
        for task in self.tasks:
            last_fired = task.get("last_fired")
            if last_fired is None:
                continue
            last_dt = datetime.fromtimestamp(last_fired)
            # 从 last_fired 后一分钟开始逐分钟扫描，避免重复算已执行的那个点
            check = last_dt + timedelta(minutes=1)
            cap = min(now, last_dt + timedelta(hours=24))
            while check <= cap:
                if cron_matches(task["cron"], check):
                    missed.append({
                        "id": task["id"],
                        "cron": task["cron"],
                        "prompt": task["prompt"],
                        "missed_at": check.isoformat(),
                    })
                    break  # one miss is enough to flag it
                check += timedelta(minutes=1)
        return missed

    
    def _compute_jitter(self, cron_expr: str) -> int:
        """
        计算错峰偏移分钟数。

        当前策略很保守：只有 minute 字段是固定值 0 或 30 才加偏移，
        偏移范围是 1~JITTER_OFFSET_MAX 分钟。

        这里用表达式 hash 做“确定性随机”：
        - 同一个 cron_expr 每次重启得到相同偏移，行为稳定；
        - 不同表达式大概率分散到不同偏移，达到错峰目的。
        """
        fields = cron_expr.strip().split()
        if len(fields) < 1:
            return 0
        minute_field = fields[0]
        try:
            minute_val = int(minute_field)
            if minute_val in JITTER_MINUTES:
                # Deterministic jitter based on the expression hash
                return (hash(cron_expr) % JITTER_OFFSET_MAX) + 1
        except ValueError:
            pass
        return 0


    def start(self):
        """
        启动调度器。

        顺序：
        1) 从磁盘恢复 durable 任务
        2) 创建并启动后台检查线程

        注意：如果已经启动过，调用方应避免重复 start（本实现未做幂等保护）。
        """
        self._load_durable()
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        count = len(self.tasks)
        if count:
            print(f"[Cron] Loaded {count} scheduled tasks")


    def stop(self):
        """
        停止调度器后台线程。

        - set() 通知线程退出；
        - join(timeout=2) 最多等待 2 秒，避免无限阻塞主线程。
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def create(self, 
        cron_expr: str, 
        prompt: str,
        recurring: bool = True, 
        durable: bool = False
    ) -> str:
        """
        这是Agent触发的
        新建任务并放入内存任务表。

        参数：
        - cron_expr: 5 段 cron 表达式
        - prompt: 命中时生成的通知文本
        - recurring: True=循环任务, False=一次性任务
        - durable: True=写入磁盘，重启可恢复
        """
        task_id = str(uuid.uuid4())[:8]
        now = time.time()
        task = {
            "id": task_id,
            "cron": cron_expr,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "createdAt": now,
        }
        # Jitter for recurring tasks: if the cron fires on :00 or :30,
        # note it so we can offset the check slightly
        if recurring:
            task["jitter_offset"] = self._compute_jitter(cron_expr)
        self.tasks.append(task)
        if durable:
            self._save_durable()
        mode = "recurring" if recurring else "one-shot"
        store = "durable" if durable else "session-only"
        return f"Created task {task_id} ({mode}, {store}): cron={cron_expr}"

    def delete(self, task_id: str) -> str:
        """按 task_id 删除任务，并同步持久化 durable 数据。"""
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        if len(self.tasks) < before:
            self._save_durable()
            return f"Deleted task {task_id}"
        return f"Task {task_id} not found"

    def list_tasks(self) -> str:
        """以人类可读格式列出当前内存中的所有任务。"""
        if not self.tasks:
            return "No scheduled tasks."
        lines = []
        for t in self.tasks:
            mode = "recurring" if t["recurring"] else "one-shot"
            store = "durable" if t["durable"] else "session"
            age_hours = (time.time() - t["createdAt"]) / 3600
            lines.append(
                f"  {t['id']}  {t['cron']}  [{mode}/{store}] "
                f"({age_hours:.1f}h old): {t['prompt'][:60]}"
            )
        return "\n".join(lines)
        
    def drain_notifications(self) -> list[str]:
        """
        一次性取走队列中所有通知（非阻塞）。

        这是主线程消费后台线程结果的入口。
        Queue 是线程安全的，因此这里无需额外加锁。
        """
        notifications = []
        while True:
            try:
                notifications.append(self.queue.get_nowait())
            except Empty:
                break
        return notifications


cron_scheduler = CronScheduler()
