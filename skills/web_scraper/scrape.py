#!/usr/bin/env python3
"""
web_scraper skill — 爬取指定网页并将内容保存为 Markdown 文件

用法:
    python skills/web_scraper/scrape.py <URL> [输出文件路径]
    python skills/web_scraper/scrape.py --batch <url_list.txt> [输出目录]

示例:
    python skills/web_scraper/scrape.py https://example.com
    python skills/web_scraper/scrape.py https://example.com output/page.md
    python skills/web_scraper/scrape.py --batch urls.txt ./output/
"""

import sys
import re
import time
import random
import datetime
import warnings
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.exceptions import SSLError
from bs4 import BeautifulSoup
from markdownify import markdownify as md


# ── 配置 ──────────────────────────────────────────────────────────────────────

# 多个 User-Agent 轮换，降低被识别为爬虫的概率
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
]

# 爬取超时（秒）
TIMEOUT = 15

# 最大重试次数（网络抖动时自动重试）
MAX_RETRIES = 3

# 重试间隔（秒），支持随机抖动
RETRY_DELAY_BASE = 2.0

# 批量爬取时每个请求之间的随机延迟范围（秒）
BATCH_DELAY = (1.0, 3.0)

# 需要从页面中移除的干扰标签（BeautifulSoup 层面）
NOISE_TAGS = [
    "script", "style", "noscript", "iframe",
    "nav", "footer", "header", "aside",
    "form", "button", "input", "select", "textarea",
    "svg", "canvas", "video", "audio",
    "[document]", "head",
]

# markdownify 转换选项
# 注意：strip 与 convert 不能同时指定（markdownify 限制），只用 strip 来排除不需要的标签
MD_OPTIONS = {
    "heading_style": "ATX",          # # 风格标题
    "bullets": "-",                  # 无序列表用 -
    "code_language": "",             # 代码块不强制指定语言
    # 只保留 strip，排除干扰标签；不再指定 convert（两者互斥）
    "strip": ["script", "style", "noscript", "iframe",
              "form", "button", "input", "select", "textarea",
              "svg", "canvas", "video", "audio", "meta", "link"],
}


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _random_headers() -> dict:
    """随机选取一个 User-Agent 构造请求头"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def sanitize_filename(name: str) -> str:
    """将字符串转为合法文件名（去掉特殊字符，空格换下划线）"""
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]', "", name)   # Windows 非法字符
    name = re.sub(r"\s+", "_", name)
    name = name[:80]  # 防止文件名过长
    return name or "page"


def build_output_path(title: str, output_dir: Path | None = None) -> Path:
    """根据页面标题自动生成输出文件路径"""
    filename = sanitize_filename(title) + ".md"
    if output_dir:
        return output_dir / filename
    return Path(filename)


def fetch_page(url: str) -> requests.Response:
    """
    HTTP GET 请求，返回 Response 对象。

    策略：
    1. 先使用 verify=True（校验 SSL 证书）尝试请求，并在网络错误时自动重试。
    2. 若遇到 SSLError，自动降级为 verify=False（忽略证书）并打印警告后重试。
    """
    last_exc: Exception | None = None

    for verify in (True, False):
        if not verify:
            warnings.warn(
                f"[warn] SSL 证书验证失败，已降级为 verify=False 重试：{url}",
                stacklevel=2,
            )

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.get(
                    url,
                    headers=_random_headers(),
                    timeout=TIMEOUT,
                    verify=verify,
                    allow_redirects=True,
                )
                response.raise_for_status()
                return response
            except SSLError as e:
                # SSL 错误不重试，直接跳出内层循环降级
                last_exc = e
                break
            except requests.exceptions.HTTPError as e:
                print(f"[error] HTTP 错误 {e.response.status_code}：{url}", file=sys.stderr)
                sys.exit(1)
            except requests.exceptions.Timeout:
                print(f"[warn] 第 {attempt} 次请求超时，稍后重试…", file=sys.stderr)
                last_exc = TimeoutError(f"Timeout on attempt {attempt}")
            except requests.exceptions.RequestException as e:
                print(f"[warn] 第 {attempt} 次请求失败：{e}", file=sys.stderr)
                last_exc = e

            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_BASE * attempt + random.uniform(0, 1)
                time.sleep(delay)

        # 如果没有遇到 SSLError，就不再尝试 verify=False
        if not isinstance(last_exc, SSLError):
            break

    print(f"[error] 请求失败（已重试 {MAX_RETRIES} 次）：{last_exc}", file=sys.stderr)
    sys.exit(1)


def extract_content(html: str) -> tuple[str, str]:
    """
    从 HTML 中提取：
      - 页面标题 (str)
      - 主要内容的 HTML 片段 (str)
    """
    soup = BeautifulSoup(html, "lxml")

    # 提取标题
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Untitled"

    # 移除噪声标签
    for tag in NOISE_TAGS:
        for el in soup.find_all(tag):
            el.decompose()

    # 优先尝试语义化主体区域
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(id=re.compile(r"content|main|article|body", re.I))
        or soup.find(class_=re.compile(r"content|main|article|post|entry", re.I))
        or soup.find("body")
        or soup
    )

    return title, str(main)


def html_to_markdown(html_fragment: str) -> str:
    """将 HTML 片段转换为 Markdown 文本"""
    raw = md(html_fragment, **MD_OPTIONS)
    # 压缩连续空行（保留最多两个换行）
    cleaned = re.sub(r"\n{3,}", "\n\n", raw)
    return cleaned.strip()


def build_markdown(title: str, url: str, body_md: str) -> str:
    """组装最终的 Markdown 文档"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = (
        f"---\n"
        f"title: {title}\n"
        f"source: {url}\n"
        f"scraped_at: {now}\n"
        f"---\n\n"
    )
    heading = f"# {title}\n\n"
    return meta + heading + body_md


