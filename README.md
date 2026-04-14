# learn-claude-code

一个基于 Anthropic Claude API 构建的**最小化 AI 编码代理（Coding Agent）**，旨在通过清晰的代码结构，帮助开发者理解和学习 Agent Loop、工具调用（Tool Use）、子代理（SubAgent）、上下文压缩（Context Compaction）等核心概念。

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
- **上下文压缩（Context Compaction）的两级策略**
- **技能系统（Skill System）的按需加载机制**

---

## 🗂️ 项目结构

```
learn-claude-code/
├── main.py                      # 主入口：Agent Loop 核心实现
├── configs/
│   ├── __init__.py
│   └── configs.py               # 配置项：工作目录、模型名称、压缩阈值等
├── models/
│   ├── __init__.py
│   └── anthropic_client.py      # Anthropic 客户端初始化
├── tools/
│   ├── __init__.py              # 工具注册与路由（TOOLS / TOOL_HANDLERS）
│   ├── common.py                # 基础工具实现：bash、读写编辑文件
│   ├── compact_messages.py      # 上下文压缩逻辑（微压缩 + 全量压缩）
│   ├── skill.py                 # 技能系统：SkillRegistry 注册表与按需加载
│   ├── subagent.py              # 子代理：SubAgent 类 & AgentSkillTemplete
│   ├── todo.py                  # TODO 管理器：多步骤任务计划
│   └── utils.py                 # 工具函数（路径安全等）
├── skills/
│   ├── web_scraper/
│   │   └── SKILL.md             # 网页爬取技能文档
│   └── cards/
│       └── SKILL.md             # 示例技能文档
├── .task_outputs/
│   ├── tool-results/            # 持久化的大体积工具输出
│   └── transcripts/             # 压缩前的完整对话快照（.jsonl）
├── pyproject.toml               # 项目依赖与配置
└── .env                         # 环境变量（不提交至版本库，见下方配置说明）
```

---

## 🛠️ 可用工具（Tools）

代理内置以下工具，Claude 可在对话中自动调用：

| 工具名          | 功能描述                                       |
|---------------|----------------------------------------------|
| `bash`        | 执行 Shell 命令（内置危险命令拦截）                 |
| `read_file`   | 读取文件内容，支持行数限制                          |
| `write_file`  | 写入文件内容（自动创建父目录）                       |
| `edit_file`   | 精确替换文件中的指定文本（只替换一次）                 |
| `task`        | 派发子代理任务（全新上下文，共享文件系统）              |
| `todo`        | 更新当前会话的多步骤任务计划                         |
| `load_skill`  | 按需加载技能文档，将专项指令注入当前上下文              |
| `compact`     | 主动触发上下文全量压缩，可指定需重点保留的 focus 信息   |

---

## ⚙️ 配置说明

项目通过根目录下的 `.env` 文件读取密钥和配置，**请勿将 `.env` 文件提交到版本库**。

项目提供了 `.env.example` 作为模板，按以下步骤配置：

```bash
cp .env.example .env
```

然后编辑 `.env`，填入真实的密钥值：

