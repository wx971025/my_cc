---
name: web_scraper
description: 爬取指定网页内容并将其转换保存为 Markdown 文件
---

# Web Scraper Skill

爬取一个或多个指定 URL 的网页，将页面正文内容转换为 Markdown 格式，并保存到本地文件。

# 默认产物位置
outputs/web_scraper/

## 用法

```bash
# 单页爬取
python skills/web_scraper/scrape.py <URL> [输出文件路径]

# 批量爬取
python skills/web_scraper/scrape.py --batch <url_list.txt> [输出目录]
```

### 参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `URL` | ✅（单页模式） | 要爬取的目标网页地址 |
| `输出文件路径` | ❌ | 保存的 .md 文件路径，默认自动根据页面标题生成 |
| `--batch` | ❌ | 批量模式标志 |
| `url_list.txt` | ✅（批量模式） | 包含 URL 列表的文本文件，每行一个，`#` 开头为注释 |
| `输出目录` | ❌ | 批量模式下的输出目录，默认为当前目录 |

### 示例

```bash
# 爬取页面，自动命名输出文件
python skills/web_scraper/scrape.py https://example.com

# 指定输出路径
python skills/web_scraper/scrape.py https://example.com output/page.md

# 批量爬取，输出到 ./output/ 目录
python skills/web_scraper/scrape.py --batch urls.txt ./output/
```

### url_list.txt 格式示例

```
# 这是注释，会被忽略
https://example.com
https://docs.python.org/3/
https://github.com
```

## 功能特性

- **自动提取标题**：提取页面 `<title>` 作为 Markdown 一级标题
- **语义化内容定位**：优先选取 `<main>`、`<article>` 等主体标签，减少导航/广告干扰
- **噪声过滤**：自动删除 `nav`、`footer`、`header`、`aside`、`script`、`style` 等干扰元素
- **Markdown 转换**：使用 `markdownify` 将 HTML 结构（标题、列表、链接、代码块、表格等）转为标准 Markdown
- **随机 UA**：内置多个真实 User-Agent 轮换，降低被反爬识别概率
- **SSL 自动降级**：HTTPS 证书验证失败时，自动降级为 `verify=False` 重试并打印警告
- **自动重试**：网络抖动/超时时最多重试 3 次，每次间隔递增 + 随机抖动
- **批量爬取**：支持从文本文件批量读取 URL，请求间自动随机延迟（1–3 秒）
- **元信息头**：输出文件包含 YAML front-matter（来源 URL、爬取时间）
- **安全文件名**：自动将页面标题转为合法文件名（去除非法字符、截断过长名称）

## 依赖

```
requests
beautifulsoup4
markdownify
lxml
```

均已在项目 `pyproject.toml` 中声明。