def save_markdown(content: str, output_path: Path) -> None:
    """将 Markdown 内容写入文件"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    print(f"[ok] 已保存至：{output_path.resolve()}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def scrape(url: str, output: str | None = None, output_dir: Path | None = None) -> Path:
    """
    爬取 url，转为 Markdown 并保存。

    Args:
        url:        目标网页地址
        output:     可选输出文件路径；若为 None，则自动根据页面标题命名
        output_dir: 批量模式下指定输出目录（output 优先级更高）

    Returns:
        保存的文件 Path
    """
    print(f"[info] 正在爬取：{url}")
    response = fetch_page(url)

    # 处理编码
    response.encoding = response.apparent_encoding or "utf-8"
    html = response.text

    title, content_html = extract_content(html)
    print(f"[info] 页面标题：{title}")

    body_md = html_to_markdown(content_html)
    full_md = build_markdown(title, url, body_md)

    if output:
        output_path = Path(output)
    else:
        output_path = build_output_path(title, output_dir)

    save_markdown(full_md, output_path)
    return output_path


def scrape_batch(url_file: str, output_dir: str | None = None) -> list[Path]:
    """
    批量爬取：从文件中逐行读取 URL，依次爬取并保存。

    Args:
        url_file:   包含 URL 列表的文本文件路径（每行一个 URL，# 开头为注释）
        output_dir: 输出目录；若为 None，则保存到当前目录

    Returns:
        成功保存的文件 Path 列表
    """
    urls_path = Path(url_file)
    if not urls_path.exists():
        print(f"[error] 文件不存在：{url_file}", file=sys.stderr)
        sys.exit(1)

    lines = urls_path.read_text(encoding="utf-8").splitlines()
    urls = [line.strip() for line in lines if line.strip() and not line.startswith("#")]

    if not urls:
        print("[warn] URL 文件中没有找到有效的 URL", file=sys.stderr)
        return []

    out_dir = Path(output_dir) if output_dir else Path(".")
    results: list[Path] = []
    total = len(urls)

    for i, url in enumerate(urls, 1):
        print(f"\n[batch] {i}/{total} — {url}")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            print(f"[skip] 无效 URL，跳过：{url!r}", file=sys.stderr)
            continue
        try:
            path = scrape(url, output_dir=out_dir)
            results.append(path)
        except SystemExit:
            print(f"[skip] 爬取失败，跳过：{url}", file=sys.stderr)

        # 批量爬取时随机延迟，避免过于频繁触发反爬
        if i < total:
            delay = random.uniform(*BATCH_DELAY)
            print(f"[batch] 等待 {delay:.1f}s …")
            time.sleep(delay)

    print(f"\n[batch] 完成：{len(results)}/{total} 个页面成功保存")
    return results


def main():
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        sys.exit(1)

    # 批量模式
    if args[0] == "--batch":
        if len(args) < 2:
            print("[error] --batch 需要指定 URL 列表文件路径", file=sys.stderr)
            sys.exit(1)
        url_file = args[1]
        output_dir = args[2] if len(args) >= 3 else None
        scrape_batch(url_file, output_dir)
        return

    # 单页模式
    url = args[0]
    output = args[1] if len(args) >= 2 else None

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        print(f"[error] URL 必须以 http:// 或 https:// 开头，得到：{url!r}", file=sys.stderr)
        sys.exit(1)

    scrape(url, output)


if __name__ == "__main__":
    main()
