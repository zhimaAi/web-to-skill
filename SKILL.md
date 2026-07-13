---
name: web-to-skill
description: "从网站或 URL 列表生成专用 Codex skill：先探测页面结构，再抓取渲染后的 HTML 快照，最后把索引、HTML 文档和按需刷新脚本组装成可复用的 HTML 资料 skill。适用于把语雀、飞书、OpenClaw 风格文档或其他网页资料转成可检索 skill。"
---

# Web To Skill

## 工作流

这个母版分三段使用：先探测并确定抓取策略，再抓取并保存渲染后的 HTML 快照，最后组装成专用 skill。

1. 先收集必要输入：起始 URL 或 URL 列表文件、输出目录，以及可选的最大页面数。不要一开始就要求用户给抓取深度，除非用户已经明确知道。
2. 如果用户给的是批量 URL 列表，跳过探测和链接发现，直接使用 `--url-list` 抓取；脚本会强制使用批量模式，只抓列表里的 URL，深度为
   0。
3. 如果是单个起始 URL，且站点结构未知或不清晰，先运行 `scripts/crawl_web_index.py --probe-only` 探测页面结构；探测结果写入
   `<out-dir>/probe.json`。
4. 查看 `probe.json`。如果命中了内置站点规则，直接使用规则给出的抓取策略和深度；站点规则优先于模型判断。
5. 如果没有命中站点规则，根据探测结果选择抓取策略：
    - `directory`：页面存在目录、侧边栏、知识库树、章节目录或表格目录链接时使用，通常配合 `--depth 1`。
    - `sequential-nav`：没有目录，但存在上一篇/下一篇、上一章/下一章等连续导航时使用；用 `--max-pages`
      作为安全上限，并给出足够覆盖链路的显式深度。
    - `body-depth`：既没有目录，也没有连续导航时使用，从正文链接继续向下抓取，通常配合 `--depth 3`。
6. 执行完整抓取。已知站点通常使用默认参数即可，因为脚本会根据站点规则解析策略和深度；未知站点需要传入模型判断后的
   `--crawl-strategy` 和数字 `--depth`。
7. 检查生成的 `index.json`。每个有效页面都应包含 `url`、从 `<head>` 提取的 `title`、`description`、`keywords`，以及
   `html_path`。`html_path` 指向的文件必须是渲染后的、去除脚本噪声的 HTML 快照，能被 `rg` 搜索并能支撑模型读取，不应只是
   JavaScript 应用壳。
8. 运行 `scripts/build_skill.py` 生成最终专用 skill zip。生成结果会包含压缩索引、渲染后的 HTML 快照，以及
   `scripts/fetch_rendered_html.py`，供最终 skill 在快照不足或需要最新内容时按 URL 重新抓取渲染 HTML。

## 抓取

优先使用 Playwright 模式，尤其是语雀、飞书这类 JavaScript 渲染较重的网站。只有静态文档站或快速验证时才考虑 HTTP 模式。

探测命令：

```bash
python3 scripts/crawl_web_index.py \
  --start-url "https://example.com/docs" \
  --probe-only \
  --out-dir work/probe
```

已知站点完整抓取：

```bash
python3 scripts/crawl_web_index.py \
  --start-url "https://example.com/docs" \
  --out-dir work/crawl \
  --max-pages 10000
```

未知站点在模型判断后完整抓取：

```bash
python3 scripts/crawl_web_index.py \
  --start-url "https://example.com/docs" \
  --out-dir work/crawl \
  --depth 3 \
  --crawl-strategy body-depth \
  --max-pages 10000
```

批量 URL 抓取：

```bash
python3 scripts/crawl_web_index.py \
  --url-list work/urls.txt \
  --out-dir work/crawl \
  --max-pages 10000
```

`--url-list` 接受 UTF-8 文本文件或 JSON 字符串数组。文本文件每行一个 URL；批量模式不会继续发现 `<a>` 链接，只抓列表中的
URL。

在 ChatWiki `llm_runner` 里运行时，命令通常从 `/workspace` 执行，skill 目录大多是只读的。使用实际挂载的脚本路径，例如
`clawbot/skills_system/alone_web/web-to-skill/scripts/crawl_web_index.py`，并把生成文件放到系统提示里的可写工作目录，通常是
`clawbot/working_dir/web-to-skill/<task_batch>/...`。不要在该容器里使用 `/workspace/work/...` 这类不可控路径。