| 变量名 | 是否必填 | 说明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ 必填 | Anthropic API 密钥，[前往获取](https://console.anthropic.com/settings/keys) |
| `DEEPSEEK_API_KEY` | 可选 | DeepSeek API 密钥，[前往获取](https://platform.deepseek.com/api_keys) |
| `DEEPSEEK_BASE_URL` | 可选 | DeepSeek API 地址，默认 `https://api.deepseek.com` |
| `DMX_API_KEY` | 可选 | DMX API 密钥 |
| `DMX_BASE_URL` | 可选 | DMX API 地址 |
| `GITHUB_PAT` | 可选 | GitHub Personal Access Token，用于 HTTPS 推送代码，需要 `repo` 权限，[前往获取](https://github.com/settings/tokens) |

> ⚠️ `.env` 已加入 `.gitignore`，请勿手动将其提交到版本库，避免密钥泄露。

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
┌──────────────────────────┐
│  将结果写回 messages      │
│  → 触发上下文压缩检查      │
└──────┬───────────────────┘
       │
       └──────────────────► 继续下一轮
```

---

## 🗜️ 上下文压缩（Context Compaction）

随着对话轮次增加，消息历史会不断膨胀并超出模型上下文窗口限制。本项目实现了**两级压缩策略**：

### Level 1 — 微压缩（Micro Compact）

每轮工具调用后自动触发，**不丢失任何轮次**，仅对历史工具结果进行截断：

- 保留最近 `KEEP_RECENT_TOOL_RESULTS`（默认 6）条工具结果的完整内容
- 更早的工具结果替换为占位文本：`[Earlier tool result compacted. Re-run the tool if you need full detail.]`

### Level 2 — 全量压缩（Full Compact）

当上下文预估大小超过阈值（`COMPACT_THRESHOLD`）时自动触发，或由 `compact` 工具主动调用：

1. 将完整对话历史以 `.jsonl` 格式保存到 `.task_outputs/transcripts/`（防止信息永久丢失）
2. 调用 Claude 对历史对话生成摘要，保留：当前目标、重要发现与决策、已读写的文件、剩余工作、用户约束
3. 用一条包含摘要的 `user` 消息替换全部历史，大幅缩减上下文体积
4. 支持传入 `focus` 参数，在摘要末尾追加需重点保留的信息

### 大体积输出持久化

工具输出超过 `PERSIST_THRESHOLD` 字节时，完整内容自动落盘至 `.task_outputs/tool-results/<tool_use_id>.txt`，消息中仅保留预览片段，避免单次输出撑爆上下文。

---

## 🤖 子代理（SubAgent）机制

当主代理调用 `task` 工具时，会启动一个**独立子代理**：

- ✅ 子代理拥有**全新的对话上下文**，不继承主代理的历史
- ✅ 子代理**共享文件系统**，可读写同一工作目录
- ✅ 子代理完成任务后，仅将**最终摘要**返回给主代理
- ✅ 最多运行 **30 轮**工具调用（安全上限）

---

## 🧩 技能系统（Skill System）

技能（Skill）是一种**按需加载的专项指令文档**，以 Markdown 文件的形式存放在 `skills/` 目录中。

### 工作原理

1. 每个技能对应 `skills/<name>/SKILL.md`，文件头部使用 YAML frontmatter 声明元信息
2. 启动时 `SkillRegistry` 自动扫描并注册所有技能，生成可用技能列表供系统提示使用
3. 代理调用 `load_skill` 工具时，技能的完整文档内容被注入当前上下文，代理即可按照文档指引执行专项任务

### 技能文档格式

```markdown
---
name: my_skill
description: 一句话描述这个技能的用途
---

# My Skill

具体的操作指引、步骤、注意事项...
```

### 内置技能

| 技能名          | 描述                              |
|---------------|----------------------------------|
| `web_scraper` | 爬取指定网页内容并将其转换保存为 Markdown 文件 |

---

## 📋 TODO 管理器

`TodoManager` 实现了一个单例的任务计划管理器：

- 最多 **12 个** 任务项
- 每个任务有三种状态：`pending`、`in_progress`、`completed`
- 同时只允许 **1 个** 任务处于 `in_progress` 状态
- 超过 **3 轮**未更新计划，会自动提醒代理刷新

---

## 📦 主要依赖

| 依赖包             | 版本要求      | 用途                     |
|-----------------|-------------|------------------------|
| `anthropic`     | >= 0.88.0   | Claude API 客户端         |
| `python-dotenv` | -           | 读取 .env 环境变量          |
| `openai`        | >= 2.30.0   | OpenAI 兼容接口（备用）      |
| `langchain`     | >= 1.2.15   | LLM 工具链（扩展用）         |

---

## 📄 License

MIT License

---

## 🙋 贡献与反馈

欢迎提交 Issue 和 Pull Request！如有问题，请在 [GitHub Issues](https://github.com/wx971025/my_cc/issues) 中反馈。
