from multiprocessing import managers
import re
import os
from pathlib import Path

from configs import(
    MEMORY_DIR, 
    MEMORY_TYPES, 
    WORKDIR, 
    MEMORY_INDEX, 
    MAX_INDEX_LINES,
    now
)


class MemoryManager:
    """
    跨会话的持久记忆管理器。单例模式
    
    存储结构：
    - 每条记忆对应一个独立的 .md 文件（带 YAML frontmatter）
    - MEMORY.md 是所有记忆的索引摘要（自动重建）
    
    典型用途：Agent 可以把"用户偏好"、"项目约定"等信息存为记忆，
    下次启动时自动加载，不需要用户重复说明。
    """

    def __new__(cls, memory_dir: Path = None):
        if not hasattr(cls, "instance"):
            cls.instance = super(MemoryManager, cls).__new__(cls)
        return cls.instance

    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.memories = {}  # name -> {description, type, content}

        self._load_all()

    
    def _parse_frontmatter(self, text: str) -> dict | None:
        """解析 YAML frontmatter 格式的 .md 文件，返回元信息 + 正文内容。"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return None
        header, body = match.group(1), match.group(2)
        result = {"content": body.strip()}
        for line in header.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result


    def _load_all(self) -> None:
        """启动时调用：扫描记忆目录，把所有 .md 记忆加载到内存中。"""
        self.memories = {}
        if not self.memory_dir.exists():
            return

        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            
            parsed = self._parse_frontmatter(md_file.read_text())
            if parsed:
                name = parsed.get("name", md_file.stem)
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "file": md_file.name,
                }
        count = len(self.memories)
        if count > 0:
            print(f"[Memory] loaded: {count} memories from {self.memory_dir}")
        else:
            print(f"[Memory] no memories found in {self.memory_dir}")


    def load_memory_prompt(self) -> str:
        """把所有记忆格式化成 Markdown 文本，注入到 system prompt 中。"""
        if not self.memories:
            return ""
        sections = []
        sections.append("# Memories (persistent across sessions)")
        sections.append("")
        # 按类型分组，便于阅读
        for mem_type in MEMORY_TYPES:
            typed = {k: v for k, v in self.memories.items() if v["type"] == mem_type}
            if not typed:
                continue
            sections.append(f"## [{mem_type}]")
            for name, mem in typed.items():
                sections.append(f"### {name}: {mem['description']}")
                if mem["content"].strip():
                    sections.append(mem["content"].strip())
                sections.append("")
        return "\n".join(sections)

    
    def _rebuild_index(self) -> None:
        """重建 MEMORY.md 索引文件，限制最多 MAX_INDEX_LINES 行防止膨胀。"""
        lines = ["# Memory Index", ""]
        for name, mem in self.memories.items():
            lines.append(f"- {name}: {mem['description']} [{mem['type']}]")
            if len(lines) >= MAX_INDEX_LINES:
                lines.append(f"... (truncated at {MAX_INDEX_LINES} lines)")
                break
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        MEMORY_INDEX.write_text("\n".join(lines) + "\n")


    def save_memory(self, name: str, description: str, mem_type: str, content: str) -> str:
        """
        这个方法作为工具的handler。
        Agent 调用此方法保存一条新记忆。
        流程：校验类型 → 写 .md 文件 → 更新内存字典 → 重建索引。
        """
        if mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"
        # 把记忆名转为安全文件名（只保留字母数字下划线横线）
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        if not safe_name:
            return "Error: invalid memory name"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        # 写入带 frontmatter 的 .md 文件
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {mem_type}\n"
            f"---\n"
            f"{content}\n"
        )
        file_name = f"{safe_name}.md"
        file_path = self.memory_dir / file_name
        file_path.write_text(frontmatter)
        self.memories[name] = {
            "description": description,
            "type": mem_type,
            "content": content,
            "file": file_name,
        }
        self._rebuild_index()
        return f"Saved memory '{name}' [{mem_type}] to {file_path.relative_to(WORKDIR)}"


class DreamConsolidator:
    """
    记忆整合器（"做梦"机制）。
    
    问题：随着记忆越来越多，会出现重复、过时、冗余条目。
    方案：在会话间歇自动执行整合——合并相似记忆、删除过时条目、裁剪索引。
    
    当前为教学骨架版：4 个阶段只打印日志，不做实际 LLM 调用。
    真实实现需要在每个阶段调用模型做语义判断。
    """

    COOLDOWN_SECONDS = 86400       # 两次整合之间至少间隔 24 小时
    SCAN_THROTTLE_SECONDS = 600    # 两次"检查是否该整合"之间至少间隔 10 分钟
    MIN_SESSION_COUNT = 5          # 至少经历 5 个会话才有足够数据做整合
    LOCK_STALE_SECONDS = 3600      # 锁文件超过 1 小时视为过期（进程可能已崩溃）

    # 整合的 4 个阶段
    PHASES = [
        "Orient: scan MEMORY.md index for structure and categories",   # 扫描索引，了解当前记忆结构
        "Gather: read individual memory files for full content",       # 读取每条记忆的完整内容
        "Consolidate: merge related memories, remove stale entries",   # 合并相关记忆，删除过时条目
        "Prune: enforce 200-line limit on MEMORY.md index",            # 裁剪索引，防止超过 200 行
    ]

    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.lock_file = self.memory_dir / ".dream_lock"
        self.enabled = True
        self.mode = "default"
        self.last_consolidation_time = 0.0
        self.last_scan_time = 0.0
        self.session_count = 0


    def should_consolidate(self) -> tuple[bool, str]:
        """
        规则检查，全部通过才允许整合。
        任一检测失败立即返回原因，避免不必要的后续检查。
        """
        
        if not self.enabled:
            return False, "Consolidation is disabled!"

        if not self.memory_dir.exists():
            return False, "Memory directory does not exist!"

        memory_files = list(self.memory_dir.glob("*.md"))
        memory_files = [f for f in memory_files if f.name != "MEMORY.md"]
        if not memory_files:
            return False, "No memory files found!"

        # plan 模式下禁止写操作，不允许整合
        if self.mode == "plan":
            return False, "Plan mode does not allow consolidation!"

        # 距上次整合至少 24 小时（防止频繁整合）
        time_since_last = now - self.last_consolidation_time
        if time_since_last < self.COOLDOWN_SECONDS:
            remaining = int(self.COOLDOWN_SECONDS - time_since_last)
            return False, f"Cooldown active, {remaining}s remaining!"

        # 距上次扫描至少 10 分钟（防止高频探测）
        time_since_scan = now - self.last_scan_time
        if time_since_scan < self.SCAN_THROTTLE_SECONDS:
            remaining = int(self.SCAN_THROTTLE_SECONDS - time_since_scan)
            return False, f"Scan throttle active, {remaining}s remaining!"

        # 至少积累 5 个会话的数据，太少没有整合价值
        if self.session_count < self.MIN_SESSION_COUNT:
            return False, f"Only {self.session_count} sessions, need {self.MIN_SESSION_COUNT}!"

        # 获取进程锁，防止多个进程同时整合
        if not self._acquire_lock():
            return False, "Lock held by another process!"

        return True, "All rules passed"


    def consolidate(self) -> list[str]:
        """
        执行 4 阶段整合流程。
        当前教学版只走流程打日志，真实版本每个阶段需调用 LLM 做语义判断。
        """
        can_run, reason = self.should_consolidate()
        if not can_run:
            print(f"[Dream] Cannot consolidate: {reason}")
            return []

        print("[Dream] Starting consolidation...")
        self.last_scan_time = now()
        completed_phases = []
        for i, phase in enumerate(self.PHASES, 1):
            print(f"[Dream] Phase {i}/4: {phase}")
            # 模型调用
            completed_phases.append(phase)

        self.last_consolidation_time = now()
        self._release_lock()
        print(f"[Dream] Consolidation complete: {len(completed_phases)} phases executed")
        return completed_phases


    def _acquire_lock(self) -> bool:
        """
        获取 PID 锁文件，防止多进程并发整合。
        
        锁文件格式："{pid}:{timestamp}"
        
        处理逻辑：
        - 锁文件不存在 → 直接创建，拿到锁
        - 锁文件存在且超过 1 小时 → 视为过期（进程可能已崩溃），删除后重新获取
        - 锁文件存在且未过期 → 检查持有锁的进程是否还活着
          - 还活着 → 获取失败
          - 已死亡 → 删除后重新获取
        """
        if self.lock_file.exists():
            try:
                lock_data = self.lock_file.read_text().strip()
                pid_str, timestamp_str = lock_data.split(":", 1)
                pid = int(pid_str)
                lock_time = float(timestamp_str)

                if (now() - lock_time) > self.LOCK_STALE_SECONDS:
                    # 锁已过期，强制移除
                    print(f"[Dream] Removing stale lock from PID {pid}")
                    self.lock_file.unlink()
                else:
                    try:
                        os.kill(pid, 0)     # 不发送信号，仅检查进程是否存在
                        return False        # 进程还活着，锁有效，获取失败
                    except OSError:
                        print(f"[Dream] Removing lock from dead PID {pid}")
                        self.lock_file.unlink()
            except (ValueError, OSError):
                self.lock_file.unlink(missing_ok=True)

        # 获得锁
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}:{now()}".encode())
            os.close(fd)
            return True
        except OSError:
            return False
    
    def _release_lock(self):
        """释放锁文件（只有锁的持有者才能释放，通过 PID 校验）。"""
        try:
            if self.lock_file.exists():
                lock_data = self.lock_file.read_text().strip()
                pid_str = lock_data.split(":")[0]
                if int(pid_str) == os.getpid():
                    self.lock_file.unlink()
        except (ValueError, OSError):
            pass

memory_manager = MemoryManager()
