# CronScheduler 生命周期说明（教学版）

这份文档专门解释 `modules/cron.py` 里的 `CronScheduler`：  
它怎么启动、怎么在后台跑、怎么停，以及 `threading.Event()` 在里面到底扮演什么角色。

---

## 先记住 3 个角色

`CronScheduler` 可以拆成三个角色：

1. **任务表（内存状态）**
   - `self.tasks`：当前所有定时任务（list[dict]）
   - 可选持久化：`durable=True` 的任务会落盘到 `.claude/scheduled_tasks.json`

2. **后台调度线程**
   - `self._thread`：一个 daemon 线程
   - 线程函数是 `_check_loop()`
   - 每秒醒一次，但只有“分钟变化”时才真正检查任务

3. **线程间通知队列**
   - `self.queue`（`queue.Queue`）
   - 后台线程把命中任务写入队列：`queue.put(...)`
   - 主线程拉取通知：`drain_notifications()`

---

## `threading.Event()` 是什么

你可以把 `threading.Event` 理解成一个**线程共享的“开关信号”**。

- 初始状态：`False`（未触发）
- `set()`：把信号设为 `True`
- `is_set()`：查询当前是不是 `True`
- `wait(timeout=...)`：等待信号变 `True`，或超时返回

在 `CronScheduler` 里，它被用作**停止后台线程**的控制信号：

- 后台线程循环条件：`while not self._stop_event.is_set():`
- 主线程停机时：`self._stop_event.set()`
- 后台线程会很快感知并退出

---

## 完整生命周期（从创建到停止）

### 阶段 0：对象创建（`__init__`）

创建时做了几件事：

- 初始化 `tasks`（空列表）
- 初始化 `queue`（线程安全队列）
- 初始化 `_stop_event`（停止信号，初始为 False）
- `_thread = None`
- `_last_check_minute = -1`（用于避免同一分钟重复检查）

这时调度器只是“准备好了”，**还没开始跑线程**。

---

### 阶段 1：启动（`start()`）

`start()` 做两步：

1. `_load_durable()`：从磁盘加载 durable 任务到 `self.tasks`
2. 创建并启动后台线程：
   - `threading.Thread(target=self._check_loop, daemon=True)`
   - `self._thread.start()`

线程一启动，就进入 `_check_loop()` 的 while 循环。

---

### 阶段 2：运行中（`_check_loop()`）

核心逻辑：

1. 每轮拿当前时间 `now = datetime.now()`
2. 计算当前分钟编号 `current_minute = hour * 60 + minute`
3. 如果分钟变了（和 `_last_check_minute` 不同）：
   - 更新 `_last_check_minute`
   - 调 `_check_tasks(now)` 做本分钟任务匹配
4. `self._stop_event.wait(timeout=1)`
   - 正常情况：1 秒后继续下一轮
   - 若主线程调用了 `set()`：会更快返回，下一轮看到 `is_set=True` 后退出

> 为什么不用 `time.sleep(1)`？  
> 因为 `wait(1)` 可以被 `set()` 立即唤醒，退出响应更快。

---

### 阶段 3：任务命中（`_check_tasks()`）

对每个任务：

1. 先判断是否自动过期（recurring 且超过 7 天）
2. 计算 `check_time`（考虑 jitter 偏移）
3. `cron_matches(task["cron"], check_time)` 命中则：
   - 生成通知文本
   - `self.queue.put(notification)` 放入队列
   - 更新 `task["last_fired"]`
   - 若是 one-shot，标记待删除

循环结束后：

- 清理过期任务 + 已触发 one-shot 任务
- 如有变更，调用 `_save_durable()` 同步 durable 数据到磁盘

---

### 阶段 4：主线程消费通知（`drain_notifications()`）

主线程在自己的时机调用：

- 循环 `queue.get_nowait()`
- 直到抛 `Empty` 为止
- 把本轮全部通知打包成 `list[str]` 返回

这就是典型的“后台生产，前台消费”模型。

---

### 阶段 5：停止（`stop()`）

`stop()` 两步：

1. `self._stop_event.set()`：发出停止信号
2. `self._thread.join(timeout=2)`：最多等 2 秒线程退出

此后后台线程结束，调度器进入停止状态。

---

## 一张时序图看懂

```text
主线程                                 后台线程(_check_loop)
 |                                             |
 | 创建 CronScheduler()                        |
 |-------------------------------------------->|
 |                                             | 初始化 tasks/queue/event
 | start()                                     |
 |-------------------------------------------->|
 |                                             | _load_durable()
 |                                             | while not event.is_set():
 |                                             |   now = datetime.now()
 |                                             |   if minute changed:
 |                                             |       _check_tasks(now)
 |                                             |       queue.put(notification)
 |                                             |   event.wait(1)
 | drain_notifications()                       |
 |<--------------------------------------------| 从 queue 拉取消息并处理
 | ...                                         |
 | stop()                                      |
 |-------------------------------------------->|
 |                                             | event.set() 生效
 |                                             | 循环退出
 | join(timeout=2) 等线程收尾                  |
 |<--------------------------------------------|
```

---

## 你现在最容易困惑的 3 个点

### 1) 这是“多线程并行执行”吗？

是。主线程和后台线程并行存在。  
但这个调度器的后台工作非常轻：每秒醒来检查一次“是否到新分钟”。

### 2) 为什么几乎没看到 `Lock`？

因为这里主要靠两点降低并发复杂度：

- 用 `Queue` 做跨线程通信（它自己线程安全）
- 对共享状态 `tasks` 的访问模式相对简单（教学版取可读性优先）

若后续你引入高频并发修改任务，建议加 `threading.Lock` 保护 `self.tasks`。

### 3) `Event` 和 `Queue` 有啥本质区别？

- `Event`：**信号**（状态位），“该停了”
- `Queue`：**数据通道**（消息容器），“有通知来了”

一个管“控制”，一个管“数据”。

---

## 对照源码建议

你可以按这个顺序读 `modules/cron.py`，最容易建立心智模型：

1. `CronScheduler.__init__`
2. `start` / `stop`
3. `_check_loop`
4. `_check_tasks`
5. `drain_notifications`
6. `_load_durable` / `_save_durable`

如果你愿意，我下一步可以再补一份“**带伪代码的最小可运行版**”（约 40 行），只保留 `Event + Thread + Queue` 三件套，帮助你彻底吃透这一套生命周期。  
