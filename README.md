# web-to-skill

`web-to-skill` 是一个把网站内容转换为可复用 Codex skill 的模板。它先分析网站结构并抓取渲染后的 HTML，再生成页面索引，最后把索引、HTML
快照和按需刷新脚本打包成一个独立的 skill zip。

这个模板适合处理语雀、飞书、OpenClaw 风格文档站，以及其他需要通过浏览器渲染才能获得完整正文的网站。

## 核心能力

- 支持单个起始 URL 和批量 URL 列表。
- 支持 Playwright 浏览器渲染和 HTTP 抓取模式。
- 可先探测页面结构，再选择合适的链接发现策略。
- 内置语雀、飞书和 OpenClaw 风格文档的抓取规则。
- 保存去除脚本噪声的渲染后 HTML 快照，便于检索和模型读取。
- 生成包含精简索引、HTML 文档和实时刷新脚本的专用 skill zip。
- 对 DNS、重定向及最终 URL 进行安全校验，拒绝访问本地和私有网络地址。

## 工作流程

1. **探测**：分析起始页面的目录、连续导航和正文链接，生成 `probe.json`。
2. **抓取**：按照站点规则或选定策略抓取页面，生成 `index.json` 和 HTML 快照。
3. **校验**：确认索引包含有效页面，且每个 `html_path` 都指向可用的渲染后 HTML。
4. **组装**：根据索引和 HTML 快照生成专用 skill，并打包为 zip 文件。

批量 URL 模式会跳过链接发现，只抓取列表中明确提供的 URL。

## 目录结构

```text
web-to-skill/
├── SKILL.md
├── README.md
├── agents/
│   └── openai.yaml
└── scripts/
    ├── crawl_web_index.py
    ├── build_skill.py
    └── fetch_rendered_html.py
```

各文件用途如下：

- `SKILL.md`：供 Codex 加载的完整工作流和执行约束。
- `agents/openai.yaml`：skill 的界面名称、简介和默认提示词。
- `scripts/crawl_web_index.py`：探测或抓取网页，生成索引及 HTML 快照。
- `scripts/build_skill.py`：把抓取结果组装并压缩为专用 skill。
- `scripts/fetch_rendered_html.py`：为生成后的 skill 按需抓取最新渲染 HTML。

## 环境依赖

推荐使用 Python 3，并安装以下依赖：

```bash
python3 -m pip install playwright beautifulsoup4 lxml
python3 -m playwright install chromium
```

ChatWiki 的 `llm_runner` 容器已预装 Playwright、Chromium、Beautiful Soup、lxml、jq 和 rg，无需重复安装。

## 快速开始

以下命令默认从当前模板目录执行。若在 ChatWiki `llm_runner` 容器中运行，请使用完整相对路径：

```text
clawbot/skills_system/alone_web/web-to-skill/scripts/<script-name>.py
```

所有生成文件必须写入可写工作目录，例如：

```text
clawbot/working_dir/web-to-skill/<task_batch>/
```

### 1. 探测未知站点

```bash
python3 scripts/crawl_web_index.py \
  --start-url "https://example.com/docs" \
  --probe-only \
  --out-dir work/probe
```

探测结果位于 `work/probe/probe.json`。如果命中了内置站点规则，优先采用规则给出的策略和深度；否则根据探测证据选择抓取策略。

### 2. 抓取单个站点

已知站点可以使用自动策略：

```bash
python3 scripts/crawl_web_index.py \
  --start-url "https://example.com/docs" \
  --out-dir work/crawl \
  --max-pages 10000
```

未知站点应显式指定探测后选出的策略和深度：

```bash
python3 scripts/crawl_web_index.py \
  --start-url "https://example.com/docs" \
  --out-dir work/crawl \
  --crawl-strategy body-depth \
  --depth 3 \
  --max-pages 10000
```

### 3. 抓取批量 URL

准备一个 UTF-8 文本文件，每行一个 URL，也可以直接使用 JSON 字符串数组文件：

```bash
python3 scripts/crawl_web_index.py \
  --url-list work/urls.txt \
  --out-dir work/crawl \
  --max-pages 10000
```

批量模式固定使用深度 `0`，不会继续发现页面中的链接。