抓取脚本会在 `--out-dir` 下写入 `index.json`、`raw/` 和 `html/`。`html/` 里的文件是供最终 skill 使用的渲染后 HTML 快照；
`raw/` 和分块文本只用于诊断。默认页面上限是 `--max-pages 10000`，默认超时是 `--timeout-ms 120000`，默认抓取器是
Playwright，默认并发是 `6`，默认链接范围是 `same-host`。只有站点确实需要时，才调整 `--concurrency`、`--browser http`、
`--timeout-ms`、`--wait-ms` 或 `--link-scope same-origin`。

抓取前会进行 DNS 校验，默认解析超时为 1 秒；解析到环回、私网、链路本地、未指定或组播地址时会拒绝访问，
并对每次重定向、页面请求和最终 URL 重新校验。该安全行为使用内置默认值，正常工作流无需额外传参。

如果缺少 Playwright，先在当前 Python 环境安装依赖和 Chromium：

```bash
python3 -m pip install playwright beautifulsoup4 lxml
python3 -m playwright install chromium
```

脚本内置了语雀、飞书和 OpenClaw 风格文档的抽取规则与抓取计划提示。语雀和飞书默认用目录抓取，深度为 1；OpenClaw
风格文档默认用目录抓取，深度为 2。这些站点计划只在 `--crawl-strategy auto` 且 `--depth auto` 时生效；显式传入的策略和深度不会被覆盖。

`probe.json` 会包含 `link_evidence.directory`、`link_evidence.sequential_nav` 和 `link_evidence.body`
样例。未知站点应结合这些链接样例和正文摘录来判断抓取策略。

## 组装

抓取到可用 HTML 后，运行：

```bash
python3 scripts/build_skill.py \
  --index-in work/crawl/index.json \
  --zip-out work/crawl/generated-skill.zip \
  --index-filename web-index.json \
  --skill-name example-docs \
  --skill-title "Example Docs" \
  --skill-description "用于根据 Example Docs 的渲染 HTML 快照回答问题，资料位于 references/web-index.json 和 references/html。" \
  --source-label "Example Docs"
```

生成的 skill 会把精简索引写入 `references/<index-filename>`，把渲染后的 HTML 复制到 `references/html/`，并加入
`scripts/fetch_rendered_html.py`。这个脚本接受一个 URL，使用无头 Chromium 获取最新渲染后的 HTML，并按本地快照同类结构输出
`data-rendered-snapshot` HTML 文档；脚本、样式和事件属性会被剔除，且会沿用内置站点规则里的正文选择器来裁剪语雀、飞书、OpenClaw
等页面的主体区域。默认输出到 stdout；在最终 skill 中只有当本地快照不足、用户要求最新内容，或需要核对当前网页状态时才使用。

`build_skill.py` 会生成 zip 包并只打印 zip 路径。如果省略 `--zip-out`，输出为 `generated-skill.zip` 或临时 `--skill-dir`
旁边的 zip。构建成功后，输入索引引用到的抓取中间文件会被清理，临时构建目录也会被删除，最终只保留 skill zip。

最终生成的 `SKILL.md` 会要求模型按固定顺序使用资料：

1. 先查 `references/<index-filename>`，用 `title`、`description`、`keywords`、`url` 和 `html_path` 匹配用户问题。
2. 如果索引命中，按命中项的 `html_path` 读取对应 HTML 文件，再基于文件内容回答。
3. 如果索引没有匹配，直接用 `rg` 搜索 `references/html/*.html`，再读取命中的 HTML 文件。
4. 如果本地 HTML 内容不足，或用户想查看最新网页内容，再调用 `scripts/fetch_rendered_html.py <url>` 抓取最新渲染
   HTML，并直接基于命令输出继续回答。

## 上下文控制

页面较大时遵循这些规则：

- `--max-pages` 默认是 `10000`。只有明确需要更小安全上限时才降低。
- 如果抓取结果里有 `crawl.truncated_by_max_pages: true`，说明页面数被上限截断；需要完整快照时用更大的 `--max-pages` 重跑。
- `--timeout-ms` 默认是 `120000`，只在页面明显加载慢时提高。
- 使用生成 skill 时，先通过索引缩小候选 HTML 文件范围，不要一开始读取大量 HTML。
- 如果索引没有识别出候选页，先用 `rg` 搜索 HTML 目录，再打开命中文件。
- `fetch_rendered_html.py` 用于补充最新网页内容，不替代本地索引和 HTML 快照的优先检索流程。默认不要传 `--out`，直接读取脚本
  stdout；输出必须是渲染后的 HTML 快照文档，不是原始脚本逻辑。只有输出过大、需要留档或调试时才使用隐藏的高级参数 `--out`。
- `build_skill.py` 成功后，工作流输出目录里应只剩最终 skill zip。
