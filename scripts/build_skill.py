#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a specialized skill zip from a web index and saved rendered HTML files.")
    parser.add_argument("--index-in", required=True)
    parser.add_argument("--skill-dir", help="Temporary build directory. Defaults to a sibling directory derived from --zip-out.")
    parser.add_argument("--index-filename", required=True)
    parser.add_argument("--skill-name", required=True)
    parser.add_argument("--skill-title", required=True)
    parser.add_argument("--skill-description", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--zip-out", help="Output zip path. Defaults to <skill-dir>.zip, or generated-skill.zip.")
    return parser.parse_args()


def validate_skill_name(name: str) -> None:
    if not re.fullmatch(r"[a-z0-9-]{1,63}", name):
        raise SystemExit("skill name must be 1-63 chars of lowercase letters, digits, and hyphens")


def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def short_description(source_label: str) -> str:
    text = f"使用 {source_label} HTML 索引。"
    if len(text) <= 64:
        return text
    return "使用生成的 HTML 索引。"


def page_id_for(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def resolve_index_path(index_path: Path, stored_path: str) -> Path:
    path = Path(stored_path)
    if path.is_absolute():
        return path
    return index_path.parent / path


def compact_pages(index_path: Path, index: dict[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[Path, str]]]:
    pages: list[dict[str, Any]] = []
    html_copies: list[tuple[Path, str]] = []
    missing: list[str] = []
    used_paths: set[str] = set()
    for page in index.get("pages", []):
        if page.get("error"):
            continue
        url = str(page.get("url", ""))
        source_html_path = str(page.get("html_path", "")).strip()
        if not source_html_path:
            missing.append(url)
            continue
        source_html = resolve_index_path(index_path, source_html_path)
        if not source_html.is_file():
            missing.append(f"{url} -> {source_html_path}")
            continue

        suffix = source_html.suffix or ".html"
        html_rel_path = f"references/html/{page_id_for(url)}{suffix}"
        counter = 2
        while html_rel_path in used_paths:
            html_rel_path = f"references/html/{page_id_for(url)}-{counter}{suffix}"
            counter += 1
        used_paths.add(html_rel_path)
        html_copies.append((source_html, html_rel_path))
        pages.append(
            {
                "url": url,
                "title": page.get("title", ""),
                "description": page.get("description", ""),
                "keywords": page.get("keywords", []),
                "html_path": html_rel_path,
            }
        )
    if missing:
        preview = "\n".join(f"- {item}" for item in missing[:20])
        extra = "" if len(missing) <= 20 else f"\n... and {len(missing) - 20} more"
        raise SystemExit(f"{len(missing)} pages are missing saved HTML files:\n{preview}{extra}")
    return pages, html_copies


def zip_skill_dir(skill_dir: Path, zip_out: Path) -> None:
    zip_out.parent.mkdir(parents=True, exist_ok=True)
    if zip_out.exists():
        zip_out.unlink()
    with zipfile.ZipFile(zip_out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(skill_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(skill_dir.parent).as_posix())


def staging_dir_for_zip(zip_out: Path) -> Path:
    if zip_out.suffix:
        return zip_out.with_suffix("")
    return zip_out.parent / f"{zip_out.name}-build"


def resolve_build_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.zip_out:
        zip_out = Path(args.zip_out)
        skill_dir = Path(args.skill_dir) if args.skill_dir else staging_dir_for_zip(zip_out)
    else:
        skill_dir = Path(args.skill_dir) if args.skill_dir else Path("generated-skill")
        zip_out = skill_dir.with_suffix(".zip")
    return skill_dir, zip_out


def validate_build_paths(skill_dir: Path, zip_out: Path, index_path: Path) -> None:
    skill_resolved = skill_dir.resolve()
    zip_resolved = zip_out.resolve()
    index_resolved = index_path.resolve()
    if skill_resolved == skill_resolved.parent:
        raise SystemExit("--skill-dir must not be a filesystem root")
    if zip_resolved == skill_resolved or is_within(zip_resolved, skill_resolved):
        raise SystemExit("--zip-out must not be inside --skill-dir because the temporary build directory is removed")
    if index_resolved == skill_resolved or is_within(index_resolved, skill_resolved):
        raise SystemExit("--index-in must not be inside --skill-dir because the temporary build directory is removed")


def safe_resolve_index_path(index_path: Path, stored_path: str) -> Path | None:
    if not stored_path:
        return None
    path = resolve_index_path(index_path, stored_path)
    try:
        return path.resolve()
    except OSError:
        return None


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def cleanup_intermediates(index_path: Path, index: dict[str, Any], protected_paths: list[Path]) -> None:
    work_root = index_path.parent.resolve()
    protected = [path.resolve() for path in protected_paths]

    def is_protected(path: Path) -> bool:
        return any(path == item or is_within(path, item) for item in protected)

    files: set[Path] = {index_path.resolve()}
    for page in index.get("pages", []):
        for key in ("html_path", "content_path"):
            resolved = safe_resolve_index_path(index_path, str(page.get(key, "")))
            if resolved is not None:
                files.add(resolved)
        for chunk in page.get("chunks", []):
            resolved = safe_resolve_index_path(index_path, str(chunk.get("path", "")))
            if resolved is not None:
                files.add(resolved)

    for file_path in sorted(files, key=lambda item: len(str(item)), reverse=True):
        if not is_within(file_path, work_root) or is_protected(file_path):
            continue
        if file_path.is_file():
            file_path.unlink()

    for dir_path in sorted((p for p in work_root.rglob("*") if p.is_dir()), key=lambda item: len(str(item)), reverse=True):
        if is_protected(dir_path):
            continue
        try:
            dir_path.rmdir()
        except OSError:
            pass


def build_markdown(args: argparse.Namespace, index: dict[str, Any]) -> str:
    start_url = index.get("crawl", {}).get("start_url", "")
    generated_at = index.get("generated_at", "")
    html_root = "references/html"
    return f"""---
name: {args.skill_name}
description: {yaml_quote(args.skill_description)}
---

# {args.skill_title}

## 资料范围

回答有关 {args.source_label} 的问题时，优先使用本 skill 内的网页索引和已保存的渲染 HTML 快照。这批快照来自 {start_url}，生成时间为 {generated_at}，内容是去除脚本噪声后的渲染结果。

## 使用流程

1. 先查 `references/{args.index_filename}`。用用户问题匹配每条索引的 `title`、`description`、`keywords`、`url` 和 `html_path`。
2. 如果索引命中候选页，按命中项的 `html_path` 读取对应 HTML 文件；正文细节必须来自 HTML 文件，不要只凭索引字段回答。
3. 如果索引没有匹配项，直接用 `rg` 搜索所有 HTML 快照，然后读取命中的文件：

```bash
rg -n -i "用户问题关键词" {html_root} -g "*.html"
```

4. 如果本地 HTML 内容不足以支撑回答，或用户要求查看最新内容，使用无头浏览器脚本抓取目标 URL 的最新渲染 HTML。优先使用索引里的 `url`；如果用户给了新 URL，则使用用户给出的 URL。默认不要传 `--out`，脚本会把和本地快照同类结构的 `data-rendered-snapshot` HTML 文档直接输出到命令结果中：

```bash
python3 scripts/fetch_rendered_html.py "https://example.com/docs/page"
```

5. 抓取完成后，直接基于命令输出中的 HTML 继续回答，避免再发起一次文件读取。只有输出过大、需要留档或调试时，才使用隐藏的高级参数 `--out <path>` 写入文件。
6. 如果只使用本地快照回答，说明答案基于已保存的渲染 HTML 快照；如果调用了脚本，则说明使用了最新抓取的渲染 HTML。

## 索引与脚本

索引文件位于 `references/{args.index_filename}`，包含每个页面的 URL、从 `<head>` 提取的标题、描述、关键词和 `html_path`。渲染后的 HTML 快照位于 `{html_root}/`。

`scripts/fetch_rendered_html.py` 接受单个 URL，使用 Playwright 无头 Chromium 获取当前页面渲染后的 HTML 文档，并默认输出到 stdout。输出会剔除脚本、样式和事件属性，并沿用内置站点规则里的正文选择器来裁剪语雀、飞书、OpenClaw 等页面的主体区域。抓取前会进行 DNS 校验（默认解析超时为 1 秒），解析到环回、私网、链路本地、未指定或组播地址时会拒绝访问，并对每次重定向、页面请求和最终 URL 重新校验。该脚本用于补充最新内容，不替代“先索引、再 HTML、再 `rg`”的本地检索顺序。
"""


def build_openai_yaml(args: argparse.Namespace) -> str:
    default_prompt = f"使用 ${args.skill_name} 根据 {args.source_label} 的索引和渲染 HTML 快照回答问题；必要时用脚本抓取最新渲染 HTML。"
    return (
        "interface:\n"
        f"  display_name: {yaml_quote(args.skill_title)}\n"
        f"  short_description: {yaml_quote(short_description(args.source_label))}\n"
        f"  default_prompt: {yaml_quote(default_prompt)}\n"
    )


def copy_generated_scripts(skill_dir: Path) -> None:
    source = Path(__file__).with_name("fetch_rendered_html.py")
    if not source.is_file():
        raise SystemExit(f"missing generated skill helper script: {source}")
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, scripts_dir / "fetch_rendered_html.py")


def main() -> int:
    args = parse_args()
    validate_skill_name(args.skill_name)
    index_path = Path(args.index_in)
    index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    pages, html_copies = compact_pages(index_path, index)
    if not pages:
        raise SystemExit("no saved HTML pages available for generated skill")
    skill_dir, zip_out = resolve_build_paths(args)
    validate_build_paths(skill_dir, zip_out, index_path)
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    agents_dir = skill_dir / "agents"
    references_dir = skill_dir / "references"
    html_dir = references_dir / "html"
    agents_dir.mkdir(parents=True, exist_ok=True)
    references_dir.mkdir(parents=True, exist_ok=True)
    if html_dir.exists():
        shutil.rmtree(html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(build_markdown(args, index), encoding="utf-8")
    (agents_dir / "openai.yaml").write_text(build_openai_yaml(args), encoding="utf-8")
    (references_dir / args.index_filename).write_text(
        json.dumps(pages, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for source_html, rel_dest in html_copies:
        dest = skill_dir / rel_dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_html, dest)
    copy_generated_scripts(skill_dir)

    zip_skill_dir(skill_dir, zip_out)
    cleanup_intermediates(index_path, index, [skill_dir, zip_out])
    if skill_dir.exists():
        shutil.rmtree(skill_dir)

    print(zip_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
