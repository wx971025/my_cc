# learn-claude-code

一个基于 Anthropic Claude API 构建的**最小化 AI 编码代理（Coding Agent）**，旨在通过清晰的代码结构，帮助开发者理解和学习 Agent Loop、工具调用（Tool Use）、子代理（SubAgent）等核心概念。

---

## 📖 项目简介

本项目实现了一个可交互的命令行 AI 编码助手，核心思想是：

```
用户输入 → 调用 Claude 模型 → 检测 tool_use → 执行工具 → 返回结果 → 循环
```

通过这个极简实现，你可以直观地看到：
- Agent Loop 的完整运转机制
- Claude 工具调用（Function Calling）的处理方式
- 子代理（SubAgent）的任务委派与上下文隔离
- 多步骤任务的 TODO 计划管理

---

## 🗂️ 项目结构

```
learn-claude-code/
├── main.py                  # 主入口：Agent Loop 核心实现
├── configs/
│   ├── __init__.py
│   └── configs.py           # 配置项：工作目录、模型名称
├── model/
│   ├── __init__.py
│   └── anthropic_client.py  # Anthropic 客户端初始化
├── tools/
│   ├── __init__.py          # 工具注册与路由（TOOLS / TOOL_HANDLERS）
│   ├── common.py            # 基础工具实现：bash、读写编辑文件
│   ├── subagent.py          # 子代理：SubAgent 类 & AgentSkillTemplete
│   ├── todo.py              # TODO 管理器：多步骤任务计划
│   └── utils.py             # 工具函数（路径安全等）
├── utils/
│   ├── __init__.py
│   └── messages.py          # 消息处理工具函数
├── pyproject.toml           # 项目依赖与配置
└── .env                     # 环境变量（不提交至版本库，见下方配置说明）
```

---

## 🛠️ 可用工具（Tools）

代理内置以下工具，Claude 可在对话中自动调用：

| 工具名       | 功能描述                                 |
|------------|----------------------------------------|
| `bash`     | 执行 Shell 命令（内置危险命令拦截）          |
| `read_file`| 读取文件内容，支持行数限制                   |
| `write_file`| 写入文件内容（自动创建父目录）               |
| `edit_file`| 精确替换文件中的指定文本（只替换一次）         |
| `task`     | 派发子代理任务（全新上下文，共享文件系统）      |
| `todo`     | 更新当前会话的多步骤任务计划                  |

---

## ⚙️ 配置说明

项目通过根目录下的 `.env` 文件读取密钥和配置，**请勿将 `.env` 文件提交到版本库**。

在项目根目录创建 `.env` 文件，按如下格式填写：

```env
# Anthropic API Key（必填）
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# GitHub Personal Access Token（用于 Git 推送，可选）
GITHUB_PAT=your_github_pat_here

# 其他第三方 API Key（可选，按需填写）
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

> ⚠️ 请确保 `.env` 已加入 `.gitignore`，避免密钥泄露。

---

## 🚀 快速开始

### 1. 环境要求

- Python >= 3.12
- [uv](https://github.com/astral-sh/uv)（推荐包管理工具）

### 2. 安装依赖

```bash
uv sync
```

或使用 pip：

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

参考上方「配置说明」，创建并填写 `.env` 文件。

### 4. 启动代理

```bash
python main.py
```

启动后进入交互式命令行：

```
s01 >> 帮我列出当前目录下的所有 Python 文件
s01 >> 写一个冒泡排序的函数并保存到 sort.py
s01 >> q   # 输入 q 或 exit 退出
```

---

## 🔄 Agent Loop 工作原理

```
┌─────────────────────────────────────────┐
│              用户输入消息                  │
└─────────────────┬───────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────┐
│         调用 Claude API                  │
│    model.messages.create(...)            │
└─────────────────┬───────────────────────┘
                  │
          stop_reason == "tool_use"?
         /                          \
        是                           否
        │                             │
        ▼                             ▼
┌──────────────┐              ┌──────────────┐
│  执行工具     │              │  输出最终回复  │
│  并收集结果   │              │  结束循环     │
└──────┬───────┘              └──────────────┘
       │
       ▼
┌──────────────┐
│  将结果写回   │
│  messages    │
└──────┬───────┘
       │
       └──────────────────► 继续下一轮
```

---

## 🤖 子代理（SubAgent）机制

当主代理调用 `task` 工具时，会启动一个**独立子代理**：

- ✅ 子代理拥有**全新的对话上下文**，不继承主代理的历史
- ✅ 子代理**共享文件系统**，可读写同一工作目录
- ✅ 子代理完成任务后，仅将**最终摘要**返回给主代理
- ✅ 最多运行 **30 轮**工具调用（安全上限）

---

## 📋 TODO 管理器

`TodoManager` 实现了一个单例的任务计划管理器：

- 最多 **12 个** 任务项
- 每个任务有三种状态：`pending`、`in_progress`、`completed`
- 同时只允许 **1 个** 任务处于 `in_progress` 状态
- 超过 **3 轮**未更新计划，会自动提醒代理刷新

---

## 📦 主要依赖

| 依赖包         | 版本要求      | 用途                     |
|--------------|-------------|------------------------|
| `anthropic`  | >= 0.88.0   | Claude API 客户端         |
| `python-dotenv` | -        | 读取 .env 环境变量          |
| `openai`     | >= 2.30.0   | OpenAI 兼容接口（备用）      |
| `langchain`  | >= 1.2.15   | LLM 工具链（扩展用）         |

---

## 📄 License

MIT License

---

## 🙋 贡献与反馈

欢迎提交 Issue 和 Pull Request！如有问题，请在 [GitHub Issues](https://github.com/wx971025/my_cc/issues) 中反馈。
