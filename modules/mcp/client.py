"""
MCPClient: 一条到外部 MCP server 的 stdio / JSON-RPC 2.0 通道。

============================================================================
一、定位
============================================================================
每个 MCPClient 对应 .mcp.json 里一个 server 条目。它负责：
    1) 启动子进程（command + args + env + cwd）
    2) 跑 initialize 握手
    3) request()  发请求并阻塞等响应；notify() 发通知不等
    4) stop()     优雅退出，atexit 会兜底

reader 线程持续读 stdout，按 id 分发到对应请求的 Event + result 盒，
因此任意一条 request() 的调用者都能被精确唤醒，不会错认结果。

============================================================================
二、省略的能力（教学边界）
============================================================================
不处理 server 主动推送的通知（tools/list_changed / logging 等）；
不处理 resources / prompts / elicitation；
不处理 SSE / WebSocket transport，只做最稳的 stdio。
"""
import json
import os
import subprocess
import threading
from pathlib import Path


PROTOCOL_VERSION = "2024-11-05"


class MCPClient:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict | None = None,
        cwd: str | Path | None = None,
    ):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = str(cwd) if cwd else None

        self.proc: subprocess.Popen | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        # request id 生成器
        self._next_id = 0
        self._id_lock = threading.Lock()

        # id → (Event, box)；reader 线程读到响应后，按 id 找到 box 填充并 set Event
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._pending_lock = threading.Lock()

        self.initialized = False
        self.last_error = ""

    # -------------------------------------------------------------------
    # 生命周期
    # -------------------------------------------------------------------

    def start(self, timeout: float = 10.0) -> str:
        """
        启动子进程并完成 initialize 握手。
        返回 "OK" 或 "Error: <reason>"，调用方据此判断是否可用。
        """
        full_env = os.environ.copy()
        full_env.update(self.env)

        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=full_env,
            )
        except (FileNotFoundError, OSError) as e:
            self.last_error = f"spawn failed: {e}"
            return f"Error: {self.last_error}"

        self.reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True,
        )
        self.reader_thread.start()

        try:
            self._initialize(timeout)
        except Exception as e:
            self.last_error = f"initialize failed: {e}"
            self.stop()
            return f"Error: {self.last_error}"

        self.initialized = True
        return "OK"

    def stop(self) -> None:
        """优雅退出：先 terminate，3 秒不走就 kill。幂等。"""
        self.stop_event.set()
        self.initialized = False
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    # -------------------------------------------------------------------
    # 内部：transport
    # -------------------------------------------------------------------

    def _next_request_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _send(self, obj: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise RuntimeError(f"mcp client '{self.name}' not running")
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _reader_loop(self) -> None:
        """
        持续读 stdout，按 id 分发响应到等待方。
        子进程挂掉 / stdout 关闭 → 退出循环；pending 里还在等的调用方会超时。
        """
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                if self.stop_event.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_id = msg.get("id")
                if msg_id is None:
                    # server → client 通知；教学版不处理
                    continue
                with self._pending_lock:
                    entry = self._pending.pop(msg_id, None)
                if entry:
                    evt, box = entry
                    box["msg"] = msg
                    evt.set()
        except Exception:
            pass

    # -------------------------------------------------------------------
    # 协议层：request / notify
    # -------------------------------------------------------------------

    def request(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """发送 JSON-RPC 请求并阻塞等响应；返回 result 部分。错误会抛异常。"""
        req_id = self._next_request_id()
        evt = threading.Event()
        box: dict = {}
        with self._pending_lock:
            self._pending[req_id] = (evt, box)

        self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        })

        if not evt.wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(
                f"mcp '{self.name}' {method} timeout after {timeout}s",
            )

        resp = box.get("msg") or {}
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(
                f"mcp '{self.name}' {method} error "
                f"{err.get('code')}: {err.get('message')}"
            )
        return resp.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        """发送 JSON-RPC 通知（无 id、无响应）。"""
        self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    # -------------------------------------------------------------------
    # 高层封装
    # -------------------------------------------------------------------

    def _initialize(self, timeout: float) -> None:
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "learn-claude-code", "version": "0.1.0"},
        }, timeout=timeout)
        self.notify("notifications/initialized")

    def list_tools(self, timeout: float = 10.0) -> list[dict]:
        result = self.request("tools/list", {}, timeout=timeout)
        return result.get("tools", [])

    def call_tool(
        self,
        tool: str,
        arguments: dict,
        timeout: float = 60.0,
    ) -> str:
        """
        调用远端工具。把 MCP 的 content blocks 压扁成字符串，
        方便和本地 handler 的返回值一致地流回主循环的 tool_result。
        isError=True 时在前面加 "Error (from mcp): " 便于 LLM 识别。
        """
        result = self.request(
            "tools/call",
            {"name": tool, "arguments": arguments},
            timeout=timeout,
        )
        parts = []
        for block in result.get("content") or []:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        output = "\n".join(parts) or "(empty)"
        if result.get("isError"):
            return f"Error (from mcp): {output}"
        return output