### 4. 生成专用 skill

```bash
python3 scripts/build_skill.py \
  --index-in work/crawl/index.json \
  --zip-out work/generate_skill/example-docs.zip \
  --index-filename web-index.json \
  --skill-name example-docs \
  --skill-title "Example Docs" \
  --skill-description "用于根据 Example Docs 的渲染 HTML 快照回答问题。" \
  --source-label "Example Docs"
```

构建成功后，脚本只向标准输出打印最终 zip 路径。`--skill-name` 只能包含小写字母、数字和连字符，长度为 1 到 63 个字符。

## 抓取策略

| 策略               | 适用场景                          | 常用深度    |
|------------------|-------------------------------|---------|
| `auto`           | 已命中内置站点规则，或让脚本自动解析计划          | `auto`  |
| `directory`      | 页面包含目录、侧边栏、知识库树或章节列表          | `1`     |
| `sequential-nav` | 页面通过上一篇、下一篇或上一章、下一章串联         | 按链路长度设置 |
| `body-depth`     | 没有目录和连续导航，需要从正文链接继续发现页面       | `3`     |
| `batch`          | 由 `--url-list` 自动启用，只抓取指定 URL | `0`     |

## 常用抓取参数

| 参数                 | 默认值          | 说明                                  |
|--------------------|--------------|-------------------------------------|
| `--out-dir`        | `work/crawl` | 索引、原始文本和 HTML 快照输出目录                |
| `--depth`          | `auto`       | 抓取深度，已知站点可由规则自动确定                   |
| `--crawl-strategy` | `auto`       | 链接发现策略                              |
| `--concurrency`    | `6`          | 并发抓取页面数                             |
| `--max-pages`      | `10000`      | 调度页面数上限，设为 `0` 表示不限制                |
| `--browser`        | `playwright` | 抓取后端，可选 `playwright` 或 `http`       |
| `--link-scope`     | `same-host`  | 链接范围，可选 `same-host` 或 `same-origin` |
| `--timeout-ms`     | `120000`     | 单页面导航或请求超时                          |
| `--wait-ms`        | `1000`       | Playwright 页面加载后的额外等待时间             |

## 抓取产物

完整抓取完成后，输出目录通常包含：

```text
work/crawl/
├── index.json
├── raw/
│   ├── *.txt
│   └── chunks/
└── html/
    └── *.html
```

- `index.json`：页面精简索引，包含 `url`、`title`、`description`、`keywords` 和 `html_path` 等字段。
- `raw/`：正文文本和分块文件，主要用于诊断。
- `html/`：去除脚本、样式和事件属性后的渲染 HTML，是最终专用 skill 的主要资料来源。
- `probe.json`：单起始 URL 流程在探测阶段生成，记录目录、连续导航和正文链接样例。

## 生成后的 skill

最终 zip 中包含：

```text
<generated-skill>/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── web-index.json
│   └── html/
│       └── *.html
└── scripts/
    └── fetch_rendered_html.py
```

生成后的 skill 会按以下顺序使用资料：

1. 先查询 `references/web-index.json`，缩小候选页面范围。
2. 读取命中项对应的本地 HTML 快照并回答问题。
3. 索引未命中时，使用 `rg` 搜索 `references/html/`。
4. 只有本地快照不足或需要最新内容时，才调用 `fetch_rendered_html.py`。

## 使用注意事项

- skill 模板目录在运行环境中通常是只读的，不要把抓取结果写入模板目录。
- 在 `llm_runner` 中使用相对路径，不要使用 `/workspace` 绝对路径、父目录跳转、管道或 shell 重定向。
- 优先使用 Playwright 抓取 JavaScript 渲染较重的网站；只有静态页面或快速验证时才使用 HTTP 模式。
- 构建前应确认 `index.json` 至少包含一个有效页面，并且所有 `html_path` 文件真实存在。
- 当 `crawl.truncated_by_max_pages` 为 `true` 时，说明抓取被页面上限截断，需要提高 `--max-pages` 后重新执行。
- `fetch_rendered_html.py` 只用于补充最新内容，不替代本地索引和 HTML 快照的优先检索流程。
